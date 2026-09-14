"""
MetroAgent 全局状态定义
=======================
定义多 Agent 协作系统中的全局状态结构，所有 Agent 共享同一个 state 字典。
LangGraph 工作流按节点顺序读写此 state，Annotated 类型的字段使用自定义
reducer 函数合并并发更新，避免节点间的写入冲突。

状态设计原则：
  1. 单一数据源：所有 Agent 从同一个 state 读取输入，写入同一个 state
  2. 累加型字段（messages / tool_results / agents_output）使用 reducer 合并
  3. 覆盖型字段（user_input / final_answer）直接赋值
  4. total=False：所有字段可选，未显式设置的字段不出现在字典中

Annotated 字段的 reducer 机制：
  当多个节点并发写入同一个 Annotated 字段时，LangGraph 不会直接覆盖，
  而是调用字段绑定的 reducer 函数来合并"当前值"和"新值"：
    - add_messages：将新消息追加到消息列表末尾（内置）
    - operator.or_：取两个 dict 的并集（内置）
    - merge_conversation_summaries：按 round_id 去重合并（自定义）
    - merge_compression_jobs：按 round_id 字段级合并更新（自定义）

state 字段按功能分为 7 组：
  1. 身份与会话        —— 标识当前用户和对话线程
  2. 短期记忆          —— 滑动窗口消息 + 压缩摘要 + 压缩任务管线
  3. 当前任务状态      —— Supervisor 编排 Agent 的执行计划
  4. 当前实体          —— 从用户输入中提取的运维相关实体
  5. 工具与 Agent 结果 —— 各 Agent 的输出汇总 + 工具调用中心黑板
  6. 长期记忆          —— ChromaDB 跨会话持久化的用户信息
  7. 可观测性          —— 运行时统计 + 预算分配账单
"""

import operator
from typing import Annotated, Any, TypedDict

from langchain_core.messages import BaseMessage
from langgraph.graph.message import add_messages


def merge_evaluation_retrieval_contexts(
    existing: list[str] | None, incoming: list[str] | None,
) -> list[str]:
    values = [*(existing or []), *(incoming or [])]
    return list(dict.fromkeys(value for value in values if isinstance(value, str) and value.strip()))






class ConversationSummary(TypedDict, total=False):
    """单轮对话的压缩摘要记录。

    当一轮对话被压缩管线处理完毕后，compression_result_node 创建此结构
    并追加到 conversation_summaries 列表。按 round_number 排序即可还原
    对话的压缩版时间线。

    total=False 表示所有字段都是可选的——不同阶段写入的摘要可能包含
    不同字段（如刚创建时还没有 token_count）。

    与原始消息的关系：
      原始消息在 messages 中（完整原文），被压缩后从 messages 中移除，
      以 ConversationSummary 形式保留在 conversation_summaries 中。
      召回时摘要优先（信息密度高），未生成摘要的轮次用原文兜底。
    """
    round_id: str
    round_number: int
    summary: str
    source_message_ids: list[str]
    source_ref: str
    token_count: int
    created_at: str


class CompressionJob(TypedDict, total=False):
    """对话压缩任务的状态跟踪。

    记录一次压缩（摘要生成）任务从发起到完成的全生命周期。
    用于异步压缩场景：detection_node 创建任务 → queue_node 入队 Redis →
    Worker 异步消费 → result_node 应用结果。

    任务状态机：
      pending → queued → (Worker 消费) → completed/failed → applied
    """
    job_id: str
    round_id: str
    round_number: int
    source_message_ids: list[str]
    base_summary_version: int
    status: str
    result_summary: str
    source_ref: str
    error: str
    requested_at: str
    queued_at: str
    completed_at: str
    applied_at: str






