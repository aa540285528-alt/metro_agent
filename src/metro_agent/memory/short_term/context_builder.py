"""
短期记忆系统 — 对话上下文构建器
===============================
负责从当前会话的消息历史中选择最近的 N 轮对话，并格式化为纯文本，
供 agent 作为上下文窗口使用。

与长期记忆系统（long_memory_system）的区别：
  - 短期记忆：当前会话内的最近消息，会话结束即丢弃
  - 长期记忆：跨会话持久化的用户信息，存入 ChromaDB 向量库
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from collections.abc import Sequence
from metro_agent.config import SHORT_TERM_MEMORY_LIMIT
from langchain_core.messages import BaseMessage


class ConversationContextBuilder:
    """选择并格式化当前会话的最近消息。

    职责：
      1. 按轮次截断消息历史（保留最近 max_rounds 轮）
      2. 将消息列表格式化为 "角色:内容" 的纯文本

    一轮对话 = 一条 human 消息 + 对应的 ai 回复。
    截断以 human 消息为锚点，确保不会从半轮对话中间截断。

    使用方式：
        builder = ConversationContextBuilder(max_rounds=6)
        context_text = builder.build_text(messages)
    """

    def __init__(self, max_rounds: int = SHORT_TERM_MEMORY_LIMIT):
        """初始化上下文构建器。

        Args:
            max_rounds: 保留的最大对话轮数，默认 6 轮。
                        必须 >= 1，否则抛出 ValueError。
        """
        if max_rounds < 1:
            raise ValueError("max_rounds必须大于0")
        self.max_rounds = max_rounds

    def select_messages(
        self,
        messages: Sequence[BaseMessage],
    ) -> list[BaseMessage]:
        """从消息历史中截取最近的 N 轮对话。

        截断逻辑：
          1. 找出所有 human 类型消息的索引位置（以 human 为轮次锚点）
          2. 如果 human 消息数 <= max_rounds，返回全部消息
          3. 否则取最后 max_rounds 条 human 消息，从最早的那条 human
             开始截断，丢弃更早的消息

        这样保证不会从 assistant/tool 消息中间截断，LLM 看到的上下文
        始终是完整的对话轮次。

        Args:
            messages: 当前会话的完整消息序列。

        Returns:
            截断后的消息列表，保留最近 max_rounds 轮。
        """
        message_list = list(messages)

        if not message_list:
            return []


        human_indexes = [
            index
            for index, message in enumerate(message_list)
            if message.type == "human"
        ]


        if len(human_indexes) <= self.max_rounds:
            return message_list


        start_index = human_indexes[-self.max_rounds]
        return message_list[start_index:]

    def format_messages(
        self,
        messages: Sequence[BaseMessage],
    ) -> str:
        """将消息列表格式化为 "角色:内容" 的纯文本。

        角色映射：
          human   → 用户
          ai      → 助手
          tool    → 工具
          system  → 系统
          其他    → 保持原始 type 值

        Args:
            messages: 待格式化的消息序列（通常已经过 select_messages 截断）。

        Returns:
            格式化后的纯文本，每条消息一行。
        """
        role_names = {
            "human": "用户",
            "ai": "助手",
            "tool": "工具",
            "system": "系统",
        }

        return "\n".join(
            f"{role_names.get(message.type, message.type)}:{message.content}"
            for message in messages
        )

    def build_text(
        self,
        messages: Sequence[BaseMessage],
    ) -> str:
        """一站式方法：截断 + 格式化。

        先调用 select_messages 截取最近 N 轮，再调用 format_messages
        转为纯文本。这是对外暴露的主要接口。

        Args:
            messages: 当前会话的完整消息序列。

        Returns:
            截断并格式化后的上下文字符串，可直接拼入 LLM prompt。
        """
        return self.format_messages(
            self.select_messages(messages)
        )