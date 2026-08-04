"""
Knowledge Agent —— 知识库检索 Agent
====================================
通过 RAG（检索增强生成）从地铁通信知识库中检索相关文档，
结合历史对话上下文回答用户问题。

与其他 Agent 的区别：
  - KnowledgeAgent：本地 Ollama Qwen2.5 7B，6K 上下文（最小），RAG 为核心
  - DiagnosisAgent：云端 GLM-4.6V，24K 上下文，消费 KnowledgeAgent 的结论
  - RealtimeAgent：本地 Ollama，8K 上下文，单工具调用

上下文预算模型：
  profile: knowledge → input_limit=6000（最紧凑的预算）
  mode: standard（历史只占 60% 可变空间，为 RAG 结果留更多余地）
  requested: {"rag": N, "tool": 0} — 只需要 rag 配额，不需要 tool

RAG 查询增强：
  将近期对话历史拼入查询语句，使检索语义更精准。例如：
    原查询："如何处理"
    增强后："对话历史：\n用户:3号线信号故障\n助手:...\n\n当前问题：如何处理"
  有了上下文，向量检索能更好匹配用户真实意图。
"""

from time import perf_counter

from langchain.messages import SystemMessage

from metro_agent.config import (
    build_Ollama_qwenLLM,       # 工厂函数：创建本地 Ollama Qwen2.5 7B 连接
    KNOWLEDGE_AGENT_PROMPT,     # 知识库系统提示词（含 {rag_context} 占位符）
    SHORT_TERM_MEMORY_LIMIT,    # 短期记忆保留轮数（用于 RAG 查询上下文截断）
)
from metro_agent.memory.short_term.agent_context_assembler import (
    AgentContextAssembler,  # 按 Agent 预算组装对话历史上下文
)
from metro_agent.tools.Knowledge_RAGtools import (
    build_rag_search,       # RAG 检索入口：向量检索 + HyDE 查询改写 + Reranker 重排
)
from metro_agent.memory.short_term.context_builder import (
    ConversationContextBuilder,  # 用于格式化 RAG 查询中的对话历史文本
)
from metro_agent.state import MetroAgentState
from metro_agent.observability.finalize_node import get_trace_registry
from metro_agent.observability.agent_usage import LLM_USAGE_RECORDS_KEY, capture_response_usages

# ============================================================
# LLM 实例与上下文构建器（模块级单例）
# ============================================================

# 本地 Ollama Qwen2.5 7B：知识库场景不需要强推理，本地模型零成本

knowledge_agent_LLM= None

_NO_RETRIEVAL_FALLBACK_MARKERS = (
    "当前知识库未检索到",
    "未检索到可引用资料",
    "无法依据知识库给出结论",
)


def get_knowledge_agent_llm():
    global knowledge_agent_LLM

    if knowledge_agent_LLM is None:
        knowledge_agent_LLM = build_Ollama_qwenLLM()

    return knowledge_agent_LLM


def _prefer_retrieval_answer(answer: str, retrieval_answer: object) -> str:
    """Do not discard a usable RAG answer when generation falsely claims no sources."""
    normalized = answer.strip()
    fallback = str(retrieval_answer or "").strip()
    if (
        fallback
        and not any(marker in fallback for marker in _NO_RETRIEVAL_FALLBACK_MARKERS)
        and any(marker in normalized for marker in _NO_RETRIEVAL_FALLBACK_MARKERS)
    ):
        return fallback
    return normalized
# RAG 查询构建器：将最近 N 轮对话历史格式化为文本，
# 拼入 RAG 查询以增强检索语义准确性
knowledge_query_builder = ConversationContextBuilder(
    max_rounds=SHORT_TERM_MEMORY_LIMIT
)

# 上下文组装器：在调 LLM 生成最终答案时，按 knowledge 的 6K 预算
# 提取对话历史（近期原文 + 旧轮摘要）
knowledge_context_assembler = AgentContextAssembler(
    agent_name="knowledge"
)


def build_rag_context(*, answer: str, source_text: str, source_doc_text: str) -> str:
    """Build only answer-facing RAG context; scores remain internal metadata."""
    return f"""
    【RAG 生成答案】
    {answer}

    【内部参考资料】
    {source_text}

    【引用文档】
    {source_doc_text}
    """


