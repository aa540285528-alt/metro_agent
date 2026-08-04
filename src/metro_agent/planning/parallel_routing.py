"""
ParallelRouting —— DAG 并行路由节点
===================================
根据当前 ExecutionPlan 的依赖关系和已完成步骤的执行结果，
找出所有"就绪"的步骤（dependencies 全部满足），
通过 LangGraph 的 Send 机制将它们**并行派发**给 worker_node 执行。

这是 Planning 系统从"计划"到"执行"的关键桥梁——它将 DAG 的
拓扑排序逻辑转化为 LangGraph 的并行执行指令。

核心概念 —— 什么是 Ready Step：
  一个 PlanStep 被标记为 "ready" 当且仅当：
    1. 自身状态为 "pending"（尚未被执行过）
    2. dependencies 中的所有步骤都已 success（前置条件满足）

  例：step_3 依赖 [step_1, step_2]
      step_1 = success ✓
      step_2 = running   ← 未完成
      → step_3 不能进入 ready，必须等 step_2 完成

与 scheduler 的分工：
  - scheduler.get_ready_steps()：找出 ready 步骤并标记
  - 本模块 route_ready_steps()：将 ready 步骤包装为 Send 指令并行派发

在 LangGraph 工作流中的位置：
  planner_node → plan_validator_node → [本节点]
    → 有 ready steps → Send → planning_worker_node × N (并行)
    → 无 ready steps → planning_aggregate_node (汇总结果)

Send 机制简介（LangGraph 官方并行原语）：
  Send 是 LangGraph 中实现 Map-Reduce / Fan-out 的核心 API。
  返回 Send 对象列表 → 框架自动为每个 Send 创建一份独立的
  worker_node 副本并行执行，执行完毕后自动汇聚到目标节点。

  本例中每个 Send 携带：
    - 完整的 state（继承当前状态上下文）
    - current_plan_step：该 worker 需要执行的 PlanStep
"""

import sys
from pathlib import Path

# 将项目根目录加入 sys.path，以便导入顶层模块
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from langgraph.types import Send  # LangGraph 并行派发原语
from metro_agent.planning.models import ExecutionPlan, StepResult
from metro_agent.planning.scheduler import get_ready_steps  # 找出所有依赖已满足的步骤
from metro_agent.state import MetroAgentState


def route_ready_steps(state: MetroAgentState):
    """LangGraph 并行路由函数 —— 找出 ready 步骤并派发 Send。

    这个函数在每轮执行循环中被 LangGraph 的条件路由系统调用。
    它不是普通的节点（不返回 dict），而是返回一个**路由指令**
    告诉 LangGraph 下一步怎么做。

    三种可能的路由结果：

      情况 1：有 ready steps → 返回 Send 列表
        例：step_1 和 step_2 的依赖都已满足
        → 返回 [Send("planning_worker_node", {...step_1}),
                 Send("planning_worker_node", {...step_2})]
        → LangGraph 并行启动两个 worker_node 副本

      情况 2：无 ready steps 且全部完成 → 返回聚合节点
        → 返回 "planning_aggregate_node"
        → 进入结果汇总阶段

      情况 3：无 ready steps 但有 running/failed → 返回聚合节点
        → running 的步骤还没出结果，failed 的步骤不能继续
        → 返回 "planning_aggregate_node" 终结本轮调度

    Send 中的 state 传递：
      每个 Send 携带 state 的一份快照 + current_plan_step。
      这样 worker_node 收到的是一个"已知道要执行哪个步骤"
      的完整上下文，不需要再查 plan。

    Args:
        state: MetroAgent 全局状态，需包含：
               - execution_plan: dict（ExecutionPlan 的序列化结果）
               - plan_results: dict[str, dict]（已完成步骤的结果，
                 key=step_id, value=StepResult 的序列化结果）

    Returns:
        list[Send] | str:
          - Send 列表：有 ready steps，并行派发
          - 字符串 "planning_aggregate_node"：本轮没有可执行的步骤
    """
    # --------------------------------------------------------------
    # 步骤 1：从 state 反序列化计划与已完成结果
    # --------------------------------------------------------------

    plan_data = state.get("execution_plan", {})
    result_data = state.get("plan_results", {})

    # dict → ExecutionPlan Pydantic 对象
    plan = ExecutionPlan(**plan_data)

    # dict[str, dict] → dict[str, StepResult]
    # 将每个已完成步骤的结果反序列化为 StepResult 对象，
    # 以便 scheduler 通过 result.status 判断是否 success
    results = {
        step_id: StepResult(**result)
        for step_id, result in result_data.items()
    }

    # --------------------------------------------------------------
    # 步骤 2：调用 scheduler 找出所有 ready steps
    #
    # get_ready_steps() 内部逻辑（见 scheduler.py）：
    #   1. 收集所有 status="success" 的 step_id
    #   2. 遍历 plan.steps，跳过非 pending 状态的
    #   3. 检查 pending 步骤的 dependencies 是否全部在 success 集合中
    #   4. 满足者标记为 "ready" 并返回
    #
    # 副作用：被标记为 ready 的 PlanStep 的 status 会被原地修改为 "ready"
    # --------------------------------------------------------------
    ready_steps = get_ready_steps(
        plan=plan,
        results=results,
    )

    # --------------------------------------------------------------
    # 步骤 3：路由决策 —— 有就绪步骤则并行派发，否则进入聚合
    # --------------------------------------------------------------

    # 情况 1：无 ready steps（所有 pending 步骤都有未完成的依赖，
    #         或者所有步骤已执行完毕）
    if not ready_steps:
        # 直接返回目标节点名称字符串，告诉 LangGraph 跳过 worker，
        # 进入结果汇总阶段
        return "planning_aggregate_node"

    # 情况 2：有 ready steps → 为每个 ready step 构造一个 Send
    #
    # Send 的参数：
    #   第一个参数 "planning_worker_node"：目标节点名称
    #   第二个参数 dict：传递给目标节点的 state
    #     - **state 展开当前完整上下文（user_input、messages 等）
    #     - current_plan_step：该 worker 专属的 PlanStep（序列化为 dict）
    #
    # 每个 worker_node 收到 Send 后，从 state["current_plan_step"]
    # 读取自己要执行的步骤信息（step_id、agent、description 等），
    # 然后调用对应的 Agent 执行。
    return [
        Send(
            "planning_worker_node",
            {
                # 展开当前 state 的全部字段（继承上下文）
                **state,
                # 注入本次执行的步骤信息（worker_node 据此知道要做什么）
                "current_plan_step": step.model_dump(mode="json"),
            },
        )
        for step in ready_steps
    ]
