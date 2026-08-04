"""
CompressionQueueNode —— 压缩任务入队节点
========================================
将 compression_detection_node 创建的 pending 任务从 state 搬运到
Redis 可靠队列，完成"检测 → 入队"的最后一环。

这是压缩管线的第二个节点：
  detection_node (检测 + 创建 pending job)
    → queue_node (本节点，将 job 入队 Redis)
    → RedisCompressionQueue (可靠队列存储)
    → Worker 异步消费

为什么需要这个节点：
  detection_node 只在 state 中创建 CompressionJob（内存中的字典），
  需要本节点将其正式"提交"到 Redis 队列才算真正入队。
  将检测和入队分离的好处：
    1. 入队失败时可以在 state 中标记失败，不影响检测逻辑
    2. 入队涉及 Redis 网络 I/O，失败时可在此节点统一处理错误
"""

from datetime import datetime, timezone  # 记录入队时间戳

from metro_agent.memory.short_term.compression_queue import (
    RedisCompressionQueue,  # 可靠队列：提供 enqueue / reserve / ack / requeue
)
from metro_agent.state import MetroAgentState
from metro_agent.observability.node_instrumentation import traced_node


# 全局队列实例（模块级单例，复用 Redis 连接）
compression_queue = RedisCompressionQueue()


@traced_node("compression_enqueue")
def enqueue_compression_jobs(
    state: MetroAgentState,
) -> dict:
    """将 state 中所有 status=pending 的压缩任务入队 Redis。

    执行流程：
      1. 筛选 state["compression_jobs"] 中 status="pending" 的任务
      2. 按 round_id 从 messages 中提取对应轮次的原始消息
      3. 校验消息完整性（source_message_ids 与实际消息 ID 必须一致）
      4. 调用 compression_queue.enqueue() 写入 Redis
      5. 更新 job 状态：queued（成功）/ failed（失败）

    消息完整性校验：
      确保从 messages 中提取的消息与 detection_node 记录的
      source_message_ids 完全匹配。如果不匹配，说明消息历史
      在检测和入队之间发生了变化（如被截断），此时标记失败
      而非入队不完整的消息。

    Args:
        state: 当前全局状态，需包含：
               - thread_id: 对话线程 ID
               - messages: 对话消息历史
               - compression_jobs: 压缩任务字典（含 status="pending" 的任务）

    Returns:
        dict: 包含以下键的字典（无 pending 任务时返回空字典）：
          - compression_jobs:  更新后的任务字典（status: pending→queued/failed）
          - compression_status: "queued"（至少一个成功）或 "error"（全部失败）
          - compression_error:  所有失败轮次的错误信息（换行分隔）

    Raises:
        ValueError: 当 thread_id 缺失时抛出（入队 Redis 必须有 thread_id）。
    """
    thread_id = state.get("thread_id")

    if not thread_id:
        raise ValueError("压缩任务缺少thread_id")

    messages = list(state.get("messages", []))
    jobs = state.get("compression_jobs", {})

    # --------------------------------------------------------------
    # 步骤 1：筛选 status="pending" 的任务
    # 只处理尚未入队的任务，跳过已 queued/completed/failed 的
    # --------------------------------------------------------------
    pending_jobs = {
        round_id: job
        for round_id, job in jobs.items()
        if job.get("status") == "pending"
    }

    if not pending_jobs:
        return {}

    # --------------------------------------------------------------
    # 步骤 2：按 round_id 建立消息索引
    # 遍历所有消息，将同一 round_id 的消息归为一组，
    # 后续按轮次提取时 O(1) 查找
    # --------------------------------------------------------------
    messages_by_round: dict[str, list] = {}

    for message in messages:
        round_id = message.additional_kwargs.get(
            "round_id"
        )

        if round_id:
            messages_by_round.setdefault(
                round_id,
                [],
            ).append(message)

    # --------------------------------------------------------------
    # 步骤 3-5：逐任务校验 + 入队
    # --------------------------------------------------------------
    job_updates = {}
    errors = []

    for round_id, job in pending_jobs.items():
        # 从按轮次索引的消息中提取该轮的所有消息
        round_messages = messages_by_round.get(
            round_id,
            [],
        )

        # ----------------------------------------------------------
        # 步骤 3：消息完整性校验
        # detection_node 记录的 source_message_ids 是创建任务时的
        # 消息快照，必须与当前 messages 中的实际消息 ID 完全一致。
        # 不一致的可能原因：
        #   - 消息历史在检测后被截断（context_builder 限制了轮数）
        #   - Redis checkpoint 恢复后消息 ID 变化
        # ----------------------------------------------------------
        expected_ids = set(
            job.get("source_message_ids", [])
        )
        actual_ids = {
            message.id
            for message in round_messages
            if message.id
        }

        if not expected_ids or expected_ids != actual_ids:
            error = "压缩轮次的原始消息不完整"

            job_updates[round_id] = {
                "status": "failed",
                "error": error,
            }
            errors.append(f"{round_id}: {error}")
            continue

        # ----------------------------------------------------------
        # 步骤 4：写入 Redis 可靠队列
        # enqueue 内部：
        #   1. 将原始消息存入 SOURCE_PREFIX key（带 TTL）
        #   2. 将任务载荷 LPUSH 到 pending 队列
        # ----------------------------------------------------------
        try:
            source_ref = compression_queue.enqueue(
                thread_id=thread_id,
                job=job,
                messages=round_messages,
            )

            # 入队成功 → 更新状态为 queued
            job_updates[round_id] = {
                "status": "queued",
                "source_ref": source_ref,     # Redis key，Worker 据此加载原始消息
                "queued_at": datetime.now(
                    timezone.utc
                ).isoformat(),
                "error": "",                  # 清空之前的错误
            }

        except Exception as exc:
            # Redis 写入失败（网络故障、内存不足等）→ 标记失败
            error = str(exc)

            job_updates[round_id] = {
                "status": "failed",
                "error": error,
            }
            errors.append(f"{round_id}: {error}")

    if not job_updates:
        return {}

    # --------------------------------------------------------------
    # 判断整体状态：至少一个成功 → queued，全部失败 → error
    # --------------------------------------------------------------
    has_queued_job = any(
        update.get("status") == "queued"
        for update in job_updates.values()
    )

    return {
        "compression_jobs": job_updates,
        "compression_status": (
            "queued" if has_queued_job else "error"
        ),
        "compression_error": "\n".join(errors),
    }
