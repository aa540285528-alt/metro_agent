"""
PlannerNode —— LLM 计划生成节点
===============================
调用 DeepSeek LLM 将用户的自然语言请求转化为结构化的执行计划
（ExecutionPlan），是 Planning 管线的入口节点。

完整的 Planning 管线：
  PlannerNode（本节点）→ PlanValidatorNode（结构校验）
    → ParallelRouting（DAG 并行派发）→ Worker（执行）
    → AggregateNode（结果汇总）

本节点的核心流程（5 阶段）：

  阶段 1 —— LLM 调用
    将 PLANNER_PROMPT + user_input 传给 DeepSeek，
    LLM 返回 JSON 格式的执行计划草案。

  阶段 2 —— JSON 解析
    尝试 json.loads() 解析 LLM 的原始输出。
    解析失败 → 创建 plan_parse_failed 事件并返回失败状态。

  阶段 3 —— PlanStep 转换
    将 JSON 中的每个 step 转为 Pydantic PlanStep 对象。
    自动处理默认值（dependencies 为空、status="pending" 等）。

  阶段 4 —— ExecutionPlan 创建
    调用 create_plan() 工厂函数，生成带 plan_id、
    created_at 时间戳的完整计划对象。

  阶段 5 —— 写回 state
    将 ExecutionPlan 序列化后写入 state["execution_plan"]，
    供下游 validator_node 消费。

LLM 返回的 JSON 格式约定：
  {
    "goal": "用户目标的自然语言描述",
    "steps": [
      {
        "step_id": "step_1",
        "description": "该步骤要做什么",
        "agent": "realtime_agent",
        "dependencies": [],
        "expected_output": "期望的输出描述"
      },
      ...
    ]
  }
"""

import sys
from pathlib import Path

# 将项目根目录加入 sys.path，以便导入 config / state 等顶层模块
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from langchain_core.exceptions import OutputParserException
from langchain_core.messages import HumanMessage, SystemMessage
from langchain_core.output_parsers import PydanticOutputParser
from metro_agent.config import PLANNER_PROMPT, build_Chat_DeepseekLLM
from metro_agent.observability.node_instrumentation import record_current_llm_usage, traced_node
from metro_agent.planning.factory import create_event, create_plan  # 工厂函数：构造 PlanEvent 和 ExecutionPlan
from metro_agent.planning.models import PlanStep, PlannerOutput      # 单个步骤的 Pydantic 数据模型
from metro_agent.state import MetroAgentState


