"""
短期记忆系统 — 助手回复记录节点
===============================
在每轮对话结束时，将助手的回复内容包装为 AIMessage，
追加到 state["messages"] 历史中，供下一轮 context_node 构建上下文时使用。

内容优先级：
  1. 优先使用 agents_output 中各 agent 的输出（过滤掉 sources 等元数据）
  2. 如果 agents_output 无有效内容，回退使用 final_answer

这样存入短时记忆的内容是 agent 实际产生的回复，而非经过多层包装后的
最终展示文本，更利于后续轮次的语义理解和上下文关联。

与 context_node.py 的配合：
  - context_node：在轮次开始时追加 HumanMessage（用户输入）
  - response_node：在轮次结束时追加 AIMessage（助手回复）
  两者交替写入 state["messages"]，构成完整的一轮对话记录。
"""

from langchain_core.messages import AIMessage
import sys
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from metro_agent.state import MetroAgentState
from metro_agent.observability.node_instrumentation import traced_node


@traced_node("record_assistant_message")
def record_assistant_message(
    state: MetroAgentState,
) -> dict:
    """将助手回复写入当前会话消息历史。

    在 LangGraph 工作流中的位置：
      应放在 trace_finalize_node 之后、短期记忆压缩管线之前，将最终回答
      写入会话历史，供后续压缩与下一轮上下文构建使用。

    内容提取逻辑：
      1. 遍历 agents_output，过滤掉 sources/sources_score 等元数据键
      2. 将各 agent 的有效输出用空行拼接
      3. 若 agents_output 无有效内容，降级使用 final_answer
      4. 两者均为空则跳过，不写入任何消息

    Args:
        state: MetroAgent 全局状态，需包含 agents_output 和 final_answer。

    Returns:
        包含 messages 键的字典，LangGraph 的 add_messages reducer 会将
        AIMessage 追加到 state["messages"] 列表末尾。
        如果无有效内容，返回空字典。
    """
    outputs = state.get("agents_output", {})
    round_id = state.get("current_round_id")
    round_number = state.get("current_round_number")

    if not round_id or not round_number:
        raise ValueError("当前对话缺少round_id或round_number")


    history_parts = [
        str(output).strip()
        for name, output in outputs.items()
        if (
            name not in {"sources", "sources_score"}
            and str(output).strip()
        )
    ]


    assistant_content = "\n\n".join(history_parts)


    if not assistant_content:
        assistant_content = state.get(
            "final_answer",
            "",
        ).strip()


    if not assistant_content:
        return {}

    return {
    "messages": [
        AIMessage(
            content=assistant_content,
            id=f"{round_id}:assistant",
            additional_kwargs={
                "round_id": round_id,
                "round_number": round_number,
            },
        )
    ]
}
