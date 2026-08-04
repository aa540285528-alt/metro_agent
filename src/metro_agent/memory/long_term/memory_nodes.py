"""
记忆系统 — LangGraph 节点
========================
将 MemoryService 封装为 LangGraph 工作流节点，在 Agent 每轮回复后自动执行记忆处理。

工作流程：
  1. 从 state 中提取 user_id、user_input、final_answer
  2. 调用 MemoryService.process_turn 执行完整记忆流水线
  3. 将处理结果（已保存 / 待确认 / 已忽略）汇总为通知文本
  4. 通知追加到 final_answer 末尾（以 [memory] 标记），供用户感知
  5. 写回 state：memory_saved / pending_memories / memory_notifications 等字段

在 graph 中的位置：通常放在 agent 回复之后、最终输出之前。
"""

import sys
from pathlib import Path

# 确保能导入项目根目录的模块
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from metro_agent.memory.long_term.memory_service import MemoryService
from metro_agent.state import MetroAgentState


# ============================================================
# 全局 MemoryService 实例（单例）
# ============================================================
_memory_service: MemoryService | None = None


def get_memory_service() -> MemoryService:
    global _memory_service

    if _memory_service is None:
        _memory_service = MemoryService()

    return _memory_service


# ============================================================
# LangGraph 节点：记忆策展节点
# ============================================================
def memory_curator_node(state: MetroAgentState) -> dict:
    """
    记忆策展节点 —— 从本轮对话中提取并处理长期记忆。

    输入（从 state 读取）：
      - user_id      → user_id（用于用户隔离）
      - user_input   → user_text（用户本轮原始输入）
      - final_answer → assistant_text（助手最终回复，作为提取记忆的上下文）

    输出（写回 state）：
      - memory_saved           → 已写入 ChromaDB 的记忆序列化列表
      - pending_memories       → 待用户确认的记忆序列化列表
      - memory_notifications   → 用户可见的通知文本列表
      - rule_change_requested  → 是否检测到规则修改请求
      - final_answer           → 原 answer 拼接 [memory] 通知块

    返回 dict，由 LangGraph 自动 merge 到 state。
    """
    # ---- 调用 MemoryService 处理本轮对话 ----
    result = get_memory_service().process_turn(
    user_id=state["user_id"],
    user_text=state["user_input"],
    assistant_text=state["final_answer"],
    )

    # ---- 组装用户通知 ----
    notifications = []

    # 成功保存：告知用户已记住的内容
    if result.saved:
        contents = ";".join(
            memory.content for memory in result.saved
        )
        notifications.append(f"已记住：{contents}")

    # 待确认：提醒用户有记忆需要确认
    if result.pending:
        notifications.append(
            f"发现 {len(result.pending)} 条长期记忆需要确认，"
            "目前尚未保存。"
        )

    # 规则修改：告知用户规则修改请求已被捕获
    if result.rule_change_requested:
        notifications.append(
            "已请求更改规则，请确认是否同意。"
        )

    # ---- 追加 [memory] 标记到最终回复 ----
    final_answer = state["final_answer"]
    if notifications:
        memory_text = "\n".join(notifications)
        final_answer = (
            f"{final_answer}\n\n"
            f"[memory]\n{memory_text}"
        )

    # ---- 写回 state ----
    return {
        "memory_saved": [
            memory.model_dump(mode="json")
            for memory in result.saved
        ],
        "pending_memories": [
            pending.model_dump(mode="json")
            for pending in result.pending
        ],
        "memory_notifications": notifications,
        "rule_change_requested": result.rule_change_requested,
        "final_answer": final_answer,
        "memory_ignored": result.ignored
    }
