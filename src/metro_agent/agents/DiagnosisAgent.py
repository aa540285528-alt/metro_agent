"""
Diagnosis Agent —— 故障诊断 Agent
==================================
负责分析地铁通信系统故障现象，推断可能原因并给出排查建议。
使用智谱 GLM-4.6V 模型，强调推理能力和结构化输出。

与 RealtimeAgent 的区别：
  - RealtimeAgent：轻量、单工具调用，使用本地 Qwen2.5 7B
  - DiagnosisAgent：重推理、多信息源融合，使用云端 GLM-4.6V（24K 上下文）
  - DiagnosisAgent 需要综合 tool_results（设备/告警证据）和
    knowledge_context（KnowledgeAgent 的知识检索结果）来诊断

上下文预算模型（通过 AgentContextBudgetManager.allocate 实现）：
  input_limit = 24000（diagnosis profile 的模型上下文上限）
  固定消耗 = system_prompt token 数
  可变消耗 = history（优先） + tool（按需） + rag（按需）

  模式切换：
    standard:  没有 tool/rag 内容时，历史占 60% 可变空间
    extended:  有 tool/rag 内容时，历史可占满可变空间（100%），
               tool/rag 按 requested 额度从剩余空间中竞争分配
"""

from langchain.messages import SystemMessage, HumanMessage

from metro_agent.config import (
    build_Chat_ZhiPuLLM_DIAGNOSIS,
    DIAGNOSIS_AGENT_PROMPT,
    TOOL_CONTEXT_MAX_ITEMS,
    TOOL_CONTEXT_MAX_RESULT_CHARS,
)
from metro_agent.memory.short_term.agent_context_assembler import (
    AgentContextAssembler,
)
from metro_agent.memory.short_term.tool_context_builder import (
    ToolContextBuilder,
)
from metro_agent.state import MetroAgentState
from metro_agent.observability.agent_usage import LLM_USAGE_RECORDS_KEY, capture_response_usages





_diagnosis_agent_llm = None


def get_diagnosis_agent_llm():
    global _diagnosis_agent_llm

    if _diagnosis_agent_llm is None:
        _diagnosis_agent_llm = build_Chat_ZhiPuLLM_DIAGNOSIS()

    return _diagnosis_agent_llm


diagnosis_context_assembler = AgentContextAssembler(
    agent_name="diagnosis"
)



diagnosis_tool_context_builder = ToolContextBuilder(
    max_items=TOOL_CONTEXT_MAX_ITEMS,
    max_result_chars=TOOL_CONTEXT_MAX_RESULT_CHARS,
)
def format_dependency_outputs(dependency_outputs: dict) -> str:
    lines = []
    for step_id, result in dependency_outputs.items():
        lines.append(f"【{step_id} | {result.get('agent')}】")
        lines.append(result.get("output", ""))
    return "\n\n".join(lines)







