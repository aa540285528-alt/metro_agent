"""
AgentAdapter —— PlanStep → Agent 函数适配层
============================================
将 Planning 系统生成的 PlanStep 适配为现有 Agent 函数
（knowledge_agent / realtime_agent / diagnosis_agent / general_agent）
可以接受和执行的格式。

这是 Planning 系统和 Agent 系统之间的"胶水层"——它解决了
两个系统对 state 字段的命名约定不同的问题：

  Planning 系统使用：
    - current_plan_step: PlanStep dict（步骤元数据）
    - planning_instruction: str（给 Agent 的自然语言指令）
    - dependency_outputs: dict（前置步骤的输出结果）

  Agent 系统使用：
    - user_input: str（用户原始输入）
    - agents_output: dict（各 Agent 输出汇总）
    - tool_results: dict（工具调用中心黑板）

适配器的工作就是将这些 Planning 专属字段注入 state，
使得 Agent 函数无需修改即可在 Planning 模式下工作。

使用方式：
  worker_node 拿到 PlanStep 后，调用 run_plan_step_agent(state, step)
  即可自动路由到正确的 Agent 函数并传入适配后的 state。

Agent 函数注册表（AGENT_FUNCTIONS）：
  将 step.agent（LLM 规划出的 agent 名称字符串）映射到
  agents/ 目录下对应的 Agent 节点函数。
  新增 Agent 时只需在 AGENT_FUNCTIONS 中加一行即可接入 Planning 系统。
"""

import sys
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


from metro_agent.agents import (
    diagnosis_agent,
    general_agent,
    knowledge_agent,
    realtime_agent,
)
from metro_agent.planning.models import PlanStep
from metro_agent.state import MetroAgentState















AGENT_FUNCTIONS = {
    "knowledge_agent": knowledge_agent,
    "realtime_agent": realtime_agent,
    "diagnosis_agent": diagnosis_agent,
    "general_agent": general_agent,
}


def build_dependency_outputs(
    state: MetroAgentState,
    step: PlanStep,
) -> dict:
    """收集当前步骤依赖的前置步骤的输出结果。

    当 step_3 依赖 [step_1, step_2] 时，step_3 的 Agent 可能需要
    参考 step_1 和 step_2 的输出。此函数从 state["plan_results"]
    中提取这些前置结果，打包为 dict 注入 state 供 Agent 使用。

    例：
      step.dependencies = ["step_1", "step_2"]
      plan_results = {
          "step_1": StepResult(output="告警查询完成：3个活跃告警"),
          "step_2": StepResult(output="知识检索完成：匹配到2条SOP"),
      }
      → 返回 {
          "step_1": StepResult(...),
          "step_2": StepResult(...),
      }

    Args:
        state: 全局状态，需包含 plan_results。
        step: 当前要执行的 PlanStep。

    Returns:
        dict: key=dependency step_id, value=StepResult 对象。
              前置步骤尚未完成（不在 plan_results 中）时，不会
              出现在返回的 dict 中（不会报错，只是没数据可用）。
    """
    plan_results = state.get("plan_results", {})






    return {
        dep: plan_results[dep]
        for dep in step.dependencies
        if dep in plan_results
    }


def run_plan_step_agent(
    state: MetroAgentState,
    step: PlanStep,
) -> dict:
    """将 PlanStep 适配为 Agent 函数调用并执行。

    这是 Planning 系统中 worker_node 的核心执行逻辑。
    每步执行时做 4 件事：

      1. 查表：从 AGENT_FUNCTIONS 找到 step.agent 对应的函数
      2. 注入指令：将 step 的 description 和 expected_output
         组装为 planning_instruction，Agent 据此知道"该做什么"
      3. 注入依赖：从 plan_results 提取前置步骤的输出
      4. 调用 Agent：将适配后的 state 传入 Agent 函数

    注入的字段说明：
      - current_plan_step: PlanStep 的完整序列化 dict，
          Agent 可以从中读取 step_id、description、dependencies 等
      - planning_instruction: 自然语言指令，告诉 Agent 当前步骤
         的目标和期望输出格式
      - dependency_outputs: 前置步骤的输出集合，Agent 可据此避免
         重复工作（如 step_1 已经查了告警，step_2 直接消费结果）

    Args:
        state: 全局状态（会被复制为新的 dict，不修改原始 state）。
        step: 当前要执行的 PlanStep。

    Returns:
        dict: Agent 函数的原始返回值，通常包含：
              - agents_output: {agent_name: answer}
              - context_allocations: {agent_name: allocation}
              - tool_results: dict (如有工具调用)

    Raises:
        ValueError: step.agent 不在 AGENT_FUNCTIONS 注册表中。
                    这通常说明 Planner LLM 生成了不合法的 agent 名称。
    """

    agent_func = AGENT_FUNCTIONS.get(step.agent)

    if agent_func is None:
        raise ValueError(
            f"未知 Agent: {step.agent}"
        )




    step_state = dict(state)


    step_state["current_plan_step"] = step.model_dump(mode="json")


    step_state["planning_instruction"] = (
        f"当前计划步骤：{step.description}\n"
        f"期望输出：{step.expected_output}"
    )


    step_state["dependency_outputs"] = build_dependency_outputs(
        state=state,
        step=step,
    )


    return agent_func(step_state)
