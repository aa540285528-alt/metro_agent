"""
CompressionWorker —— 压缩任务异步消费者
=======================================
独立运行的 Worker 进程，从 Redis 可靠队列中预占压缩任务，
调用本地 Ollama 模型生成单轮摘要，结果写回 Redis 供
compression_result_node 读取。

为什么不把压缩放在 LangGraph 图中：
  1. LLM 调用耗时（Ollama 本地模型可能数秒到数十秒），
     同步执行会阻塞用户等待回复
  2. 压缩并非每轮都必须完成（下一轮开头才应用结果），
     异步执行不影响对话流畅性
  3. 独立进程可单独管理（启动/停止/重启），不影响主服务

启动方式：
  python -m short_memory_system.compression_worker
  或由进程管理器（supervisor/systemd/docker）托管

与主流程的交互：
  主流程（LangGraph）               Worker（本文件）
  ────────────────                 ────────────────
  enqueue_compression_jobs
    → LPUSH pending 队列  ─────→   BRPOPLPUSH 预占任务
                                   load_source 加载消息
                                   summarize 生成摘要
                                   save_result 写回 Redis
  apply_compression_results  ←───  get_result 读取摘要
"""

from datetime import datetime, timezone  # 记录完成时间

from metro_agent.memory.short_term.compression_queue import (
    RedisCompressionQueue,  # 可靠队列：reserve / load_source / save_result / ack
)
from metro_agent.memory.short_term.round_summarizer import (
    RoundConversationSummarizer,  # 单轮摘要器（本地 Ollama，≤100 字）
)


def run_worker() -> None:
    """启动压缩 Worker 主循环。

    循环逻辑：
      1. reserve() 阻塞等待任务（timeout=5s）
      2. 无任务 → 继续循环（定期检查退出信号）
      3. 有任务 → load_source 加载原始消息
      4. 调 Ollama 生成摘要
      5. save_result 保存结果到 Redis
      6. acknowledge 确认完成（LREM processing）

    故障处理：
      - 任何异常（LLM 超时/Ollama 不可用/网络故障）都会被捕获
      - 失败时将错误写入 result（status="failed"），而非 requeue
      - 失败的任务不会重新入队（避免死循环），由主流程
        compression_result_node 读取 failed 状态后标记
      - 如果希望重试，可改为 queue.requeue(reserved_job)

    注意：Worker 使用本地 Ollama 模型（Qwen2.5 7B），
    而非云端大模型。原因是：
      - 摘要任务简单，不需要强推理能力
      - 本地调用零成本、低延迟
      - ≤100 字的摘要限制防止本地模型跑偏
    """
    queue = RedisCompressionQueue()
    summarizer = RoundConversationSummarizer()

    print("Ollama压缩Worker已启动")

    while True:
        # ----------------------------------------------------------
        # 步骤 1：阻塞预占任务（BRPOPLPUSH）
        # 5 秒超时让循环定期醒来，检查是否需要退出
        # ----------------------------------------------------------
        reserved_job = queue.reserve(timeout=5)

        # 超时无任务 → 继续循环
        if reserved_job is None:
            continue

        job = reserved_job.payload
        job_id = job["job_id"]

        try:
            # ------------------------------------------------------
            # 步骤 2：加载原始消息
            # 通过 enqueue 时写入的 source_ref 从 Redis 读取
            # ------------------------------------------------------
            messages = queue.load_source(
                job["source_ref"]
            )

            # ------------------------------------------------------
            # 步骤 3：调用本地 Ollama 生成单轮摘要
            # RoundConversationSummarizer：
            #   - 格式化消息为 "角色:内容" 文本
            #   - 调 Ollama Qwen2.5 7B 生成 ≤100 字摘要
            #   - 空摘要或超长摘要 → 抛出 ValueError
            # ------------------------------------------------------
            summary = summarizer.summarize(messages)

            # ------------------------------------------------------
            # 步骤 4：保存成功的压缩结果
            # 结果包含原始 job 元数据 + status + summary + 时间戳
            # ------------------------------------------------------
            queue.save_result(job_id, {
                **job,                              # 继承入队时的元数据
                "status": "completed",
                "summary": summary,
                "completed_at": datetime.now(
                    timezone.utc
                ).isoformat(),
            })

            # 步骤 5：确认完成，从 processing 备份队列移除
            queue.acknowledge(reserved_job)

            print(
                f"压缩完成：round={job['round_number']}"
            )

        except Exception as exc:
            # ------------------------------------------------------
            # 失败处理：保存错误结果 + ack（不 requeue）
            # 选择 ack 而非 requeue 的原因：
            #   - 避免因持续性故障（如 Ollama 崩溃）导致无限重试
            #   - 失败信息写入 result，主流程可感知并展示
            #   - 如需重试，可后续手动将 failed 任务重新入队
            # ------------------------------------------------------
            queue.save_result(job_id, {
                **job,
                "status": "failed",
                "error": str(exc),
                "completed_at": datetime.now(
                    timezone.utc
                ).isoformat(),
            })

            # 仍 ack（从 processing 移除），不阻塞后续任务
            queue.acknowledge(reserved_job)

            print(
                f"压缩失败：job={job_id}, error={exc}"
            )


if __name__ == "__main__":
    try:
        run_worker()
    except KeyboardInterrupt:
        print("压缩Worker已停止")