def diagnosis_agent(state: MetroAgentState) -> dict:
    """故障诊断 Agent —— 分析故障现象，给出排查步骤和安全注意事项。

    与 RealtimeAgent 的关键区别：本 Agent 不调用工具（tool-use），
    而是消费其他 Agent 已经产生的信息（tool_results + knowledge_context），
    在此基础上做推理和结构化输出。

    执行流程（5 阶段）：

      阶段 1 —— 构建辅助上下文
        从 state.tool_results 提取最近的工具调用记录（格式化文本）
        从 state.agents_output.knowledge 提取 KnowledgeAgent 的检索结果

      阶段 2 —— 预算分配
        由 AgentContextBudgetManager 根据 diagnosis profile、
        当前 mode（standard/extended）和 requested 需求，
        计算出各部分（history / tool / rag）的 token 配额

      阶段 3 —— 对话历史组装
        AgentContextAssembler.build() 按配额从 messages 中
        提取近期对话原文 + 旧轮摘要作为历史消息列表

      阶段 4 —— 内容截断
        根据分配到的 tool_tokens / rag_tokens 额度，
        将阶段 1 的辅助内容截断至不超出预算

      阶段 5 —— LLM 推理
        组装三层消息结构 → 调 GLM-4.6V → 返回诊断结果

    消息结构（三层，按 LLM 处理优先级排列）：
      Layer 1: SystemMessage(DIAGNOSIS_AGENT_PROMPT)  ← 行为约束 + 输出格式
      Layer 2: 可选 SystemMessage(工具证据)           ← 设备/告警数据
      Layer 3: 可选 SystemMessage(知识依据)           ← KnowledgeAgent 结论
      Layer 4: 对话历史消息                           ← 近期原文 + 旧轮摘要

    Args:
        state: MetroAgent 全局状态，需包含：
               - user_input: 用户输入
               - tool_results: 中心黑板中的工具调用记录
               - agents_output.knowledge: KnowledgeAgent 的检索结论
               - messages: 对话历史

    Returns:
        dict: 包含以下键：
          - agents_output.diagnosis: 结构化诊断结果文本
          - context_allocations.diagnosis: 本次上下文分配的详细账单
            （含 history_tokens / tool_tokens / rag_tokens 等，
             用于调试和监控上下文预算使用情况）
    """




    existing_tool_results = state.get("tool_results", {})



    tool_context = diagnosis_tool_context_builder.build_text(
        existing_tool_results
    )



    knowledge_context = str(
        state.get("agents_output", {}).get(
            "knowledge",
            "",
        )
    ).strip()







    tool_tokens = diagnosis_context_assembler.count_text(
        tool_context
    )
    knowledge_tokens = diagnosis_context_assembler.count_text(
        knowledge_context
    )




    context_mode = (
        "extended"
        if tool_context or knowledge_context
        else "standard"
    )






    assembly = diagnosis_context_assembler.build(
        state=state,
        system_prompt=DIAGNOSIS_AGENT_PROMPT,
        requested={
            "tool": tool_tokens,
            "rag": knowledge_tokens,
        },
        mode=context_mode,
    )
    allocation = assembly["allocation"]







    limited_tool_context = diagnosis_context_assembler.truncate_text(
        tool_context,
        allocation["tool_tokens"],
    )
    limited_knowledge_context = (
        diagnosis_context_assembler.truncate_text(
            knowledge_context,
            allocation["rag_tokens"],
        )
    )






    history_messages = assembly["messages"]



    if not history_messages:
        history_messages = [
            HumanMessage(content=state["user_input"])
        ]



    context_messages = []



    if limited_tool_context:
        context_messages.append(
            SystemMessage(
                content=(
                    "以下是工具查询得到的设备或告警证据。"
                    "只能依据其中实际存在的数据诊断：\n\n"
                    f"{limited_tool_context}"
                )
            )
        )



    if limited_knowledge_context:
        context_messages.append(
            SystemMessage(
                content=(
                    "以下是KnowledgeAgent提供的知识依据，"
                    "用于辅助诊断，不得扩展其中没有的事实：\n\n"
                    f"{limited_knowledge_context}"
                )
            )
        )
    dependency_outputs = state.get("dependency_outputs", {})
    formatted_dependency_outputs = format_dependency_outputs(dependency_outputs)
    dependency_context = f"""
    以下是前置步骤结果，请优先基于这些结果诊断：
    {formatted_dependency_outputs}
    """





    messages = [
        SystemMessage(content=DIAGNOSIS_AGENT_PROMPT),
        SystemMessage(content=dependency_context),
        *context_messages,
        *history_messages,
    ]


    response = get_diagnosis_agent_llm().invoke(messages)
    answer = response.content

    state_update = {


        "agents_output": {
            "diagnosis": answer,
        },




        "context_allocations": {
            "diagnosis": allocation,
        },
        LLM_USAGE_RECORDS_KEY: capture_response_usages(
            response,
            fallback_model="glm-4.6v",
            call_name="diagnosis.generate",
        ),
    }

    return state_update
