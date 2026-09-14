"""
MemoryObserverNode —— 短期记忆统计观察节点
==========================================
非侵入式地收集短期记忆系统的运行时统计指标，写入 state 供监控、调试和
日志记录使用。

设计动机：
  短期记忆系统涉及多个异步流程（消息截断、压缩入队、Worker 消费、结果应用），
  系统状态随时间动态变化。需要一个统一的观测点来采集关键指标，帮助：
    1. 诊断上下文膨胀问题（raw_tokens / total_tokens 是否过高）
    2. 监控压缩管线健康状态（queued_jobs / failed_jobs / compression_status）
    3. 追踪记忆容量趋势（raw_round_count + summary_count 是否接近上限）

在 LangGraph 工作流中的位置：
  通常放在 Agent 回复之后、压缩检测之前。
  这是一个纯读取节点——只读取 state，不修改任何数据，
  对主流程零副作用。

使用方式：
  stats = collect_short_memory_stats(state)
  # stats["short_memory_stats"]["total_tokens"] → 总 token 占用
  # stats["short_memory_stats"]["compression_status"] → "idle" | "compressing" | "error"

与上下游的关系：
  上游：state["messages"] / state["conversation_summaries"] / state["compression_jobs"]
  下游：监控面板、日志系统、告警规则
"""

from metro_agent.memory.short_term.conversation_segmenter import ConversationSegmenter
from metro_agent.state import MetroAgentState
from metro_agent.observability.node_instrumentation import traced_node



segmenter = ConversationSegmenter()


@traced_node("short_memory_collect")
def collect_short_memory_stats(
    state: MetroAgentState,
) -> dict:
    """收集短期记忆系统的运行时统计指标。

    本函数纯读取 state，不产生任何副作用。统计指标分为四类：

      1. 原始消息指标
         - raw_message_count: messages 列表中的消息总数
         - raw_round_count: 按 HumanMessage 锚点分组后的对话轮数
         - raw_tokens: 原始消息的总 token 数（cl100k_base 编码估算）

      2. 摘要指标
         - summary_count: 已生成的压缩摘要数量
         - summary_tokens: 所有摘要的 token 数之和

      3. 聚合指标
         - total_tokens: raw_tokens + summary_tokens
           （当前短期记忆的总 token 占用，用于判断是否接近上限）

      4. 压缩管线健康指标
         - queued_jobs: 已入队 Redis 但 Worker 尚未完成的压缩任务数
         - failed_jobs: 压缩失败的任务数
         - compression_status: 压缩管线整体状态
           "idle"        → 无活跃任务，系统静默
           "compressing" → 有任务在处理中（pending/queued/running 任一存在）
           "error"       → 有任务失败，需人工介入

    典型告警场景：
      - total_tokens > SHORT_MEMORY_MAX_TOTAL_TOKENS × 0.8
        → 短期记忆接近容量上限，即将触发 pruner 裁剪
      - failed_jobs > 0 且持续增长
        → 压缩管线异常（Ollama 不可用 / Redis 连接故障）
      - queued_jobs > 10 且持续增长
        → Worker 消费速度跟不上，可能需要增加 Worker 数量
      - compression_status == "error" 持续多轮
        → 需要检查 compression_error 字段获取具体错误信息

    Args:
        state: MetroAgent 全局状态，从中读取：
               - messages: 当前会话的消息历史列表
               - conversation_summaries: 已归档的摘要列表
               - compression_jobs: 压缩任务字典（key=round_id）
               - compression_status: 压缩管线整体状态

    Returns:
        dict: 包含 "short_memory_stats" 键的字典，值为统计指标字典。
              由 LangGraph 自动 merge 到全局 state。
              示例：
              {
                  "short_memory_stats": {
                      "raw_message_count": 42,
                      "raw_round_count": 8,
                      "raw_tokens": 15420,
                      "summary_count": 12,
                      "summary_tokens": 2400,
                      "total_tokens": 17820,
                      "queued_jobs": 2,
                      "failed_jobs": 0,
                      "compression_status": "compressing",
                  }
              }
    """

    messages = list(state.get("messages", []))


    summaries = state.get("conversation_summaries", [])


    jobs = state.get("compression_jobs", {})







    raw_tokens = segmenter.count_tokens(messages)







    summary_tokens = sum(
        int(item.get("token_count", 0))
        for item in summaries
    )







    queued_jobs = sum(
        job.get("status") == "queued"
        for job in jobs.values()
    )



    failed_jobs = sum(
        job.get("status") == "failed"
        for job in jobs.values()
    )



    compression_status = state.get(
        "compression_status", "idle"
    )




    return {
        "short_memory_stats": {

            "raw_message_count": len(messages),
            "raw_round_count": len(
                segmenter.group_rounds(messages)
            ),
            "raw_tokens": raw_tokens,


            "summary_count": len(summaries),
            "summary_tokens": summary_tokens,


            "total_tokens": raw_tokens + summary_tokens,


            "queued_jobs": queued_jobs,
            "failed_jobs": failed_jobs,
            "compression_status": compression_status,
        }
    }
