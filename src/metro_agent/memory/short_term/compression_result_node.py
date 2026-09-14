"""
CompressionResultNode —— 压缩结果应用节点
========================================
从 Redis 读取异步压缩任务的结果，将摘要写回 state 的
conversation_summaries，同时从 messages 中移除已被压缩的原始消息，
释放 state 空间。

这是压缩管线的最后一个节点：
  queue_node (入队 Redis) → Worker (异步调 LLM 生成摘要)
    → result_node (本节点，读取结果 + 写回 state + 清理原消息)

设计要点：
  1. 异步非阻塞：Worker 在后台执行压缩，本节点在每轮对话中
     检查结果是否就绪，不会阻塞用户
  2. 安全检查：应用结果前校验 round_id 匹配、摘要非空、
     原消息未被篡改
  3. 幂等性：同一轮次已存在摘要时不会重复创建，但仍会清理原消息
  4. 消息清理：压缩成功后从 messages 中移除原始消息，
     用摘要替代，大幅减少 state 体积
"""

from datetime import datetime, timezone

import tiktoken
from langgraph.types import Overwrite

from metro_agent.memory.short_term.compression_queue import RedisCompressionQueue
from metro_agent.state import MetroAgentState
from metro_agent.observability.node_instrumentation import traced_node



queue = RedisCompressionQueue()
encoding = tiktoken.get_encoding("cl100k_base")


@traced_node("compression_apply")
def apply_compression_results(
    state: MetroAgentState,
) -> dict:
    """从 Redis 读取压缩结果，应用到 state。

    执行流程：
      1. 扫描 compression_jobs 中 status="queued" 的任务
      2. 通过 queue.get_result() 查询 Redis 中的压缩结果
      3. 跳过尚未完成的任务（Worker 还在处理中）
      4. 校验结果有效性（round_id 匹配、摘要非空、原消息完整）
      5. 创建 ConversationSummary 并追加到 conversation_summaries
      6. 从 messages 中移除已被压缩的原始消息
      7. 更新 job 状态：queued → applied / failed

    安全检查（防止数据损坏）：
      - 结果中的 round_id 必须与任务一致（防止串号）
      - 摘要文本不能为空
      - 如果该轮尚未有摘要，原消息必须仍完整存在（防止截断后
        用过期结果覆盖）

    Args:
        state: 当前全局状态。

    Returns:
        dict: 包含以下键的字典（无结果可应用时返回空字典）：
          - compression_jobs:        更新的 job 状态
          - compression_version:     递增的版本号
          - compression_status:      "compressing" / "error" / "idle"
          - conversation_summaries:  新增的摘要条目
          - messages:               Overwrite 覆盖（移除已压缩的消息）
    """
    jobs = state.get("compression_jobs", {})
    messages = list(state.get("messages", []))
    existing_summaries = state.get("conversation_summaries", [])


    summarized_ids = {
        item["round_id"]
        for item in existing_summaries
    }


    current_message_ids = {
        message.id
        for message in messages
        if message.id
    }

    job_updates = {}
    summary_updates = []
    remove_message_ids = set()

    for round_id, job in jobs.items():

        if job.get("status") != "queued":
            continue




        result = queue.get_result(job["job_id"])


        if result is None:
            continue




        if result.get("status") == "failed":
            job_updates[round_id] = {
                "status": "failed",
                "error": result.get("error", "压缩失败"),
            }
            continue






        summary = str(result.get("summary", "")).strip()
        source_ids = set(job.get("source_message_ids", []))

        if result.get("round_id") != round_id or not summary:
            job_updates[round_id] = {
                "status": "failed",
                "error": "压缩结果与任务不匹配",
            }
            continue








        if (
            round_id not in summarized_ids
            and not source_ids.issubset(current_message_ids)
        ):
            job_updates[round_id] = {
                "status": "failed",
                "error": "原消息已变化，拒绝应用过期结果",
            }
            continue




        if round_id not in summarized_ids:
            summary_updates.append({
                "round_id": round_id,
                "round_number": job["round_number"],
                "summary": summary,
                "source_message_ids": list(source_ids),
                "source_ref": result["source_ref"],
                "token_count": len(
                    encoding.encode(summary)
                ),
                "created_at": result["completed_at"],
            })


        remove_message_ids.update(source_ids)


        job_updates[round_id] = {
            "status": "applied",
            "result_summary": summary,
            "applied_at": datetime.now(
                timezone.utc
            ).isoformat(),
            "error": "",
        }


    if not job_updates:
        return {}





    merged_jobs = {
        **jobs,
        **{
            round_id: {
                **jobs.get(round_id, {}),
                **update,
            }
            for round_id, update in job_updates.items()
        },
    }







    statuses = {
        job.get("status")
        for job in merged_jobs.values()
    }

    update = {
        "compression_jobs": job_updates,
        "compression_version": (
            state.get("compression_version", 0)
            + len(summary_updates)
        ),
        "compression_status": (
            "compressing"
            if statuses & {"pending", "queued", "running"}
            else "error"
            if "failed" in statuses
            else "idle"
        ),
    }


    if summary_updates:
        update["conversation_summaries"] = summary_updates



    if remove_message_ids:
        update["messages"] = Overwrite(
            value=[
                message
                for message in messages
                if message.id not in remove_message_ids
            ]
        )

    return update