def merge_conversation_summaries(
    current: list[ConversationSummary],
    updates: list[ConversationSummary],
) -> list[ConversationSummary]:
    """合并对话摘要列表：以 round_id 为键去重合并，按 round_number 排序。

    LangGraph 的 Annotated reducer 机制：
      当多个节点并发写入同一个 Annotated 字段时，LangGraph 会调用
      此处定义的自定义 reducer 来合并"当前值"和"新值"，而不是简单覆盖。

    合并规则：
      1. 相同 round_id 的条目：updates 中的字段覆盖 current 中同名字段
         （因为 updates 通常包含更新的摘要内容或新增字段如 token_count）
      2. 不同 round_id 的条目：保留双方的独立条目
      3. 最终按 round_number 升序排列，保持时间线顺序（旧→新）

    为什么不是简单的 list 拼接：
      如果两个节点都向 conversation_summaries 追加摘要，直接拼接
      会导致同一个 round_id 出现两条记录。以 round_id 为键去重
      确保每轮对话只有一条摘要记录。

    Args:
        current: state 中已有的摘要列表。
        updates: 本次节点写入的新摘要列表（通常是 compression_result_node 的输出）。

    Returns:
        合并并按 round_number 升序排序后的摘要列表。
    """

    merged = {
        item["round_id"]: item
        for item in current
    }




    for item in updates:
        round_id = item["round_id"]
        merged[round_id] = {
            **merged.get(round_id, {}),
            **item,
        }


    return sorted(
        merged.values(),
        key=lambda item: item.get("round_number", 0),
    )


def merge_compression_jobs(
    current: dict[str, CompressionJob],
    updates: dict[str, CompressionJob],
) -> dict[str, CompressionJob]:
    """合并压缩任务字典：以 round_id 为键，updates 字段级覆盖 current 同键值。

    与 merge_conversation_summaries 类似，但操作对象是 dict[str, dict] 而非
    list[dict]。适用于异步压缩场景中跟踪任务状态迁移：
      detection_node 创建 job (status: pending, 含 source_message_ids)
        → queue_node 更新 (status: queued, 含 source_ref + queued_at)
        → result_node 更新 (status: applied, 含 result_summary + applied_at)

    字段级合并的含义：
      每次更新只传入变化了的字段，保留之前写入的其他字段。
      例如 queue_node 只写入 status/queued_at/source_ref，
      不会覆盖 detection_node 写入的 source_message_ids。

    Args:
        current: state 中已有的压缩任务字典（key = round_id）。
        updates: 本次节点写入的新/更新压缩任务（通常只包含变化字段）。

    Returns:
        合并后的压缩任务字典，所有字段被聚合到一条记录中。
    """
    merged = dict(current)

    for round_id, update in updates.items():
        merged[round_id] = {
            **merged.get(round_id, {}),
            **update,
        }

    return merged