def record_rag_retrieval_trace(
    state: MetroAgentState,
    *,
    query: str,
    rag_result: dict | None,
    latency_ms: float,
    error: Exception | None = None,
) -> None:
    """Record retrieval identity and timing only; chunk text stays in build artifacts."""
    trace_id = state.get("trace_id")
    if not isinstance(trace_id, str) or not trace_id:
        return
    try:
        recorder = get_trace_registry().get(trace_id)
        if recorder is None:
            return
        sources = rag_result.get("sources", []) if isinstance(rag_result, dict) else []
        index_build_id = (
            rag_result.get("index_build_id") if isinstance(rag_result, dict) else None
        )
        top_k = rag_result.get("top_k", len(sources)) if isinstance(rag_result, dict) else 0
        with recorder.span(
            "rag",
            agent="knowledge_agent",
            attributes={
                "query_length": len(query),
                "index_build_id": index_build_id,
                "top_k": top_k,
            },
        ) as span:
            if error is not None:
                span.record_rag_retrieval(
                    query_summary=query[:512],
                    index_build_id=index_build_id,
                    result_summary={"error_type": type(error).__name__},
                )
                span.set_result(
                    "failed",
                    {
                        "query_length": len(query),
                        "index_build_id": index_build_id,
                        "retrieved_count": 0,
                        "top_k": top_k,
                        "latency_ms": latency_ms,
                        "error_type": type(error).__name__,
                    },
                )
                return
            for source in sources:
                if not isinstance(source, dict):
                    continue
                metadata = source.get("metadata")
                metadata = metadata if isinstance(metadata, dict) else {}
                span.record_rag_retrieval(
                    query_summary=query[:512],
                    index_build_id=index_build_id,
                    chunk_id=source.get("chunk_id"),
                    document_id=source.get("file_name"),
                    rank=source.get("rank"),
                    raw_score=source.get("raw_score"),
                    rerank_score=source.get("rerank_score"),
                    cited=False,
                    result_summary={
                        "source_path": (
                            metadata.get("relative_path")
                            or metadata.get("file_path")
                            or ""
                        ),
                    },
                )
            span.set_result(
                "success",
                {
                    "query_length": len(query),
                    "index_build_id": index_build_id,
                    "retrieved_count": len(sources),
                    "top_k": top_k,
                    "latency_ms": latency_ms,
                },
            )
    except Exception:
        return


# ============================================================
# 知识库 Agent
# ============================================================


