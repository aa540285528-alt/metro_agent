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


sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from langchain_core.exceptions import OutputParserException
from langchain_core.messages import HumanMessage, SystemMessage
from langchain_core.output_parsers import PydanticOutputParser
from metro_agent.config import PLANNER_PROMPT, build_Chat_DeepseekLLM
from metro_agent.observability.node_instrumentation import record_current_llm_usage, traced_node
from metro_agent.planning.factory import create_event, create_plan
from metro_agent.planning.models import PlanStep, PlannerOutput
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

    content = response.content.strip()




    try:
        parsed_plan = parser.parse(content)
    except OutputParserException as exc:







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













    steps = [PlanStep(**item.model_dump()) for item in parsed_plan.steps]












    plan = create_plan(
        goal=parsed_plan.goal,
        steps=steps,
    )






    event = create_event(
        event_type="plan_created",
        message=f"Planner 创建计划：{plan.goal}",
        metadata={"step_count": len(plan.steps)},
    )

    return {


        "execution_plan": plan.model_dump(mode="json"),

        "planning_status": "planning",

        "planning_error": "",
        "planning_events": [event.model_dump(mode="json")],
    }
