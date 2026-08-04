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

# 将项目根目录加入 sys.path，以便导入 config / state 等顶层模块
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from metro_agent.planning.factory import create_event     # 构造 PlanEvent 记录校验结果
from metro_agent.planning.models import ExecutionPlan    # 计划的 Pydantic 数据模型
from metro_agent.planning.validator import validate_plan # 确定性校验函数（纯 Python，不调 LLM）
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
    # --------------------------------------------------------------
    # 从 state 中提取计划数据
    # state["execution_plan"] 是 planner_node 写入的 dict，
    # 需要用 ExecutionPlan(**plan_data) 反序列化为 Pydantic 模型
    # --------------------------------------------------------------
    plan_data = state.get("execution_plan")

    # ==============================================================
    # 分支 A：计划缺失 —— state 中根本没有 execution_plan
    # 可能原因：planner_node 在上一步执行失败或未执行
    # ==============================================================
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

    # --------------------------------------------------------------
    # 反序列化：dict → ExecutionPlan Pydantic 对象
    # 如果 plan_data 中的字段与 ExecutionPlan schema 不匹配，
    # Pydantic 会在此处抛出 ValidationError（预期外的情况，
    # 因为 planner_node 的 create_plan 应保证 schema 合法）
    # --------------------------------------------------------------
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

    # ==============================================================
    # 调用 validator 进行 6 项确定性检查
    #
    # validate_plan() 内部逻辑（见 validator.py）：
    #   1. 空计划检查
    #   2. 步骤数上限检查
    #   3. step_id 重复检查
    #   4. agent 合法性检查
    #   5. 依赖存在性检查
    #   6. 循环依赖检查（DFS 检测环）
    #
    # 全部是纯 Python 逻辑，不调 LLM，延迟可忽略
    # ==============================================================
    errors = validate_plan(plan)

    # ==============================================================
    # 分支 B：校验不通过 —— 计划存在结构性问题
    #
    # errors 示例：
    #   ["步骤 step_3 使用了不允许的 agent: unknown_agent",
    #    "步骤 step_5 依赖了不存在的步骤: step_99",
    #    "计划存在循环依赖"]
    #
    # 这些错误需要返回给上游（planner_node）进行修正
    # ==============================================================
    if errors:
        event = create_event(
            event_type="plan_validation_failed",
            message="计划校验失败",
            metadata={"errors": errors},  # 将错误列表附加到事件元数据中
        )
        return {
            "planning_status": "failed",
            # 多行错误用 \n 拼接，方便日志阅读和 LLM 分析
            "planning_error": "\n".join(errors),
            "final_answer": "【计划校验失败】\n生成的计划不满足执行规则，请稍后重试。",
            "planning_events": [event.model_dump(mode="json")],
        }

    # ==============================================================
    # 分支 C：校验通过 —— 将计划从 draft 状态推进到 validated
    #
    # validated 状态意味着：
    #   - DAG 结构合法（无循环、无缺失依赖）
    #   - 所有 agent 在白名单内
    #   - 步数在合理范围内
    #   - 可以安全地交给 scheduler_node 执行
    # ==============================================================
    event = create_event(
        event_type="plan_validated",
        message="计划校验通过",
        metadata={"step_count": len(plan.steps)},  # 记录步骤数便于监控
    )

    # 更新计划状态：draft → validated
    # 这是 ExecutionPlan 生命周期中的第一次状态迁移
    plan.status = "validated"

    return {
        # 返回完整计划（status 已更新），覆盖 state 中的旧版本
        "execution_plan": plan.model_dump(mode="json"),
        "planning_status": "validated",
        # 清空之前的错误信息（如果有的话）
        "planning_error": "",
        "planning_events": [event.model_dump(mode="json")],
    }