def knowledge_agent(state: MetroAgentState) -> dict:
    """知识库检索 Agent —— 基于 RAG 回答 SOP、操作规程等知识性问题。

    执行流程（6 阶段）：

      阶段 1 —— 构建增强 RAG 查询
        排除当前轮消息（避免当前问题重复），将近期对话历史
        格式化为文本拼入查询，提升向量检索的语义匹配度

      阶段 2 —— RAG 检索
        build_rag_search() 内部执行：
          a. HyDE 查询改写（LLM 将短查询扩展为假设文档）
          b. 向量检索（ChromaDB 相似度搜索）
          c. Reranker 重排序（Cross-encoder 精排）
        返回：answer + sources + source_docs

      阶段 3 —— 解析检索结果
        提取三种格式的引用信息：
          - source_text: 资料片段全文（注入 prompt 供 LLM 参考）
          - source_doc_text: 文档名列表（展示给用户的引用来源）
          - source_score: 文件名 + 相似度分数（内部调试用）

      阶段 4 —— 预算分配
        先以空 RAG 上下文计算 system_prompt 的固定 token 消耗，
        再向 BudgetManager 请求 rag 配额

      阶段 5 —— RAG 内容截断
        按分配到的 rag_tokens 额度截断 RAG 上下文，
        截断后的内容重新填入 KNOWLEDGE_AGENT_PROMPT

      阶段 6 —— LLM 生成
        组装 [SystemPrompt(含RAG), ...历史消息] → Ollama 生成答案

    Args:
        state: MetroAgent 全局状态，需包含：
               - user_input: 用户当前输入
               - messages: 对话历史
               - current_round_id: 当前轮次 ID（用于排除本轮消息）

    Returns:
        dict: 包含以下键：
          - agents_output.knowledge:      RAG 增强后的答案
          - agents_output.sources:        引用文档名列表（展示用）
          - agents_output.sources_score:  检索分数详情（调试用）
          - context_allocations.knowledge: 上下文分配账单
    """
    user_input = state["user_input"]

    # ==============================================================
    # 阶段 1：构建增强 RAG 查询（对话历史 + 当前问题）
    # ==============================================================

    # 排除当前轮消息：当前 HumanMessage 已在 context_node 中追加到
    # messages，如果不排除，RAG 查询中会出现两次当前问题
    past_messages = [
        message
        for message in state.get("messages", [])
        if message.additional_kwargs.get("round_id")
        != state.get("current_round_id")
    ]

    # 从过去消息中截取最近 N 轮（默认 6 轮），格式化为文本
    query_history = knowledge_query_builder.select_messages(
        past_messages
    )

    # 有历史 → 拼入查询增强语义；无历史（首轮对话）→ 直接用原始问题
    if query_history:
        history_text = knowledge_query_builder.format_messages(
            query_history
        )
        rag_query = (
            f"对话历史：\n{history_text}\n\n"
            f"当前问题：{user_input}"
        )
    else:
        rag_query = user_input

    # ==============================================================
    # 阶段 2：RAG 检索（向量搜索 + HyDE 改写 + Reranker 重排）
    # ==============================================================
    rag_started_at = perf_counter()
    try:
        rag_result = build_rag_search(rag_query)
    except Exception as exc:
        record_rag_retrieval_trace(
            state,
            query=rag_query,
            rag_result=None,
            latency_ms=(perf_counter() - rag_started_at) * 1000,
            error=exc,
        )
        raise
    record_rag_retrieval_trace(
        state,
        query=rag_query,
        rag_result=rag_result,
        latency_ms=(perf_counter() - rag_started_at) * 1000,
    )
    evaluation_retrieval_contexts = list(
        dict.fromkeys(
            str(source.get("text", "")).strip()
            for source in rag_result["sources"]
            if isinstance(source, dict) and str(source.get("text", "")).strip()
        )
    )

    if not rag_result["sources"]:
        return {
            "agents_output": {
                "knowledge": (
                    "当前知识库未检索到可引用资料，无法依据知识库给出结论。"
                    "请补充具体系统、设备、站点或故障现象后重试。"
                ),
                "sources": "",
                "sources_score": "",
                "retrieved_sources": [],
                "index_build_id": rag_result["index_build_id"],
                "top_k": rag_result["top_k"],
                "retrieved_count": 0,
            },
            "evaluation_retrieval_contexts": [],
            LLM_USAGE_RECORDS_KEY: [],
        }

    # ==============================================================
    # 阶段 3：解析检索结果 → 三种格式的引用信息
    # ==============================================================

    # 3a：资料片段全文 —— 每个 source 是一段从知识文档中切出的文本
    # 注入 prompt 供 LLM 直接参考，格式："资料片段1:xxx\n\n资料片段2:xxx"
    source_text = "\n\n".join(
        f"资料片段{i + 1}:{source['text']}"
        for i, source in enumerate(rag_result["sources"])
    )

    # 3b：引用文档名 —— 去重后的文档名列表，展示给用户
    # 格式："通信SOP手册、故障处理规程"
    source_doc_text = (
        "、".join(rag_result["source_docs"])
        if rag_result["source_docs"]
        else "未检索到明确文档来源"
    )

    # 3c：检索分数 —— Reranker 的相似度评分
    # 同一文档可能有多个切片，取最高分（因为 Reranker 返回同一文档的多个 chunk）
    doc_scores = {}
    for source in rag_result["sources"]:
        file_name = source["file_name"]
        score = source["score"]
        if score is None:
            continue
        # 同一文档的多个切片取最高分
        doc_scores[file_name] = max(
            doc_scores.get(file_name, float("-inf")),
            score,
        )

    # 格式化为可读的分数列表，供内部检索质量调试。
    source_score = "\n".join(
        f"{i + 1}. {source['file_name']}: "
        f"{source['score']:.4f}"
        for i, source in enumerate(rag_result["sources"])
        if source["score"] is not None
    )

    # ==============================================================
    # 阶段 4：预算分配 —— 计算 RAG 上下文消耗 + 请求配额
    # ==============================================================

    # 将 RAG 结果组装为【RAG 生成答案 + 参考资料 + 引用文档】。
    # 检索分数只保留在 agents_output 中，不能注入回答模型的上下文。
    # 这些内容将作为 {rag_context} 填入 KNOWLEDGE_AGENT_PROMPT
    rag_context = build_rag_context(
        answer=rag_result["answer"],
        source_text=source_text,
        source_doc_text=source_doc_text,
    )

    # 以空 RAG 上下文计算 base_system_prompt 的 token 数
    # 这是传给 BudgetManager 的 fixed_tokens：无论 RAG 结果如何都必须消耗的部分
    base_system_prompt = KNOWLEDGE_AGENT_PROMPT.format(
        rag_context=""
    )

    # 计算完整 RAG 上下文的 token 数，作为 requested.rag 提交
    rag_token_count = knowledge_context_assembler.count_text(
        rag_context
    )

    # BudgetManager.allocate() 内部：
    #   1. available = 6000(input_limit) - fixed_tokens(base_prompt)
    #   2. history_min = 600（最低保障）→ 先分配给历史
    #   3. 按 priority 分配 rag 配额（knowledge profile 的 priority = ["rag"]）
    #   4. 剩余空间回填给历史
    assembly = knowledge_context_assembler.build(
        state=state,
        system_prompt=base_system_prompt,
        requested={
            "rag": rag_token_count,  # 请求将 RAG 内容全部装入
            "tool": 0,               # KnowledgeAgent 不消费工具结果
        },
        mode="standard",  # 保守模式：历史只占 60%，为 RAG 留更多空间
    )

    allocation = assembly["allocation"]

    # ==============================================================
    # 阶段 5：RAG 内容截断 —— 按配额裁剪
    # ==============================================================

    # 如果 rag_token_count > allocation["rag_tokens"]，
    # RAG 上下文会被截断，优先保留靠前的段落
    # （rag_context 的内部顺序：RAG答案 → 参考资料 → 引用文档 → 分数，
    #   越靠前的内容对 LLM 越有价值）
    limited_rag_context = (
        knowledge_context_assembler.truncate_text(
            rag_context,
            allocation["rag_tokens"],
        )
    )

    # 用截断后的 RAG 内容重新填入 prompt 模板
    system_prompt = KNOWLEDGE_AGENT_PROMPT.format(
        rag_context=limited_rag_context
    )

    # ==============================================================
    # 阶段 6：LLM 生成最终答案
    # ==============================================================

    # 消息结构（两层）：
    #   [0] SystemMessage(提示词 + 截断后的 RAG 内容)  ← 知识约束
    #   [1:] 对话历史消息                                ← 近期原文 + 旧轮摘要
    messages = [
        SystemMessage(content=system_prompt),
        *assembly["messages"],
    ]

    # 调本地 Ollama Qwen2.5 7B 生成答案
    # temperature 在 build_Ollama_qwenLLM 中已配置（通常 0.1-0.3，偏确定性）
    response = get_knowledge_agent_llm().invoke(messages)
    answer = _prefer_retrieval_answer(str(response.content), rag_result.get("answer"))
    retrieved_sources = [
        {
            "chunk_id": source.get("chunk_id"),
            "rank": source.get("rank"),
            "score": source.get("score"),
        }
        for source in rag_result["sources"]
        if source.get("chunk_id")
    ]

    return {
        "agents_output": {
            "knowledge": answer,          # RAG 增强后的答案文本
            "sources": source_doc_text,   # 引用文档名（供前端展示）
            "sources_score": source_score,  # 检索分数（供内部质量调试）
            "retrieved_sources": retrieved_sources,
            "index_build_id": rag_result["index_build_id"],
            "top_k": rag_result["top_k"],
            "retrieved_count": rag_result["retrieved_count"],
        },
        # 上下文分配账单：记录本次各部分实际获得的 token 配额
        "context_allocations": {
            "knowledge": allocation,
        },
        "evaluation_retrieval_contexts": evaluation_retrieval_contexts,
        LLM_USAGE_RECORDS_KEY: capture_response_usages(
            response,
            fallback_model="metro-ops-qwen2.5-7b",
            call_name="knowledge.generate",
        ),
    }
