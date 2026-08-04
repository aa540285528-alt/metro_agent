from langchain_core.messages import SystemMessage, HumanMessage

from metro_agent.config import build_Chat_QwenLLM, GENERAL_AGENT_PROMPT
from metro_agent.memory.short_term.agent_context_assembler import AgentContextAssembler
from metro_agent.state import MetroAgentState
from metro_agent.observability.agent_usage import LLM_USAGE_RECORDS_KEY, capture_response_usages
from metro_agent.safety_policy import safety_refusal_response
# from metro_agent.skills.skill_runtime import build_skill_context

_general_agent_llm = None


def product_help_response(user_input: str) -> str | None:
    """Answer stable product-help questions without inventing interface behavior."""
    normalized = user_input.strip()
    if normalized.casefold() in {"你好", "您好", "嗨", "hello", "hi"}:
        return "你好，我是地铁通信智能运维助手。"
    if any(phrase in normalized for phrase in ("你能做什么", "你可以做什么", "有什么功能")):
        return "我可以查询实时告警、基于知识库答疑、提供故障诊断建议，并支持普通咨询；涉及危险操作或敏感信息会明确拒绝。"
    if any(phrase in normalized for phrase in ("历史对话", "历史会话", "查看历史")):
        return "在页面左侧边栏选择历史会话即可查看；支持新建、重命名、置顶和彻底删除会话。"
    return None


def get_general_agent_llm():
    global _general_agent_llm

    if _general_agent_llm is None:
        _general_agent_llm = build_Chat_QwenLLM()

    return _general_agent_llm

general_context_assembler = AgentContextAssembler(
    agent_name="general"
)

# skill_context = build_skill_context(
#     llm=general_agent_LLM,
#     agent_name="general",
#     user_input=state["user_input"],
# )

def general_agent(state: MetroAgentState) -> dict:
    safety_response = safety_refusal_response(state["user_input"])
    if safety_response is not None:
        return {
            "agents_output": {"general": safety_response},
            "context_allocations": {"general": {}},
            LLM_USAGE_RECORDS_KEY: [],
        }

    product_response = product_help_response(state["user_input"])
    if product_response is not None:
        return {
            "agents_output": {"general": product_response},
            "context_allocations": {"general": {}},
            LLM_USAGE_RECORDS_KEY: [],
        }

    assembly = general_context_assembler.build(
        state=state,
        system_prompt=GENERAL_AGENT_PROMPT,
        requested={},
        mode="standard",
    )

    history_messages = assembly["messages"]

    if not history_messages:
        history_messages = [
            HumanMessage(content=state["user_input"])
        ]
    
    messages = [
        SystemMessage(content=GENERAL_AGENT_PROMPT),
        *history_messages,
    ]
    # if skill_context:
    #     messages.append(
    #     SystemMessage(
    #         content=f"以下是本轮需要遵守的 Skill 上下文：\n\n{skill_context}"
    #     )
    # )

    response = get_general_agent_llm().invoke(messages)

    return {
        "agents_output": {
            "general": response.content,
        },
        "context_allocations": {
            "general": assembly["allocation"],
        },
        LLM_USAGE_RECORDS_KEY: capture_response_usages(
            response,
            fallback_model="qwen3.6-flash",
            call_name="general.generate",
        ),
    }
