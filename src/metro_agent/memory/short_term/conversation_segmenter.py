"""
ConversationSegmenter —— 对话分段器（基于 Token 预算）
====================================================
将长对话的消息历史按轮次切分为三段，用精确的 token 计数做预算控制，
代替简单的"阈值触发"方案，使上下文管理更精确、更抗膨胀。


  - 引入 tiktoken（cl100k_base 编码）做精确 token 计数
  - 输出三段而非两段：evicted（丢弃） / archived（归档） / recent（保留）
  - archived 段同时在轮数和 token 两个维度上受限
  - 从旧→新方向贪婪选择，确保较新的消息优先进入归档

三段模型：
  ┌──────────────┬──────────────────┬──────────────────┐
  │   evicted    │    archived      │     recent       │
  │  （直接丢弃） │ （压缩为摘要后保留）│  （完整保留原文）  │
  └──────────────┴──────────────────┴──────────────────┘
   最旧 ←──────────────────────────────────────────→ 最新
"""

from collections.abc import Sequence  # 消息序列的抽象类型
import tiktoken                        # OpenAI 官方 token 计数库，精确计算文本 token 数
from langchain_core.messages import BaseMessage



class ConversationSegmenter:
    def __init__(self):
        self.encoding = tiktoken.get_encoding(
            "cl100k_base"
        )
        self.encoding = tiktoken.get_encoding("cl100k_base")

    # ------------------------------------------------------------------
    # Token 计数
    # ------------------------------------------------------------------

    def count_tokens(
        self,
        messages: Sequence[BaseMessage],
    ) -> int:
        """计算消息列表的精确 token 数。

        计数方式模仿 OpenAI ChatCompletion API 的消息格式规则：
          每条消息的基础开销 = 4 token（角色标记等元数据）
          文本内容 = encoding.encode() 的 token 数
          总 token = Σ(4 + encode(content))

        注意：这是近似值，不完全等同于 API 实际计费 token（API 还会
        加上 messages 数组的结构开销），但用于上下文预算控制已足够精确。

        Args:
            messages: 待计数的消息列表。

        Returns:
            估算的 token 总数。
        """
        total = 0

        for message in messages:
            content = str(message.content)
            total += 4  # 每条消息的元数据开销（角色标记等）
            total += len(self.encoding.encode(content))

        return total

    # ------------------------------------------------------------------
    # 按轮分组
    # ------------------------------------------------------------------

    def group_rounds(
        self,
        messages: Sequence[BaseMessage],
    ) -> list[list[BaseMessage]]:
        """将消息列表按 HumanMessage 为锚点分组为"轮"。

        分组规则：
          - 每个 HumanMessage 开启新的一轮
          - HumanMessage 之后的所有非 HumanMessage（AI/Tool/System）
            归入同一轮，直到遇到下一个 HumanMessage 为止
          - 如果消息列表不以 HumanMessage 开头（如只有 system prompt），
            这些消息单独成为第一轮

        示例：
          输入: [System, Human1, AI1, Tool1, Human2, AI2]
          输出: [[System], [Human1, AI1, Tool1], [Human2, AI2]]

        Args:
            messages: 完整的消息序列。

        Returns:
            二维列表，每个子列表代表一轮对话。
        """
        message_list = list(messages)

        # 找到所有 HumanMessage 的索引位置（轮次边界）
        human_indexes = [
            index
            for index, message in enumerate(message_list)
            if message.type == "human"
        ]

        # 没有任何 HumanMessage（例如只有 system prompt），整段作为一轮
        if not human_indexes:
            return [message_list] if message_list else []

        rounds = []

        for position, human_index in enumerate(human_indexes):
            # 每轮的起始索引：
            #   - 第一轮从索引 0 开始（可能包含 HumanMessage 之前的 system 消息）
            #   - 后续轮从当前 HumanMessage 的位置开始
            start = 0 if position == 0 else human_index

            # 每轮的结束索引：
            #   - 如果后面还有 HumanMessage，到下一个 HumanMessage 位置为止
            #   - 否则到消息列表末尾
            if position + 1 < len(human_indexes):
                end = human_indexes[position + 1]
            else:
                end = len(message_list)

            rounds.append(message_list[start:end])

        return rounds


    # ------------------------------------------------------------------
    # 工具方法
    # ------------------------------------------------------------------

    @staticmethod
    def flatten(
        rounds: Sequence[Sequence[BaseMessage]],
    ) -> list[BaseMessage]:
        """将嵌套的轮次列表展平为一维消息列表。

        [[H1, A1], [H2, A2]] → [H1, A1, H2, A2]

        Args:
            rounds: 二维消息列表（每轮一个子列表）。

        Returns:
            展平后的一维消息列表，保持原始时间顺序。
        """
        return [
            message
            for round_messages in rounds
            for message in round_messages
        ]