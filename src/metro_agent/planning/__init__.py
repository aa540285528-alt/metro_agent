"""
Planning 模块 —— 多步骤任务编排系统
==================================
将用户的复杂请求（如"查告警后诊断再生成报告"）自动拆解为
DAG 执行计划，按依赖顺序并行/串行调度 Agent 执行。

核心管线（Plan-and-Execute 模式）：

  planner_node          → LLM 将用户输入转化为 ExecutionPlan（DAG 步骤图）
  plan_validator_node   → 确定性校验（非空/步数/去重/agent/依赖/无环）
  route_ready_steps     → 找出依赖已满足的步骤，Send 并行派发
  planning_worker_node  → 适配 PlanStep → 调用对应 Agent 函数
  planning_aggregate_node → 汇总各步骤结果，生成最终回复

子模块一览：

  数据模型:
    models.py           — PlanStep / ExecutionPlan / StepResult / PlanEvent 等

  计划生成与校验:
    planner_node.py     — LLM 生成 ExecutionPlan
    plan_validator_node.py — 结构合法性校验（纯代码，不调 LLM）
    validator.py        — 校验规则实现（步数/去重/依赖/无环检测）

  调度与执行:
    scheduler.py        — DAG 依赖调度器（get_ready_steps）
    parallel_routing.py — LangGraph Send 并行派发
    worker_node.py      — 单步执行节点（适配 PlanStep → Agent 函数）
    aggregate_node.py   — 结果汇总节点

  适配与辅助:
    agent_adapter.py    — PlanStep → Agent 函数的胶水层
    factory.py          — 工厂函数（create_plan / create_event）
    debug_node.py       — 调试输出节点（打印计划状态）
"""

# ============================================================
# 数据模型（供外部直接引用，无需 from metro_agent.planning.models import ...）
# ============================================================
from metro_agent.planning.models import (
    ExecutionPlan,
    PlanStep,
    StepResult,
    StepError,
    PlanEvent,
    ReviewDecision,
    utc_now_iso,
)

# ============================================================
# 计划生成 & 校验节点（LangGraph 节点函数）
# ============================================================
from metro_agent.planning.planner_node import planner_node
from metro_agent.planning.plan_validator_node import plan_validator_node
from metro_agent.planning.validator import validate_plan, has_cycle

# ============================================================
# 调度 & 执行节点
# ============================================================
from metro_agent.planning.scheduler import get_ready_steps, get_success_step_ids
from metro_agent.planning.parallel_routing import route_ready_steps
from metro_agent.planning.worker_node import planning_worker_node, extract_step_output
from metro_agent.planning.aggregate_node import planning_aggregate_node

# ============================================================
# 适配 & 辅助
# ============================================================
from metro_agent.planning.agent_adapter import (
    run_plan_step_agent,
    build_dependency_outputs,
    AGENT_FUNCTIONS,
)
from metro_agent.planning.factory import create_plan, create_event
from metro_agent.planning.debug_node import planning_debug_end_node

# ============================================================
# 公开 API
# ============================================================
__all__ = [
    # —— 数据模型 ——
    "ExecutionPlan",
    "PlanStep",
    "StepResult",
    "StepError",
    "PlanEvent",
    "ReviewDecision",
    "utc_now_iso",
    # —— 管线节点 ——
    "planner_node",
    "plan_validator_node",
    "route_ready_steps",
    "planning_worker_node",
    "extract_step_output",
    "planning_aggregate_node",
    "planning_debug_end_node",
    # —— 调度器 ——
    "get_ready_steps",
    "get_success_step_ids",
    # —— 校验 ——
    "validate_plan",
    "has_cycle",
    # —— 适配器 ——
    "run_plan_step_agent",
    "build_dependency_outputs",
    "AGENT_FUNCTIONS",
    # —— 工厂函数 ——
    "create_plan",
    "create_event",
]
