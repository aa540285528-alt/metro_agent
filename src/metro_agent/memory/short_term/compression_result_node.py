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

from datetime import datetime, timezone  # 记录应用时间

import tiktoken  # 计算摘要的精确 token 数
from langgraph.types import Overwrite  # 强制覆盖 messages 列表（删除已压缩消息）

from metro_agent.memory.short_term.compression_queue import RedisCompressionQueue
from metro_agent.state import MetroAgentState
from metro_agent.observability.node_instrumentation import traced_node


# 全局实例（模块级单例）
queue = RedisCompressionQueue()
encoding = tiktoken.get_encoding("cl100k_base")  # 用于计算摘要 token 数


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

    # 已有摘要的 round_id 集合（用于幂等判断）
    summarized_ids = {
        item["round_id"]
        for item in existing_summaries
    }

    # 当前 messages 中所有消息的 ID 集合（用于完整性校验）
    current_message_ids = {
        message.id
        for message in messages
        if message.id
    }

    job_updates = {}         # 本轮要更新的 job 状态
    summary_updates = []     # 本轮新增的 ConversationSummary
    remove_message_ids = set()  # 本轮要移除的消息 ID

    for round_id, job in jobs.items():
        # 只处理已入队但尚未应用结果的任务
        if job.get("status") != "queued":
            continue

        # ----------------------------------------------------------
        # 从 Redis 读取压缩结果
        # ----------------------------------------------------------
        result = queue.get_result(job["job_id"])

        # Worker 尚未完成 → 跳过，下轮再检查
        if result is None:
            continue

        # ----------------------------------------------------------
        # 校验 1：Worker 报告压缩失败
        # ----------------------------------------------------------
        if result.get("status") == "failed":
            job_updates[round_id] = {
                "status": "failed",
                "error": result.get("error", "压缩失败"),
            }
            continue

        # ----------------------------------------------------------
        # 校验 2：结果与任务必须匹配
        # round_id 不一致 → 串号，拒绝
        # summary 为空 → LLM 返回异常，拒绝
        # ----------------------------------------------------------
        summary = str(result.get("summary", "")).strip()
        source_ids = set(job.get("source_message_ids", []))

        if result.get("round_id") != round_id or not summary:
            job_updates[round_id] = {
                "status": "failed",
                "error": "压缩结果与任务不匹配",
            }
            continue

        # ----------------------------------------------------------
        # 校验 3：原消息完整性检查
        # 如果该轮之前没有摘要，说明原消息必须仍在 messages 中。
        # 如果原消息 ID 不是当前消息 ID 的子集，说明消息已被截断/
        # 修改，此时拒绝应用（防止用过期结果覆盖）
        # 但如果该轮已有摘要（幂等重跑），则不要求原消息存在
        # ----------------------------------------------------------
        if (
            round_id not in summarized_ids
            and not source_ids.issubset(current_message_ids)
        ):
            job_updates[round_id] = {
                "status": "failed",
                "error": "原消息已变化，拒绝应用过期结果",
            }
            continue

        # ----------------------------------------------------------
        # 创建 ConversationSummary（幂等：已有摘要的轮次跳过）
        # ----------------------------------------------------------
        if round_id not in summarized_ids:
            summary_updates.append({
                "round_id": round_id,
                "round_number": job["round_number"],
                "summary": summary,
                "source_message_ids": list(source_ids),
                "source_ref": result["source_ref"],
                "token_count": len(
                    encoding.encode(summary)   # tiktoken 精确计数
                ),
                "created_at": result["completed_at"],
            })

        # 标记原消息待删除（无论是否已有摘要，都执行清理）
        remove_message_ids.update(source_ids)

        # 更新 job 状态为 applied
        job_updates[round_id] = {
            "status": "applied",
            "result_summary": summary,
            "applied_at": datetime.now(
                timezone.utc
            ).isoformat(),
            "error": "",
        }

    # 无任何结果可应用
    if not job_updates:
        return {}

    # --------------------------------------------------------------
    # 构造完整的 compression_jobs 更新
    # 将本轮 job_updates 合并到已有 jobs 中（字段级合并）
    # --------------------------------------------------------------
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

    # --------------------------------------------------------------
    # 计算全局压缩状态
    #   仍有 pending/queued/running → "compressing"（Worker 仍在工作）
    #   有 failed                  → "error"（需人工介入）
    #   全部 applied               → "idle"（压缩完成，系统静默）
    # --------------------------------------------------------------
    statuses = {
        job.get("status")
        for job in merged_jobs.values()
    }

    update = {
        "compression_jobs": job_updates,
        "compression_version": (
            state.get("compression_version", 0)
            + len(summary_updates)  # 每新增一个摘要版本号 +1
        ),
        "compression_status": (
            "compressing"
            if statuses & {"pending", "queued", "running"}
            else "error"
            if "failed" in statuses
            else "idle"
        ),
    }

    # 有新增摘要 → 追加到 conversation_summaries
    if summary_updates:
        update["conversation_summaries"] = summary_updates

    # 有待删除的消息 → 用 Overwrite 覆盖 messages 列表
    # 过滤掉 source_message_ids 中的消息，只保留未被压缩的
    if remove_message_ids:
        update["messages"] = Overwrite(
            value=[
                message
                for message in messages
                if message.id not in remove_message_ids
            ]
        )

    return update
