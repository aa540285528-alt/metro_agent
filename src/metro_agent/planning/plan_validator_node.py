"""
PlanValidatorNode —— 计划校验节点
=================================
在 Planner 生成 ExecutionPlan 之后、Scheduler 开始执行之前，
对计划做确定性校验（纯代码，不调 LLM），确保 DAG 计划合法。

这是 Planning 管线的第二道关卡——第一道是 Planner 的 LLM 生成，
本节点只做刚性检查，不关心计划
的"质量好坏"，只关心"能否安全执行"。

校验内容（由 validator.validate_plan 执行）：
  1. 非空检查 —— 计划至少包含一个步骤
  2. 步数上界 —— 不能超过 PLANNING_MAX_STEPS（默认 12）
  3. 唯一性检查 —— step_id 不能重复
  4. 合法 Agent 检查 —— 每个步骤的 agent 必须在 PLANNING_ALLOWED_AGENTS 中
  5. 依赖存在性 —— dependencies 中引用的 step_id 必须真实存在
  6. 无循环依赖 —— DAG 中不允许出现环（DFS 检测）

在 LangGraph 工作流中的位置：
  Planner 生成计划 → PlanValidatorNode（本节点）→ Scheduler 执行
  如果校验失败 → 直接返回失败标记，不进入执行阶段
"""

import sys
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from metro_agent.planning.factory import create_event
from metro_agent.planning.models import ExecutionPlan
from metro_agent.planning.validator import validate_plan
from pydantic import ValidationError
from metro_agent.observability.node_instrumentation import traced_node
from metro_agent.state import MetroAgentState


@traced_node("plan_validator_node")
def plan_validator_node(state: MetroAgentState) -> dict:
    """LangGraph 节点：对 state 中的 execution_plan 进行结构合法性校验。

    执行流程（3 个分支）：

      分支 A —— 计划缺失：
        state 中没有 execution_plan 字段或值为空。
        → 创建 plan_validation_failed 事件
        → 返回 planning_status="failed"

      分支 B —— 校验不通过：
        validate_plan() 返回非空错误列表（如循环依赖、非法 agent 等）。
        → 创建 plan_validation_failed 事件（附带具体错误列表）
        → 返回 planning_status="failed" + planning_error

      分支 C —— 校验通过：
        所有 6 项检查均通过。
        → 创建 plan_validated 事件
        → 将 plan.status 从 "draft" 改为 "validated"
        → 返回完整的 execution_plan + planning_status="validated"

    Args:
        state: MetroAgent 全局状态，需包含 execution_plan 键。
               execution_plan 是由 planner_node 生成的 ExecutionPlan 的
               model_dump(mode="json") 序列化结果。

    Returns:
        dict: 写入 state 的字段：
          - execution_plan:  校验通过时返回（status 已改为 "validated"）
          - planning_status: "validated" | "failed"
          - planning_error:  失败时的错误信息（多行文本，\\n 分隔）
          - planning_events: PlanEvent 列表（追踪校验过程）
    """





    plan_data = state.get("execution_plan")





    if not plan_data:
        event = create_event(
            event_type="plan_validation_failed",
            message="state 中不存在 execution_plan",
        )
        return {
            "planning_status": "failed",
            "planning_error": "缺少 execution_plan",
            "final_answer": "【计划校验失败】\n当前未生成可执行计划，请稍后重试。",
            "planning_events": [event.model_dump(mode="json")],
        }







    try:
        plan = ExecutionPlan(**plan_data)
    except ValidationError as exc:
        event = create_event(
            event_type="plan_validation_failed",
            message="execution_plan 结构不合法",
            metadata={"errors": exc.errors()},
        )
        return {
            "planning_status": "failed",
            "planning_error": str(exc),
            "final_answer": "【计划校验失败】\n生成的计划结构不完整，请稍后重试。",
            "planning_events": [event.model_dump(mode="json")],
        }














    errors = validate_plan(plan)











    if errors:
        event = create_event(
            event_type="plan_validation_failed",
            message="计划校验失败",
            metadata={"errors": errors},
        )
        return {
            "planning_status": "failed",

            "planning_error": "\n".join(errors),
            "final_answer": "【计划校验失败】\n生成的计划不满足执行规则，请稍后重试。",
            "planning_events": [event.model_dump(mode="json")],
        }










    event = create_event(
        event_type="plan_validated",
        message="计划校验通过",
        metadata={"step_count": len(plan.steps)},
    )



    plan.status = "validated"

    return {

        "execution_plan": plan.model_dump(mode="json"),
        "planning_status": "validated",

        "planning_error": "",
        "planning_events": [event.model_dump(mode="json")],
    }
