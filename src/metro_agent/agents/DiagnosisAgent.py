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
    build_Chat_ZhiPuLLM_DIAGNOSIS,   # 工厂函数：创建智谱 GLM-4.6V 连接
    DIAGNOSIS_AGENT_PROMPT,          # 诊断系统提示词（含输出格式约束）
    TOOL_CONTEXT_MAX_ITEMS,          # 工具调用记录最多保留条数（默认 3）
    TOOL_CONTEXT_MAX_RESULT_CHARS,   # 单条工具结果最大字符数（默认 2000）
)
from metro_agent.memory.short_term.agent_context_assembler import (
    AgentContextAssembler,  # 按 Agent 预算组装对话历史的上下文消息
)
from metro_agent.memory.short_term.tool_context_builder import (
    ToolContextBuilder,     # 从中心黑板提取并格式化工具调用记录
)
from metro_agent.state import MetroAgentState
from metro_agent.observability.agent_usage import LLM_USAGE_RECORDS_KEY, capture_response_usages

# ============================================================
# LLM 实例与上下文构建器（模块级单例，复用连接和配置）
# ============================================================

_diagnosis_agent_llm = None


def get_diagnosis_agent_llm():
    global _diagnosis_agent_llm

    if _diagnosis_agent_llm is None:
        _diagnosis_agent_llm = build_Chat_ZhiPuLLM_DIAGNOSIS()

    return _diagnosis_agent_llm
# 上下文组装器：从 messages 历史中按 diagnosis 的 token 预算
# （24K 上下文窗口）提取最近对话，优先用摘要，摘要不够用原文
diagnosis_context_assembler = AgentContextAssembler(
    agent_name="diagnosis"
)

# 工具上下文构建器：从中心黑板（state.tool_results）中提取
# 最近的工具调用记录，截断并格式化为诊断参考文本
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


# ============================================================
# 故障诊断 Agent
# ============================================================


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
    # ==============================================================
    # 阶段 1：构建辅助上下文（工具数据 + 知识依据）
    # ==============================================================

    existing_tool_results = state.get("tool_results", {})

    # 从中心黑板提取最近的工具调用记录，格式化为诊断参考文本
    # 例如："调用ID：xxx\n工具：query_alarm\n参数：...\n结果：..."
    tool_context = diagnosis_tool_context_builder.build_text(
        existing_tool_results
    )

    # KnowledgeAgent 若在当前链路中先执行，其结论作为知识依据注入诊断
    # 例如："该告警关联到 3 号线信号系统，历史同类故障 12 起..."
    knowledge_context = str(
        state.get("agents_output", {}).get(
            "knowledge",
            "",
        )
    ).strip()

    # ==============================================================
    # 阶段 2：预算分配 —— 由 BudgetManager 决定各部分的 token 配额
    # ==============================================================

    # 预计算 tool 和 rag 内容的 token 数，作为 requested 需求提交给分配器
    # 分配器会根据 profile 中的 priority 顺序和 max 上限决定实际配额
    tool_tokens = diagnosis_context_assembler.count_text(
        tool_context
    )
    knowledge_tokens = diagnosis_context_assembler.count_text(
        knowledge_context
    )

    # 模式选择：
    #   extended: 有 tool 或 knowledge 内容时启用 → 历史预算因子 = 1.00
    #   standard: 两者都为空时 → 历史预算因子 = 0.60（更保守，为输出留空间）
    context_mode = (
        "extended"
        if tool_context or knowledge_context
        else "standard"
    )

    # AgentContextAssembler.build() 内部流程：
    #   1. count_text(system_prompt) → fixed_tokens（固定消耗）
    #   2. BudgetManager.allocate() → allocation（各区域配额）
    #   3. 按 allocation.history_tokens 从 messages 中贪婪选择对话历史
    #   4. 返回 {"messages": [...], "allocation": {...}}
    assembly = diagnosis_context_assembler.build(
        state=state,
        system_prompt=DIAGNOSIS_AGENT_PROMPT,
        requested={
            "tool": tool_tokens,   # 请求分配给工具上下文的 token 数
            "rag": knowledge_tokens,  # 请求分配给知识上下文的 token 数
        },
        mode=context_mode,
    )
    allocation = assembly["allocation"]

    # ==============================================================
    # 阶段 4：按配额截断辅助内容
    # ==============================================================

    # 将工具上下文截断到分配到的 tool_tokens 额度
    # truncate_text 使用 tiktoken 精确截断，不会从多字节字符中间切断
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

    # ==============================================================
    # 阶段 5：组装最终消息列表并推理
    # ==============================================================

    # 提取组装好的对话历史消息（近期原文 + 旧轮摘要）
    history_messages = assembly["messages"]

    # 兜底：如果对话历史为空（极端情况，如首轮且无摘要），
    # 至少把当前用户输入作为历史消息传给 LLM
    if not history_messages:
        history_messages = [
            HumanMessage(content=state["user_input"])
        ]

    # 构建辅助上下文消息列表（Layer 2 + Layer 3）
    # 每个辅助上下文用独立的 SystemMessage 包装，附带使用约束
    context_messages = []

    # Layer 2：工具查询结果 —— 设备/告警的实际数据
    # "只能依据其中实际存在的数据诊断" 防止 LLM 编造不存在的设备状态
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

    # Layer 3：KnowledgeAgent 结论 —— 知识库检索结果
    # "不得扩展其中没有的事实" 防止 LLM 把检索结果当作灵感随意发挥
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
    # 最终消息结构（按 LLM 处理优先级排列）：
    #   [0] SystemMessage(诊断提示词)      ← 行为约束，最高优先级
    #   [1] SystemMessage(工具证据)        ← 可选，事实约束
    #   [2] SystemMessage(知识依据)        ← 可选，知识约束
    #   [3:] 对话历史                       ← Human/AI/System 消息序列
    messages = [
        SystemMessage(content=DIAGNOSIS_AGENT_PROMPT),
        SystemMessage(content=dependency_context),
        *context_messages,
        *history_messages,
    ]

    # 调云端 GLM-4.6V 进行推理
    response = get_diagnosis_agent_llm().invoke(messages)
    answer = response.content

    state_update = {
        # 诊断结果写入 agents_output，供计划聚合和观测使用。
        # 和 response_node 记录到 messages 历史
        "agents_output": {
            "diagnosis": answer,
        },
        # 记录本次上下文分配的详细账单，用于：
        #   1. 调试 token 预算是否合理
        #   2. 监控各 Agent 的上下文利用率
        #   3. 发现预算配置问题（如某 Agent 频繁耗尽配额）
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
