"""
ToolContextBuilder —— 工具调用结果上下文构建器
==============================================
从"中心黑板"（tool_results 字典）中提取最近的工具调用记录，
截断并格式化为 agnet可消费的文本上下文。

设计动机：
  在多 Agent 协作系统中，各个 Agent 共享同一个 tool_results 字典
  （即"中心黑板"），记录所有工具调用的参数和返回值。当某个 Agent
  需要参考之前的工具调用结果时，不能把所有记录都塞给 LLM，因为：
    1. 历史记录可能非常多，超出 LLM 的 token 限制
    2. 单条结果可能非常长（如大批量告警数据），浪费上下文窗口

本模块的职责：
  - 数量控制：只取最近 N 条记录（max_items）
  - 长度控制：单条结果超过上限则截断（max_result_chars）
  - 格式化：将结构化记录转为 LLM 友好的文本格式

使用方式：
    builder = ToolContextBuilder(max_items=5, max_result_chars=2000)
    context_text = builder.build_text(tool_results)  # → 可直接嵌入 prompt 的字符串
"""

import json  # 将 Python 对象序列化为 JSON 字符串，便于 LLM 解析
from typing import Any  # 工具参数和结果的类型可以是任意 JSON 可序列化的值

from metro_agent.config import (
    TOOL_CONTEXT_MAX_ITEMS,          # 最多保留多少条工具调用记录
    TOOL_CONTEXT_MAX_RESULT_CHARS,   # 单条工具返回结果的最大字符数
)


class ToolContextBuilder:
    """从中心黑板构建有限的工具结果上下文。

    该类是一个无状态的格式化器，核心逻辑为三步流水线：
      select_latest  →  取最近 N 条记录
      format_records →  逐条截断并格式化
      build_text     →  串联上述两步，返回最终文本
    """

    def __init__(
        self,
        max_items: int = TOOL_CONTEXT_MAX_ITEMS,
        max_result_chars: int = TOOL_CONTEXT_MAX_RESULT_CHARS,
    ):
        """初始化构建器。

        Args:
            max_items: 最多保留的工具调用记录条数。
                       只取 tool_results 字典中最后 max_items 条，
                       避免上下文过长。
            max_result_chars: 单条工具返回结果的最大字符数。
                              超过此长度的结果会被截断，并在末尾追加截断提示。

        Raises:
            ValueError: 当 max_items < 1 时抛出，至少需要保留 1 条记录才有意义。
        """
        if max_items < 1:
            raise ValueError("max_items必须大于0")

        self.max_items = max_items
        self.max_result_chars = max_result_chars

    # ------------------------------------------------------------------
    # 步骤 1：选取最近的工具调用记录
    # ------------------------------------------------------------------

    def select_latest(
        self,
        tool_results: dict[str, dict[str, Any]],
    ) -> list[tuple[str, dict[str, Any]]]:
        """从工具结果字典中选取最近的 max_items 条记录。

        由于 Python 3.7+ 的 dict 保持插入顺序，而 tool_results 中的
        记录是按调用时间先后插入的，因此直接取尾部即为"最近"的记录。

        Args:
            tool_results: 全局工具调用记录字典。
                          key   = tool_call_id（每次调用的唯一标识）
                          value = {"tool_name": ..., "arguments": ..., "result": ...}

        Returns:
            (tool_call_id, record) 元组列表，最多 max_items 条，按时间升序排列。
        """
        items = list(tool_results.items())
        return items[-self.max_items:]  # 取列表末尾 N 条（最新的记录）

    # ------------------------------------------------------------------
    # 步骤 2：格式化记录为文本
    # ------------------------------------------------------------------

    def format_records(
        self,
        records: list[
            tuple[str, dict[str, Any]]
        ],
    ) -> str:
        """将选中的工具调用记录格式化为 LLM 可读的文本块。

        每条记录包含四个字段：
          - 调用ID：工具调用的唯一标识
          - 工具：工具函数名称
          - 参数：调用时传入的参数（JSON 格式）
          - 结果：工具返回的数据（JSON 格式，超长则截断）

        Args:
            records: select_latest 返回的记录列表。

        Returns:
            格式化后的文本，多条记录之间用双换行分隔。
            可直接作为 SystemMessage 或 HumanMessage 的一部分嵌入 prompt。
        """
        sections = []

        for tool_call_id, record in records:
            # 序列化参数 —— 使用 default=str 处理无法直接序列化的对象
            arguments = json.dumps(
                record.get("arguments", {}),
                ensure_ascii=False,  # 保留中文，不转义为 \uXXXX
                default=str,         # 遇到不可序列化类型时降级为 str()
            )

            # 序列化结果
            result = json.dumps(
                record.get("result"),
                ensure_ascii=False,
                default=str,
            )

            # 截断过长的结果，避免单条数据撑爆 LLM 上下文窗口
            if len(result) > self.max_result_chars:
                result = (
                    result[:self.max_result_chars]
                    + "...（已截断）"
                )

            # 组装单条记录的文本块
            sections.append(
                "\n".join([
                    f"调用ID：{tool_call_id}",
                    f"工具：{record.get('tool_name', '')}",
                    f"参数：{arguments}",
                    f"结果：{result}",
                ])
            )

        return "\n\n".join(sections)  # 记录之间用空行分隔，便于 LLM 区分

    # ------------------------------------------------------------------
    # 步骤 3：一站式构建文本上下文
    # ------------------------------------------------------------------

    def build_text(
        self,
        tool_results: dict[str, dict[str, Any]],
    ) -> str:
        """选取最近的工具调用记录并格式化为文本（select + format 的便捷入口）。

        这是该类的主要对外接口，一步完成选取和格式化。

        Args:
            tool_results: 全局工具调用记录字典（同 select_latest 的参数）。

        Returns:
            可直接嵌入 LLM prompt 的上下文字符串。
            若 tool_results 为空，返回空字符串。
        """
        records = self.select_latest(tool_results)
        return self.format_records(records)