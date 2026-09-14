"""
CompressionDetectionNode —— 压缩任务检测节点
============================================
在每轮对话结束后扫描消息历史，识别已完成但尚未压缩的旧轮次，
为其创建 CompressionJob 并写入 state，供后续异步压缩节点消费。

设计动机：
  压缩（摘要生成）不应在对话主流程中同步执行，原因：
    1. LLM 调用耗时较长，会阻塞用户等待回复
    2. 并非每轮对话都需要立即压缩（近期轮次应完整保留）
  因此采用"检测 → 入队 → 异步执行"的模式：
    - 本节点只负责检测和创建任务（极快，纯 Python 逻辑）
    - 下游 compression_node 异步消费任务队列，调 LLM 生成摘要

与上下游的关系：
  context_node (轮开始) → Agent 执行 → response_node (轮结束)
    → detect_compression_jobs (本节点，检测 + 入队)
    → compression_node (异步消费，调 LLM 生成摘要)
"""

from datetime import datetime, timezone
from uuid import uuid4

from metro_agent.config import SHORT_MEMORY_KEEP_ROUNDS
from metro_agent.memory.short_term.conversation_segmenter import (
    ConversationSegmenter,
)
from metro_agent.state import MetroAgentState
from metro_agent.observability.node_instrumentation import traced_node



segmenter = ConversationSegmenter()


@traced_node("compression_detect")
def detect_compression_jobs(
    state: MetroAgentState,
) -> dict:
    """检测已完成但尚未压缩的旧轮次，创建 CompressionJob 入队。

    检测逻辑（按优先级过滤）：
      1. 按 HumanMessage 锚点将消息分组为"轮"
      2. 过滤：只保留已完成的轮次（同时包含 human 和 ai 消息）
      3. 提取：从消息 additional_kwargs 中取 round_id / round_number
      4. 排序：按 round_number 升序（旧→新）
      5. 排除：去掉最近 SHORT_MEMORY_KEEP_ROUNDS 轮（应完整保留原文）
      6. 去重：跳过已有摘要（conversation_summaries）和已有任务（compression_jobs）的轮次
      7. 入队：为剩余的轮次创建 CompressionJob（status=pending）

    幂等性保证：
      - 同一 round_id 只创建一次 CompressionJob
      - 已生成摘要的轮次不会再创建新任务
      - 可安全地每轮重复调用，不会产生重复任务

    Args:
        state: 当前全局状态，需包含：
               - messages: 对话消息历史
               - conversation_summaries: 已生成的摘要列表
               - compression_jobs: 已有的压缩任务字典
               - compression_version: 当前压缩版本号

    Returns:
        dict: 包含以下键的字典（无新任务时返回空字典）：
          - compression_jobs:  新增的 CompressionJob 字典（key=round_id）
          - compression_status: "pending"（触发异步消费）
          - compression_error:  清空之前的错误信息
    """
    messages = list(state.get("messages", []))




    rounds = segmenter.group_rounds(messages)




    completed_rounds = []

    for round_messages in rounds:

        message_types = {
            message.type
            for message in round_messages
        }



        if "human" not in message_types or "ai" not in message_types:
            continue



        metadata_message = next(
            (
                message
                for message in round_messages
                if message.additional_kwargs.get("round_id")
            ),
            None,
        )


        if metadata_message is None:
            continue

        round_id = metadata_message.additional_kwargs["round_id"]
        round_number = metadata_message.additional_kwargs["round_number"]

        completed_rounds.append({
            "round_id": round_id,
            "round_number": round_number,
            "messages": round_messages,
        })




    completed_rounds.sort(
        key=lambda item: item["round_number"]
    )





    compressible_rounds = completed_rounds[
        :-SHORT_MEMORY_KEEP_ROUNDS
    ]


    if not compressible_rounds:
        return {}






    summarized_ids = {
        summary["round_id"]
        for summary in state.get(
            "conversation_summaries",
            [],
        )
    }


    existing_jobs = state.get("compression_jobs", {})
    new_jobs = {}

    for round_data in compressible_rounds:
        round_id = round_data["round_id"]


        if round_id in summarized_ids:
            continue


        if round_id in existing_jobs:
            continue


        source_message_ids = [
            message.id
            for message in round_data["messages"]
            if message.id
        ]


        if not source_message_ids:
            continue


        new_jobs[round_id] = {
            "job_id": str(uuid4()),
            "round_id": round_id,
            "round_number": round_data["round_number"],
            "source_message_ids": source_message_ids,
            "base_summary_version": state.get(
                "compression_version",
                0,
            ),
            "status": "pending",
            "requested_at": datetime.now(
                timezone.utc
            ).isoformat(),
        }


    if not new_jobs:
        return {}


    return {
        "compression_jobs": new_jobs,
        "compression_status": "pending",
        "compression_error": "",
    }
