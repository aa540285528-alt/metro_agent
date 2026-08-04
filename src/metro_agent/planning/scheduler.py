"""
Scheduler —— DAG 依赖调度器
===========================
负责根据 ExecutionPlan 的依赖图和已完成的 StepResult 集合，
找出所有"就绪"的步骤（dependencies 全部满足），
供 ParallelRouting 并行派发给 worker_node 执行。

这是 Planning 系统的调度核心——它把 DAG 的拓扑依赖关系
转化为"哪些步骤可以现在执行"的决策。

核心规则（两条，缺一不可）：
  1. 步骤自身状态为 "pending"（尚未执行过）
  2. 步骤声明的所有 dependencies 都已经 success（前置条件全部满足）

  两个条件都满足时，调度器将步骤标记为 "ready" 并返回。

状态流转（单步视角）：
  pending ──依赖全部满足──→ ready ──worker执行──→ running
    → 成功 → success / 失败 → failed / 需人工 → waiting_human

与 ParallelRouting 的分工：
  - 本模块：纯逻辑判断（不涉及 LangGraph）
  - parallel_routing.py：调用本模块 + 封装 Send 指令

设计原则：
  - 确定性：所有判断基于 step.status 和 result.status 的状态值，
    不依赖 LLM 或外部服务，结果完全可预测
  - 幂等性：同一轮调用中每个 ready 步骤只返回一次
    （step.status 从 "pending" 改为 "ready" 后不再重复返回）
"""

import sys
from pathlib import Path

# 将项目根目录加入 sys.path，以便导入 planning 模块
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from metro_agent.planning.models import ExecutionPlan, PlanStep, StepResult


def get_success_step_ids(
    results: dict[str, StepResult],
) -> set[str]:
    """提取可供下游步骤消费的已完成步骤 ID。

    ``degraded`` 表示步骤已结束，但其结果存在可靠性限制。下游 Agent
    仍应执行，并通过 dependency_outputs 获知这一限制；否则依赖该步骤的
    DAG 会永久无法就绪。

    这是一个纯函数——输入相同的结果集合，输出始终相同。
    用于 get_ready_steps() 判断依赖是否已满足。

    Args:
        results: dict，key=step_id，value=StepResult 对象。
                 来自 state["plan_results"] 的反序列化结果。

    Returns:
        set[str]: 所有执行成功的 step_id 集合。
                  空 dict 输入 → 空 set（没有步骤成功 = 无依赖被满足）。
    """
    return {
        step_id
        for step_id, result in results.items()
        if result.status in {"success", "degraded"}
    }


def get_ready_steps(
    plan: ExecutionPlan,
    results: dict[str, StepResult],
) -> list[PlanStep]:
    """从执行计划中找出所有当前可执行的 ready 步骤。

    判断逻辑（对每个 pending 步骤执行）：

      1. 跳过非 pending 状态的步骤
         - ready / running → 已被本函数处理过或在执行中
         - success / failed / waiting_human → 已终态
         - skipped / cancelled → 已结束

      2. 检查该步骤的所有 dependencies
         - 使用 all() 确保每一个依赖步骤都在 success 集合中
         - 空依赖列表（无依赖的步骤）→ all([]) = True，直接 ready

      3. 将满足条件的步骤标记为 "ready"
         - 这是原地修改（副作用），防止同一轮被重复选出
         - 下游 parallel_routing 拿到 ready 列表后直接派发

    例：
      plan.steps = [step_1(deps=[]), step_2(deps=[step_1]), step_3(deps=[step_1])]
      results    = {step_1: success}
      → step_1 已 success，不再是 pending → 跳过
      → step_2 依赖 [step_1]，step_1 在 success 集合中 → ready
      → step_3 依赖 [step_1]，step_1 在 success 集合中 → ready
      → 返回 [step_2, step_3]（两个可以并行执行）

    Args:
        plan: 完整的 ExecutionPlan 对象（含 steps 列表）。
        results: 已完成步骤的结果集合。

    Returns:
        list[PlanStep]: 可以立即执行的步骤列表。
                        无 ready 步骤时返回空列表。
    """
    # 收集所有已成功步骤的 ID
    completed_dependency_step_ids = get_success_step_ids(results)

    ready_steps: list[PlanStep] = []

    for step in plan.steps:
        # ---- 条件 1：步骤必须处于 pending 状态 ----
        # 如果步骤已经是 ready/running/success/failed 等，
        # 说明它已经被处理过，不应再次调度
        if step.step_id in results:
            continue
        if step.status != "pending":
            continue

        # ---- 条件 2：所有依赖步骤必须已完成且可供消费 ----
        # all() 对空列表返回 True：
        #   无依赖的步骤（step.dependencies == []）直接视为就绪
        dependencies_done = all(
            dep in completed_dependency_step_ids
            for dep in step.dependencies
        )

        if dependencies_done:
            # 原地修改状态：防止下一轮调用 get_ready_steps 时
            # 该步骤再次被选中（幂等性保证）
            step.status = "ready"
            ready_steps.append(step)

    return ready_steps
