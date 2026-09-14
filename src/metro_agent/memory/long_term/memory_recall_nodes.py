"""
记忆召回节点模块。

本模块负责从 Chroma 向量数据库中召回用户的长期记忆。
核心流程：
1. 根据用户输入文本中的关键词检测记忆类别（计划、目标、偏好等）
2. 从记忆存储中查询该类别下的活跃记忆
3. 将记忆格式化为自然语言返回给用户
"""

from metro_agent.state import MetroAgentState
from metro_agent.observability.node_instrumentation import traced_node
from metro_agent.memory.long_term.chroma_store import build_memory_store
from metro_agent.memory.long_term.memory_intent import detect_memory_category



_memory_store = None


def get_memory_store():
    global _memory_store

    if _memory_store is None:
        _memory_store = build_memory_store()

    return _memory_store

CATEGORY_NAMES = {
    "plan": "计划",
    "goal": "目标",
    "preference": "偏好",
    "identity": "身份信息",
    "requirement": "长期要求",
}

@traced_node("memory_recall")
def memory_recall_node(
    state: MetroAgentState,
) -> dict:
    """记忆召回节点 —— LangGraph 工作流中的核心节点之一。

    执行步骤：
    1. 检测用户输入中的记忆类别
    2. 从 Chroma 向量数据库中查询该用户在该类别下的所有活跃记忆
    3. 将查询结果格式化为自然语言回复

    Args:
        state: MetroAgent 的全局状态对象，包含 user_input、user_id 等关键字段。

    Returns:
        包含以下键的字典：
        - final_answer: 格式化后的自然语言回复，告知用户其已保存的记忆内容
        - recalled_memories: 原始记忆对象列表，供下游节点使用
    """

    category = detect_memory_category(
        state["user_input"]
    )


    memories = get_memory_store().list_active(
        user_id=state["user_id"],
        category=category,
    )


    category_name = CATEGORY_NAMES.get(
        category,
        "长期记忆",
    )


    if not memories:

        answer = f"我还没有保存你的{category_name}。"
    else:

        memory_text = "\n".join(
            f"{index}. {memory['content']}"
            for index, memory in enumerate(
                memories,
                start=1,
            )
        )
        answer = (
            f"我记得你的{category_name}：\n"
            f"{memory_text}"
        )

    return {
        "final_answer": answer,
        "recalled_memories": memories,
    }