@traced_node("planner")
def planner_node(state: MetroAgentState) -> dict:
    """LangGraph 节点：将用户输入转化为结构化的 ExecutionPlan。

    这是 Planning 管线的第一个节点，负责把用户的自然语言请求
    （如"查一下3号线赤沙站的告警，然后诊断一下"）转化为
    可执行的 DAG 步骤列表。

    执行流程（5 阶段）：

      阶段 1 —— LLM 调用
        system: PLANNER_PROMPT（从 config 注入的计划生成提示词）
        human:  state["user_input"]（用户原始输入）
        → DeepSeek 返回 JSON

      阶段 2 —— JSON 解析
        尝试 json.loads()，失败时进入错误分支。
        LLM 可能输出 Markdown 包裹的 JSON（```json ... ```），
        当前实现未做额外清理（假设 DeepSeek 遵守 prompt 约束）。

      阶段 3 —— PlanStep 转换
        遍历 raw_plan["steps"]，逐条转为 PlanStep 对象。
        未提供的可选字段使用 PlanStep 的默认值：
          - dependencies → []
          - status → "pending"
          - max_attempts → PLANNING_DEFAULT_MAX_ATTEMPTS (2)
          - timeout_seconds → PLANNING_DEFAULT_TIMEOUT_SECONDS (120)

      阶段 4 —— ExecutionPlan 创建
        create_plan() 工厂函数自动填充：
          - plan_id: UUID4
          - created_at / updated_at: 当前 UTC 时间戳
          - version: 1
          - status: "draft"

      阶段 5 —— 写回 state
        plan.model_dump(mode="json") 将 Pydantic 对象转为 dict，
        写回 state 供下游 validator_node 消费。

    错误处理：
      LLM 返回非法 JSON → plan_parse_failed 事件
        → planning_status="failed" + raw_content 保留在 metadata 中
        → 上层可根据 raw_content 重试或降级（如回退到单 Agent 模式）

    Args:
        state: MetroAgent 全局状态，需包含 user_input。

    Returns:
        dict: 写入 state 的字段：
          - execution_plan:   成功时返回（ExecutionPlan 的序列化 dict）
          - planning_status:  "planning"（成功）/ "failed"（失败）
          - planning_error:   失败时的错误信息
          - planning_events:  PlanEvent 列表（追踪计划生成过程）
    """
    # --------------------------------------------------------------
    # 阶段 1：LLM 调用
    # --------------------------------------------------------------
    planner_llm = build_Chat_DeepseekLLM()
    user_input = state["user_input"]
    parser = PydanticOutputParser(pydantic_object=PlannerOutput)

    messages = [
        SystemMessage(
            content=f"{PLANNER_PROMPT}\n\n{parser.get_format_instructions()}"
        ),
        HumanMessage(content=user_input),
    ]

    response = planner_llm.invoke(messages)
    record_current_llm_usage(response, fallback_model="planner")
    # LLM 返回的原始文本内容，去除首尾空白
    content = response.content.strip()

    # --------------------------------------------------------------
    # 阶段 2：结构化输出解析
    # --------------------------------------------------------------
    try:
        parsed_plan = parser.parse(content)
    except OutputParserException as exc:
        # LLM 没有遵守 JSON 输出约束——可能输出了 Markdown 包裹、
        # 纯文本解释、或格式有语法错误
        #
        # 创建 plan_parse_failed 事件记录：
        #   - event_type: 事件类型标识
        #   - message: 人类可读的失败描述
        #   - metadata.raw_content: 保留 LLM 原始输出，方便调试和重试
        event = create_event(
            event_type="plan_parse_failed",
            message="Planner 返回内容不是合法 JSON",
            metadata={"raw_content": content},
        )
        return {
            "planning_status": "failed",
            "planning_error": str(exc),
            "final_answer": "【计划生成失败】\n无法生成可执行计划，请稍后重试或换一种更明确的描述。",
            "planning_events": [event.model_dump(mode="json")],
        }

    # --------------------------------------------------------------
    # 阶段 3：JSON → PlanStep 对象列表
    #
    # 使用列表推导式遍历 LLM 返回的 steps 数组，
    # 逐条构造 PlanStep Pydantic 对象。
    #
    # step_id / description / agent / expected_output 是必填字段。
    # dependencies 通过 .get("dependencies", []) 处理——
    #   如果 LLM 没写 dependencies 字段，默认空列表（无依赖）。
    # PlanStep 的其他字段（status / max_attempts / timeout_seconds
    #   / requires_approval / input_refs）使用模型默认值。
    # --------------------------------------------------------------
    steps = [PlanStep(**item.model_dump()) for item in parsed_plan.steps]

    # --------------------------------------------------------------
    # 阶段 4：创建完整的 ExecutionPlan
    #
    # create_plan() 工厂函数（见 factory.py）：
    #   - 生成 UUID plan_id
    #   - 设置 created_at / updated_at 为当前 UTC 时间
    #   - version=1, status="draft"
    #
    # goal 取值：优先使用 LLM 返回的 goal 字段，
    #   如果 LLM 没写 goal（不应该，但做防御），回退到用户原始输入
    # --------------------------------------------------------------
    plan = create_plan(
        goal=parsed_plan.goal,
        steps=steps,
    )

    # --------------------------------------------------------------
    # 阶段 5：记录事件 & 写回 state
    # --------------------------------------------------------------

    # 创建计划生成成功的事件，记录目标描述和步骤数
    event = create_event(
        event_type="plan_created",
        message=f"Planner 创建计划：{plan.goal}",
        metadata={"step_count": len(plan.steps)},
    )

    return {
        # 将 ExecutionPlan Pydantic 对象序列化为 dict，
        # 使用 mode="json" 确保 datetime 等复杂类型正确转换
        "execution_plan": plan.model_dump(mode="json"),
        # planning_status="planning" 表示计划已生成但尚未校验/执行
        "planning_status": "planning",
        # 清空之前的错误（如果有）
        "planning_error": "",
        "planning_events": [event.model_dump(mode="json")],
    }
