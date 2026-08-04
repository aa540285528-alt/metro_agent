"""
短期记忆系统 —— 对话上下文管理与异步压缩管线
============================================
管理单次会话内消息历史的完整生命周期，包括上下文构建、
token 预算分配、对话压缩（摘要生成）和状态裁剪。

核心管线：

  轮次开始:
    build_conversation_context → 追加 HumanMessage + 构建格式化上下文

  Agent 执行:
    AgentContextAssembler   → 按 Agent token 预算组装定制化上下文
    AgentContextBudgetManager → 决定 history/tool/rag 各分多少 token

  轮次结束:
    record_assistant_message → 追加 AIMessage 到消息历史

  异步压缩管线:
    detect_compression_jobs   → 检测已完成旧轮次，创建 CompressionJob
    enqueue_compression_jobs  → 入队 Redis 可靠队列
    compression_worker        → 独立进程异步消费，调 Ollama 生成摘要
    apply_compression_results → 读取摘要，写回 state，清理原消息

  容量控制:
    prune_short_memory       → 超出上限时删除最旧摘要
    collect_short_memory_stats → 运行时统计指标

子模块一览：

  上下文构建:
    context_node.py                — LangGraph 入口节点（轮次开始）
    context_builder.py             — 消息截断 + 格式化
    agent_context_assembler.py     — 按 Agent 预算组装上下文
    agent_context_budget_manager.py — Token 预算分配核心
    conversation_segmenter.py      — 按轮分组 + token 计数
    tool_context_builder.py        — 工具调用结果上下文

  响应记录:
    response_node.py               — AIMessage 写入消息历史

  压缩管线:
    compression_detection_node.py  — 检测待压缩轮次
    compression_queue_node.py      — 任务入队 Redis
    compression_queue.py           — Redis 可靠队列（BRPOPLPUSH）
    compression_worker.py          — 异步 Worker 进程
    compression_result_node.py     — 结果应用 + 原消息清理
    round_summarizer.py            — 单轮摘要器（Ollama）

  运维:
    state_memory_pruner.py         — 超出容量时裁剪旧摘要
    memory_observer_node.py        — 运行时可观测性统计
    redis_checkpointer.py          — Redis Checkpoint 持久化
"""

# ============================================================
# 上下文构建
# ============================================================
from metro_agent.memory.short_term.context_node import (
    build_conversation_context,
    get_next_round_number,
)
from metro_agent.memory.short_term.context_builder import ConversationContextBuilder
from metro_agent.memory.short_term.agent_context_assembler import AgentContextAssembler
from metro_agent.memory.short_term.agent_context_budget_manager import AgentContextBudgetManager
from metro_agent.memory.short_term.conversation_segmenter import ConversationSegmenter
from metro_agent.memory.short_term.tool_context_builder import ToolContextBuilder

# ============================================================
# 响应记录
# ============================================================
from metro_agent.memory.short_term.response_node import record_assistant_message

# ============================================================
# 压缩管线 — 节点
# ============================================================
from metro_agent.memory.short_term.compression_detection_node import detect_compression_jobs
from metro_agent.memory.short_term.compression_queue_node import enqueue_compression_jobs
from metro_agent.memory.short_term.compression_result_node import apply_compression_results

# ============================================================
# 压缩管线 — 基础设施
# ============================================================
from metro_agent.memory.short_term.compression_queue import (
    RedisCompressionQueue,
    ReservedCompressionJob,
)
from metro_agent.memory.short_term.compression_worker import run_worker
from metro_agent.memory.short_term.round_summarizer import RoundConversationSummarizer

# ============================================================
# 运维
# ============================================================
from metro_agent.memory.short_term.state_memory_pruner import prune_short_memory
from metro_agent.memory.short_term.memory_observer_node import collect_short_memory_stats
from metro_agent.memory.short_term.redis_checkpointer import build_redis_checkpointer

# ============================================================
# 公开 API
# ============================================================
__all__ = [
    # —— 上下文构建 ——
    "build_conversation_context",
    "get_next_round_number",
    "ConversationContextBuilder",
    "AgentContextAssembler",
    "AgentContextBudgetManager",
    "ConversationSegmenter",
    "ToolContextBuilder",
    # —— 响应记录 ——
    "record_assistant_message",
    # —— 压缩管线节点 ——
    "detect_compression_jobs",
    "enqueue_compression_jobs",
    "apply_compression_results",
    # —— 压缩管线基础设施 ——
    "RedisCompressionQueue",
    "ReservedCompressionJob",
    "run_worker",
    "RoundConversationSummarizer",
    # —— 运维 ——
    "prune_short_memory",
    "collect_short_memory_stats",
    "build_redis_checkpointer",
]
