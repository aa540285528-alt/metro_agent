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

from datetime import datetime, timezone  # 记录任务发起时间（UTC）
from uuid import uuid4                    # 为每个压缩任务生成唯一 job_id

from metro_agent.config import SHORT_MEMORY_KEEP_ROUNDS  # 最近 N 轮不压缩（完整保留）
from metro_agent.memory.short_term.conversation_segmenter import (
    ConversationSegmenter,  # 复用其 group_rounds 方法将消息按轮分组
)
from metro_agent.state import MetroAgentState
from metro_agent.observability.node_instrumentation import traced_node


# 全局分段器实例（模块级单例）
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

    # --------------------------------------------------------------
    # 步骤 1：按轮分组
    # --------------------------------------------------------------
    rounds = segmenter.group_rounds(messages)

    # --------------------------------------------------------------
    # 步骤 2-3：过滤已完成的轮次 + 提取元数据
    # --------------------------------------------------------------
    completed_rounds = []

    for round_messages in rounds:
        # 收集该轮中出现的所有消息类型
        message_types = {
            message.type
            for message in round_messages
        }

        # 必须同时包含 human（用户提问）和 ai（助手回复）才算完整的一轮
        # 当前轮可能只有 human 消息（AI 尚未回复），不能压缩
        if "human" not in message_types or "ai" not in message_types:
            continue

        # 从该轮消息中提取包含 round_id 元数据的那条消息
        # 通常是 context_node 中包装的 HumanMessage
        metadata_message = next(
            (
                message
                for message in round_messages
                if message.additional_kwargs.get("round_id")
            ),
            None,
        )

        # 无元数据的轮次无法追踪，跳过
        if metadata_message is None:
            continue

        round_id = metadata_message.additional_kwargs["round_id"]
        round_number = metadata_message.additional_kwargs["round_number"]

        completed_rounds.append({
            "round_id": round_id,
            "round_number": round_number,
            "messages": round_messages,
        })

    # --------------------------------------------------------------
    # 步骤 4：按轮次序号升序排列（旧→新）
    # --------------------------------------------------------------
    completed_rounds.sort(
        key=lambda item: item["round_number"]
    )

    # --------------------------------------------------------------
    # 步骤 5：排除最近 N 轮（SHORT_MEMORY_KEEP_ROUNDS）
    # 最近 N 轮完整保留在 messages 中，不压缩
    # --------------------------------------------------------------
    compressible_rounds = completed_rounds[
        :-SHORT_MEMORY_KEEP_ROUNDS
    ]

    # 没有可压缩的轮次（对话还不够长）
    if not compressible_rounds:
        return {}

    # --------------------------------------------------------------
    # 步骤 6-7：去重 + 入队
    # --------------------------------------------------------------

    # 已生成摘要的 round_id 集合（不再重复创建任务）
    summarized_ids = {
        summary["round_id"]
        for summary in state.get(
            "conversation_summaries",
            [],
        )
    }

    # 已存在任务（pending/running/completed）的 round_id，避免重复
    existing_jobs = state.get("compression_jobs", {})
    new_jobs = {}

    for round_data in compressible_rounds:
        round_id = round_data["round_id"]

        # 去重 1：已有摘要 → 跳过
        if round_id in summarized_ids:
            continue

        # 去重 2：已有压缩任务 → 跳过
        if round_id in existing_jobs:
            continue

        # 收集该轮中所有有 ID 的消息（用于溯源）
        source_message_ids = [
            message.id
            for message in round_data["messages"]
            if message.id
        ]

        # 无消息 ID 的轮次无法关联原始消息，跳过
        if not source_message_ids:
            continue

        # 创建新的压缩任务（status=pending，等待异步消费）
        new_jobs[round_id] = {
            "job_id": str(uuid4()),
            "round_id": round_id,
            "round_number": round_data["round_number"],
            "source_message_ids": source_message_ids,
            "base_summary_version": state.get(
                "compression_version",
                0,
            ),
            "status": "pending",              # 初始状态：等待处理
            "requested_at": datetime.now(
                timezone.utc
            ).isoformat(),
        }

    # 无新任务（所有可压缩轮次都已处理）
    if not new_jobs:
        return {}

    # 有新增任务 → 写入 state，触发下游异步消费
    return {
        "compression_jobs": new_jobs,
        "compression_status": "pending",  # 通知下游：有待处理任务
        "compression_error": "",          # 清空旧错误，开始新一轮压缩周期
    }