class MetroAgentState(TypedDict, total=False):
    """地铁运维多 Agent 系统的全局状态。

    整个 LangGraph 工作流中的所有节点共享同一个 state 字典。
    各字段按功能分为 7 组，便于理解和维护。

    total=False 的含义：
      所有字段默认都是可选的（Optional），不需要在初始化时全部提供。
      LangGraph 会在工作流执行过程中逐节点填充所需字段。
      未显式设置的字段不会出现在 state 字典中，读取时 get() 返回默认值。

    字段命名约定：
      - str 字段：直接赋值（后写入覆盖先写入）
      - Annotated[..., reducer] 字段：通过 reducer 合并并发写入
      - 短期记忆相关字段以 current_ 或 compression_ 为前缀
      - 长期记忆相关字段以 memory_ 为前缀
    """





    user_id: str
    """用户唯一标识（如工号 "M12345"）。
    用于：
      - 长期记忆的 user_id 隔离（ChromaDB where 过滤）
      - 压缩队列的 thread_id 隔离（Redis key 前缀）
      - 审计日志的用户维度检索
    """

    thread_id: str
    """对话线程 ID，用于 Redis 归档隔离和 LangGraph checkpoint 管理。
    同一用户可以有多个并发 thread（如不同设备），
    压缩队列的 Redis key 包含 thread_id 以防止跨会话串数据：
      metro:compression:source:{thread_id}:{round_id}
    """

    session_id: str
    """会话 ID，用于追踪单次登录会话。
    可用于会话级别的日志关联和会话终止时的清理回调。
    """





    messages: Annotated[list[BaseMessage], add_messages]
    """当前对话的完整消息历史。

    使用 LangGraph 内置的 add_messages reducer：新消息自动追加到列表末尾，
    而非覆盖整个列表。这是 LangGraph 中消息管理的最标准方式。

    生命周期：
      轮次开始 → context_node 追加 HumanMessage（含 round_id/round_number）
      轮次结束 → response_node 追加 AIMessage（含 round_id/round_number）
      压缩完成 → compression_result_node 用 Overwrite 移除已压缩的旧消息

    HumanMessage 和 AIMessage 通过 additional_kwargs 携带轮次追踪信息：
      {"round_id": "uuid-...", "round_number": 5}
    """

    conversation_context: str
    """格式化后的对话上下文字符串。

    由 context_node 调用 ConversationContextBuilder.build_text() 生成：
      1. 截断最近 6 轮（以 HumanMessage 为锚点）
      2. 格式化为 "角色:内容\\n角色:内容..." 纯文本
      3. 直接拼入各 Agent 的 LLM prompt

    注意：这是给旧版简单 Agent 使用的全局上下文文本。
    新版 Agent 通过 AgentContextAssembler 获取按 token 预算定制的上下文消息列表。
    """

    conversation_summaries: Annotated[
        list[ConversationSummary],
        merge_conversation_summaries,
    ]
    """对话压缩摘要列表。

    当旧轮次被压缩管线处理后，compression_result_node 创建 ConversationSummary
    并写入此列表。与 messages 互补——messages 保留近期原文，summaries 保留旧轮次摘要。

    在上下文组装时的使用：
      AgentContextAssembler 优先使用 summaries 中的摘要（信息密度高），
      摘要未生成的旧轮次用 messages 中的原文兜底。

    容量控制：
      state_memory_pruner 监控 summaries 数量，超出 SHORT_MEMORY_MAX_TOTAL_ROUNDS
      或 SHORT_MEMORY_MAX_TOTAL_TOKENS 时从最旧的开始删除。
    """

    compression_jobs: Annotated[
        dict[str, CompressionJob],
        merge_compression_jobs,
    ]
    """压缩任务跟踪字典。

    key = round_id（被压缩的轮次 ID），value = CompressionJob（任务状态）。
    整个压缩管线中的节点通过此字典追踪每个压缩任务的生命周期：
      detection_node → 创建 job (status: pending)
      queue_node     → 更新 job (status: queued + source_ref + queued_at)
      result_node    → 更新 job (status: applied + result_summary + applied_at)
      失败时任一节点 → 更新 job (status: failed + error)

    merge_compression_jobs 保证各节点只写入自己的字段，不破坏其他节点写入的数据。
    """

    compression_version: int
    """当前压缩版本号，单调递增。

    每新增一个 ConversationSummary 时 +1（由 compression_result_node 维护）。
    用途：
      - 冲突检测：创建 CompressionJob 时记录 base_summary_version，
        应用结果时检查版本是否已变化
      - 调试追踪：通过版本号快速定位摘要的生成顺序
    """

    compression_status: str
    """全局压缩管线状态。

    由 compression_result_node 在每轮应用结果时更新：
      "idle"        → 无活跃任务（所有 pending/queued/running 都已 applied）
      "pending"     → 有新任务被 detection_node 创建，等待 queue_node 入队
      "queued"      → 任务已入队 Redis，等待 Worker 消费
      "compressing" → 有待处理或处理中的任务（pending/queued/running 任一存在）
      "error"       → 有任务失败，需检查 compression_error 获取详情
    """

    compression_error: str
    """最近一次压缩失败的错误信息。

    由 queue_node（入队失败）或 result_node（结果校验失败）写入。
    多行文本（\\n 分隔），包含失败的 round_id 和具体原因。
    成功压缩时被清空为空字符串。
    """





    user_input: str
    """用户本轮原始输入文本（去除首尾空白后的内容）。

    由 context_node 在轮次开始时包装为 HumanMessage 之前记录。
    整个工作流中多个节点从此字段读取用户请求：
      - entry_router 用于判断是否进入记忆召回路径
      - planner_node 用于生成执行计划
      - memory_curator_node 作为 Curator 提取记忆的 user_text 输入
      - memory_recall_node 用于检测记忆查询意图和类别
    """

    task_state: dict[str, Any]
    """当前任务的临时状态字典。

    Agent 之间传递的中间数据，避免各 Agent 重复计算：
      例：realtime_agent 获取的实时告警数据存于此，
          diagnosis_agent 直接读取而无需重新调用工具。
    这是一个非结构化的自由字段，由各 Agent 自行约定 key 命名。
    """
    execution_plan: dict[str, Any]
    """Planning 模块生成的结构化执行计划。

    存储 ExecutionPlan.model_dump(mode="json") 后的字典。
    不直接存 Pydantic 对象，避免 RedisSaver / checkpoint 序列化出问题。
    """

    plan_results: Annotated[dict[str, dict[str, Any]], operator.or_]
    """每个计划步骤的执行结果。

    key = step_id
    value = StepResult.model_dump(mode="json")
    """

    planning_events: Annotated[list[dict[str, Any]], operator.add]
    """Planning 执行过程事件日志。

    例如 plan_created、step_ready、step_started、step_success、step_failed。
    """

    planning_error: str
    """Planning 链路最近一次错误信息。"""

    planning_status: str
    """Planning 当前状态。
    常见值：
    planning / validating / running / completed / failed / waiting_human
    """
    current_plan_step: dict[str, Any]
    """当前并行 worker 正在执行的计划步骤。
    由 LangGraph Send 注入。
    每个并行 worker 拿到自己的 current_plan_step，
    避免多个 worker 争抢同一个 step。
    """    
    planning_instruction: str
    """当前计划步骤给 Agent 的系统级执行指令。

    与 user_input 不同：
    - user_input 保留用户原话
    - planning_instruction 说明当前 worker 要完成哪个计划步骤
    """
    dependency_outputs: dict[str, Any]
    """当前计划步骤依赖的前置步骤输出。

    key = step_id
    value = StepResult 或该步骤的简化输出
    """





    line: str
    """地铁线路编号，如 "1"、"3"、"11"。
    由 NER 实体提取节点从 user_input 中识别，用于过滤和定位运维数据。
    """

    station: str
    """车站名称，如 "赤沙站"、"体育西路"、"公园前"。
    与 line 配合使用，精确定位到具体站点。
    """

    system: str
    """通信系统名称。

    典型值： "集中告警"、"无线"、"骨干传输"、"电源"、"CCTV"、"PIS"。
    用于过滤对应专业的数据源和告警信息。
    """

    current_entities: Annotated[dict[str, Any], operator.or_]
    """当前识别到的所有实体集合。

    使用 operator.or_ 作为 reducer：多节点并发写入时取并集（dict 合并），
    后写入的同名 key 覆盖先写入的。

    可包含 line/station/system 之外的其他实体类型：
      - device_id：设备编号
      - alarm_id：告警 ID
      - time_range：时间范围（如"最近1小时"）
      - metric：监控指标名称

    由各实体提取节点分别写入，最终汇总为完整的实体画像。
    """

    current_round_id: str
    """当前轮次的全局唯一标识（UUID4）。

    由 context_node 在轮次开始时生成，贯穿整轮对话。
    用途：
      - HumanMessage 的 id 字段："{round_id}:human"
      - AIMessage 的 id 字段："{round_id}:assistant"
      - 压缩任务的 round_id 外键
      - 日志和调试中的轮次关联
    """

    current_round_number: int
    """当前轮次的序号，从 1 开始单调递增。

    由 context_node 调用 get_next_round_number() 计算：
      从 conversation_summaries 和 messages 中提取所有已有 round_number，
      取最大值 +1。确保跨归档/未归档消息统一编号。
    """





    tool_results: Annotated[dict[str, dict[str, Any]], operator.or_]
    """工具调用记录字典（中心黑板）。

    key = tool_call_id（每次工具调用的唯一标识），
    value = {
        "tool_name": "get_train_status",   # 工具函数名称
        "arguments": {"line": "3"},         # 调用参数
        "result": {"delay": "5min", ...},  # 工具返回数据
    }

    使用 operator.or_ 合并：多个 Agent 的工具调用结果取 dict 并集。
    供 ToolContextBuilder 读取最近 N 条（默认 3 条）作为后续 Agent 调用的上下文，
    避免重复调用相同的工具。
    """

    agents_output: Annotated[dict[str, str], operator.or_]
    """各 Agent 的输出汇总。

    key = Agent 名称（如 "realtime"、"diagnosis"、"knowledge"），
    value = 该 Agent 生成的自然语言回复。

    使用 operator.or_ 合并：各 Agent 独立写入各自的 key，互不覆盖。
    planning_aggregate_node 根据计划步骤结果生成 final_answer。
    record_assistant_message 将最终回答写入消息历史。
    """

    final_answer: str
    """最终返回给用户的回答文本。

    由 planning_aggregate_node 生成，随后由 trace_finalize_node 记录追踪摘要。
    这是 LangGraph 工作流的最终输出，并由 record_assistant_message 归档。
    """





    recalled_memories: list[dict]
    """本轮对话中从 ChromaDB 召回的用户长期记忆。

    由 memory_recall_node 在检测到记忆查询时从 ChromaDB 检索并填充。
    每条 dict 包含：
      - memory_id: 记忆唯一标识
      - content: 记忆正文
      - metadata: 元数据（category/created_at/current_value 等）
    """

    memory_saved: list[dict]
    """本轮对话中新保存的长期记忆条目。

    由 memory_curator_node 在每轮结束时将 MemoryRecord 序列化（model_dump）后写入。
    每条 dict 是 MemoryRecord.model_dump(mode="json") 的输出，
    包含完整的 memory_id/category/content/source/evidence/时间戳/价值评分等字段。
    """

    memory_ignored: list[str]
    """本轮中被判定为无长期价值而忽略的记忆忽略原因列表。

    由 memory_curator_node 写入，每个元素是 policy.evaluate_memory() 返回的
    reason 字符串，如：
      - "当前告警或临时状态不进入长期记忆"
      - "已有语义重复的长期记忆"
      - "记忆查询不写入长期记忆"
    """

    pending_memories: list[dict]
    """待用户确认的记忆候选。

    当 policy 检测到以下情况时，候选记忆暂存于此而非直接写入：
      - 包含敏感信息（身份证/密码/手机号等）
      - 与已有记忆冲突（replace / conflict）
      - 冲突检测暂时不可用

    每条 dict 是 PendingMemory.model_dump(mode="json") 的输出，
    包含 candidate/ existing_memory_id/ existing_content/ relation/ reason。
    需用户明确确认后才会调用 create_memory_record(confirmed=True) 写入。
    """

    memory_notifications: list[str]
    """需展示给用户的记忆相关通知。

    由 memory_curator_node 在每轮结束时生成，追加到 final_answer 的 [memory] 块中。
    如："已记住：用户偏好简短回答"、"发现 2 条长期记忆需要确认，目前尚未保存"。
    """

    rule_change_requested: bool
    """用户是否请求修改长期记忆的规则设置。

    由 policy.is_rule_change_request() 检测（同时包含动作词 + 规则目标词）。
    True 时整轮跳过记忆写入流程，将请求路由到专门的规则修改处理逻辑。
    """





    short_memory_stats: dict[str, Any]
    """短期记忆系统的运行时统计指标。

    由 memory_observer_node 在每轮对话中采集并写入，包含：
      - raw_message_count / raw_round_count / raw_tokens  (原始消息指标)
      - summary_count / summary_tokens                      (摘要指标)
      - total_tokens                                        (聚合指标)
      - queued_jobs / failed_jobs / compression_status     (压缩管线健康)

    用于监控面板展示、容量告警（total_tokens 接近上限时预警）、
    压缩管线异常诊断（failed_jobs 持续增长时排查 Ollama/Redis）。
    """

    context_allocations: Annotated[
        dict[str, dict[str, Any]],
        operator.or_,
    ]
    """各 Agent 的上下文预算分配账单。

    key = Agent 名称（如 "knowledge"、"diagnosis"），
    value = AgentContextBudgetManager.allocate() 的返回字典，包含：
      - history_tokens / rag_tokens / tool_tokens / agent_output_tokens  (各部配额)
      - unused_tokens / mandatory_overflow / history_shortfall           (告警字段)
      - dropped_tokens                                                   (各区域丢弃量)
      - selected_recent_rounds / selected_history_entries                (实际使用统计)

    使用 operator.or_ 合并：各 Agent 独立写入各自的预算账单 key。
    用于调试 token 分配策略和诊断上下文不足问题。
    """

    trace_id: str
    trace_started_at: str
    aggregate_span_id: str
    trace_summary: dict[str, Any]
    trace_artifact_uri: str
    evaluation_retrieval_contexts: Annotated[list[str], merge_evaluation_retrieval_contexts]




    skill_plan: dict[str, list[str]]
