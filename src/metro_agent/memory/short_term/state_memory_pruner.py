"""
StateMemoryPruner —— 短期记忆状态裁剪器
======================================
当对话摘要（conversation_summaries）的累积数量或 token 总量
超出预设上限时，从最旧的摘要开始删除，释放 state 空间。

这是短期记忆系统的"垃圾回收"机制——随着对话不断进行，
旧轮次被压缩为摘要后存放在 conversation_summaries 中。
如果不加限制，摘要列表会无限增长，导致：
  1. state 体积膨胀（LangGraph checkpoint 每次都会序列化整个 state）
  2. 上下文组装时候选摘要过多，影响预算分配的效率
  3. Redis checkpoint 存储压力（每个 checkpoint 包含完整 state）

裁剪策略：
  - 只删除摘要（conversation_summaries），不删原始消息（messages）
    （原始消息由压缩管线在应用结果时清理，分工明确）
  - 从最旧的摘要开始删（FIFO：按 round_number 升序弹出第一个）
  - 同步清理对应的 compression_jobs（避免残留无主任务）

触发条件（任一满足即触发）：
  1. 总轮数 > SHORT_MEMORY_MAX_TOTAL_ROUNDS（默认 70）
     总轮数 = 原始消息轮数 + 摘要数量
  2. 总 token > SHORT_MEMORY_MAX_TOTAL_TOKENS（默认 100,000）
     总 token = 原始消息 token + 摘要 token

在 LangGraph 工作流中的位置：
  apply_compression_results → prune_short_memory（本节点）
    → build_conversation_context → ...
  放在压缩结果应用之后、新轮上下文构建之前，
  确保每轮开始时 state 容量在可控范围内。
"""

import sys
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from langgraph.types import Overwrite

from metro_agent.config import (
    SHORT_MEMORY_MAX_TOTAL_ROUNDS,
    SHORT_MEMORY_MAX_TOTAL_TOKENS,
)
from metro_agent.memory.short_term.conversation_segmenter import ConversationSegmenter
from metro_agent.observability.node_instrumentation import traced_node
from metro_agent.state import MetroAgentState



segmenter = ConversationSegmenter()


@traced_node("prune_short_memory")
def prune_short_memory(state: MetroAgentState) -> dict:
    """检查短期记忆容量，超出上限时从最旧摘要开始裁剪。

    执行流程（4 步）：

      步骤 1 —— 统计当前容量
        计算 raw_rounds（messages 轮数）、raw_tokens（消息 token）、
        summary_tokens（摘要 token），得到 total_rounds 和 total_tokens。

      步骤 2 —— 容量检查 + 裁剪循环
        while 循环：只要 total_rounds 或 total_tokens 超限且
        summaries 非空，就从列表头部（最旧的）弹出一个摘要。
        同步更新计数器和 removed_ids。

      步骤 3 —— 短路返回
        如果本轮没有删除任何摘要（容量未超限），返回空 dict，
        不触发 state 写入（减少不必要的 checkpoint 持久化）。

      步骤 4 —— 清理 compression_jobs
        将已删除摘要对应的 compression_jobs 也一并移除，
        避免残留"无主"的任务记录占用 state 空间。

    为什么不需要裁剪原始消息：
      messages 已经受 context_builder（6 轮截断）和压缩管线
      （旧消息被移除后用摘要替代）的双重控制，其增长有限。
      真正的膨胀来源是 summaries——每轮旧对话都产生一条摘要，
      100 轮对话 = 100 条摘要。

    Args:
        state: MetroAgent 全局状态，需包含：
               - messages: 消息历史
               - conversation_summaries: 摘要列表
               - compression_jobs: 压缩任务字典

    Returns:
        dict: 包含以下键（无裁剪时返回空字典）：
          - conversation_summaries: Overwrite 覆盖（裁剪后的列表）
          - compression_jobs:       Overwrite 覆盖（清理后的字典）
    """





    messages = list(state.get("messages", []))


    summaries = sorted(
        state.get("conversation_summaries", []),
        key=lambda item: item.get("round_number", 0),
    )


    raw_rounds = segmenter.group_rounds(messages)
    raw_tokens = segmenter.count_tokens(messages)



    summary_tokens = sum(
        int(item.get("token_count", 0))
        for item in summaries
    )


    total_rounds = len(raw_rounds) + len(summaries)
    total_tokens = raw_tokens + summary_tokens
















    removed_ids = set()

    while summaries and (
        total_rounds > SHORT_MEMORY_MAX_TOTAL_ROUNDS
        or total_tokens > SHORT_MEMORY_MAX_TOTAL_TOKENS
    ):

        removed = summaries.pop(0)


        removed_ids.add(removed["round_id"])


        total_rounds -= 1
        total_tokens -= int(
            removed.get("token_count", 0)
        )








    if not removed_ids:
        return {}











    jobs = {
        round_id: job
        for round_id, job in state.get(
            "compression_jobs", {}
        ).items()
        if round_id not in removed_ids
    }










    return {
        "conversation_summaries": Overwrite(
            value=summaries
        ),
        "compression_jobs": Overwrite(
            value=jobs
        ),
    }
