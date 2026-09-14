"""
短期记忆系统 — LangGraph 上下文节点
===================================
在每轮对话开始时，将用户新消息追加到会话历史，分配轮次 ID 和序号，
构建格式化后的对话上下文文本，注入到 state 中供下游 Agent 使用。

这是 LangGraph 工作流的入口节点，每次用户输入后最先执行。

与 context_builder.py 的关系：
  - context_builder.py：底层工具类，负责截断 + 格式化消息列表
  - context_node.py：LangGraph 节点，负责编排逻辑 —— 从 state 读取历史、
    生成轮次元数据、包装 HumanMessage、调用 builder、写回 state

数据流：
  state (messages + user_input)
    → get_next_round_number() 计算轮次序号
    → 生成 round_id (UUID) 和 HumanMessage
    → context_builder.build_text() 截断 + 格式化
    → 写回 state (messages / conversation_context / current_round_* / agents_output)
"""

from uuid import uuid4

from langchain_core.messages import HumanMessage
from langgraph.types import Overwrite
from metro_agent.observability.node_instrumentation import traced_node
from metro_agent.memory.short_term.context_builder import ConversationContextBuilder
from metro_agent.state import MetroAgentState






def get_next_round_number(
    state: MetroAgentState,
) -> int:
    """计算本轮对话的序号（单调递增，从 1 开始）。

    轮次序号来源有两个，取最大值后 +1：
      1. conversation_summaries 中已归档轮次的 round_number
      2. messages 中各消息 additional_kwargs 中的 round_number
         （尚未归档的近期轮次）

    这样即使归档和未归档的消息中存在交叉，也能生成正确的自增序号，
    不会产生重复或回退。

    Args:
        state: 当前全局状态。

    Returns:
        本轮对话的序号（int），等于已有最大序号 + 1。
        若当前没有任何历史轮次，返回 1。
    """
    round_numbers = [
        summary.get("round_number", 0)
        for summary in state.get("conversation_summaries", [])
    ]


    for message in state.get("messages", []):
        round_number = message.additional_kwargs.get(
            "round_number"
        )

        if isinstance(round_number, int):
            round_numbers.append(round_number)

    return max(round_numbers, default=0) + 1







context_builder = ConversationContextBuilder(max_rounds=6)


@traced_node("build_conversation_context")
def build_conversation_context(
    state: MetroAgentState,
) -> dict:
    """构建当前轮的对话上下文，注入到 state。

    这是 LangGraph 工作流中每轮对话的第一个处理节点。
    每次用户输入后由框架自动调用。

    执行步骤：
      1. 计算本轮轮次序号（get_next_round_number）
      2. 生成本轮唯一 round_id（UUID）
      3. 将 user_input 包装为带轮次元数据的 HumanMessage
      4. 调用 ConversationContextBuilder 截断最近 N 轮并格式化为纯文本
      5. 将本轮 HumanMessage 追加到 state["messages"]
      6. 将格式化后的上下文文本写入 state["conversation_context"]
      7. 暴露 current_round_id / current_round_number 供下游节点引用
      8. 重置 agents_output 为空（开始新一轮 Agent 调度）

    边界情况：
      - user_input 为空（如仅触发记忆检查）：不追加新消息，
        仅基于已有历史消息构建上下文，不分配新的 round_id

    Args:
        state: MetroAgent 全局状态，需包含：
               - messages: 当前会话的历史消息列表（可为空）
               - user_input: 用户本轮输入

    Returns:
        dict: 包含以下键的字典，LangGraph 自动 merge 到全局 state：
          - messages:              本次追加的 HumanMessage 列表
          - conversation_context:  格式化后的上下文字符串，可直接拼入 LLM prompt
          - current_round_id:      本轮 UUID（供压缩/归档/日志引用）
          - current_round_number:  本轮序号（供摘要排序和状态追踪）
          - agents_output:         Overwrite 置空，重置上一轮的 Agent 输出
    """
    user_input = state.get("user_input", "").strip()
    existing_messages = list(state.get("messages", []))


    if not user_input:
        return {
            "conversation_context":
                context_builder.build_text(existing_messages)
        }


    round_id = str(uuid4())
    round_number = get_next_round_number(state)





    current_message = HumanMessage(
        content=user_input,
        id=f"{round_id}:human",
        additional_kwargs={
            "round_id": round_id,
            "round_number": round_number,
        },
    )


    context_text = context_builder.build_text(
        [*existing_messages, current_message]
    )

    return {
        "messages": [current_message],
        "conversation_context": context_text,

        "current_round_id": round_id,
        "current_round_number": round_number,

        "agents_output": Overwrite(value={}),
        "context_allocations": Overwrite(value={}),
    }
