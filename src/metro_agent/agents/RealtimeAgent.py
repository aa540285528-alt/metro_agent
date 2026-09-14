"""
Realtime Agent —— 实时告警查询 Agent
====================================
负责从用户问题中提取线路、车站、系统参数，调用模拟网管接口查询
实时告警信息。使用本地 Ollama Qwen2.5 7B，8K 上下文窗口。

与其他 Agent 的关键区别：
  - 使用 LangGraph create_react_agent 执行 ReAct 工具调用循环
  - ReAct 内部自动完成：LLM 判断 → 工具调用 → 工具观察 → 最终回答
  - 历史工具结果作为参数补全来源注入（但不作为实时结果）

两阶段上下文分配：
  决策阶段：不知道工具结果有多大 → 按 tool_max 请求最大配额
  最终阶段：已知实际结果大小 → 按真实大小重新分配，各调用按剩余预算均分

工作流概述：
  1. 注入历史工具结果（补全线路/车站/系统等缺失参数）
  2. create_react_agent 内部判断是否需要调用 query_alarm_tool
  3. 从 ReAct 消息中提取 ToolMessage，写回 state.tool_results
  4. 若 ReAct 漏调工具但参数完整，则使用确定性 fallback 查询
"""

import json
import re
from uuid import uuid4
from langchain.agents import create_agent
from langchain.messages import SystemMessage, HumanMessage, ToolMessage

from metro_agent.config import (
    AGENT_CONTEXT_PROFILES,
    build_Ollama_qwenLLM,
    REALTIME_AGENT_PROMPT,
    TOOL_CONTEXT_MAX_ITEMS,
    TOOL_CONTEXT_MAX_RESULT_CHARS,
)
from metro_agent.memory.short_term.agent_context_assembler import (
    AgentContextAssembler,
)
from metro_agent.tools.Realtime_tools import (
    query_alarm_tool,
)
from metro_agent.state import MetroAgentState
from metro_agent.observability.agent_usage import LLM_USAGE_RECORDS_KEY, capture_response_usages
from metro_agent.memory.short_term.tool_context_builder import (
    ToolContextBuilder,
)






realtime_agent_LLM= None


def get_realtime_agent_llm():
    global realtime_agent_LLM

    if realtime_agent_LLM is None:
        realtime_agent_LLM = build_Ollama_qwenLLM()

    return realtime_agent_LLM

realtime_context_assembler = AgentContextAssembler(
    agent_name="realtime"
)

realtime_tool_context_builder = ToolContextBuilder(
    max_items=TOOL_CONTEXT_MAX_ITEMS,
    max_result_chars=TOOL_CONTEXT_MAX_RESULT_CHARS,
)






def infer_alarm_query_from_text(text: str) -> dict | None:
    """从用户原文中确定性提取告警查询参数。

    这是 LLM tool_call 的兜底机制：当模型因为过度谨慎没有调用工具时，
    只要线路、站点、系统三项已经明确，就仍然允许代码层调用实时接口。
    """
    text = text or ""

    line_match = re.search(r"(\d+)\s*号线", text)
    station_search_text = text[line_match.end():] if line_match else text
    station_match = re.search(r"([\u4e00-\u9fa5A-Za-z]+站)", station_search_text)

    system = ""
    if "骨干" in text and "传输" in text:
        system = "骨干传输"
    elif "传输" in text:
        system = "骨干传输"
    elif "无线" in text:
        system = "无线"
    elif "集中告警" in text:
        system = "集中告警"
    elif "电源" in text:
        system = "电源"

    if not line_match or not station_match or not system:
        return None

    return {
        "line": line_match.group(1),
        "station": station_match.group(1),
        "system": system,
    }


def format_alarm_tool_result(result: dict) -> str:
    """将告警工具结果整理为稳定、简洁的实时状态回答。"""
    if not result.get("success"):
        error = result.get("error", "未知错误")
        return f"查询失败，接口返回异常：{error}"

    alarms = result.get("alarms", [])
    count = result.get("count", len(alarms))
    query = result.get("query", {})

    if count == 0:
        return (
            f"未查询到匹配的活动告警。"
            f"查询条件：{query.get('line', '')}号线、"
            f"{query.get('station', '')}、{query.get('system', '')}。"
        )

    lines = [f"当前查询到 {count} 条活动告警："]
    for alarm in alarms:
        lines.append(
            "- "
            f"告警编号：{alarm.get('alarm_id', '未知')}；"
            f"级别：{alarm.get('level', '未知')}；"
            f"设备：{alarm.get('device_id', '未知')}；"
            f"系统：{alarm.get('system', '未知')}；"
            f"内容：{alarm.get('message', '未知')}；"
            f"状态：{alarm.get('status', '未知')}。"
        )

    return "\n".join(lines)


def build_budgeted_tool_content(
    result: object,
    token_budget: int,
) -> str:
    """将工具调用结果按 token 预算截断，适配 LLM 上下文窗口。

    用于最终阶段：已知工具返回了实际数据，但 LLM 上下文剩余空间
    可能装不下完整结果。此时有两种策略：
      - 结果较小（≤ budget）→ 完整 JSON 传入
      - 结果过大（> budget）→ 截断为预览，标记 truncated=True

    截断开销：预留 50 token 给包装 JSON {"truncated":true,"result_preview":"..."}
    的壳子和 truncated 标记，剩余空间用于实际数据预览。

    Args:
        result: 工具返回的原始结果（通常为 dict 或 list）。
        token_budget: 分配给此条工具结果的 token 上限。

    Returns:
        str: 完整 JSON 字符串（未超预算）或截断预览 JSON（超预算）。
    """

    raw_text = json.dumps(
        result,
        ensure_ascii=False,
        default=str,
    )


    if realtime_context_assembler.count_text(
        raw_text
    ) <= token_budget:
        return raw_text



    preview = realtime_context_assembler.truncate_text(
        raw_text,
        max(0, token_budget - 50),
    )


    return json.dumps(
        {
            "truncated": True,
            "result_preview": preview,
        },
        ensure_ascii=False,
    )
_realtime_react_agent = None

def get_realtime_react_agent():
    global _realtime_react_agent

    if _realtime_react_agent is None:
        _realtime_react_agent = create_agent(
            model=build_Ollama_qwenLLM(),
            tools=[query_alarm_tool],
            system_prompt=REALTIME_AGENT_PROMPT,
        )

    return _realtime_react_agent


def extract_tool_records_from_react_messages(messages: list) -> dict:
    """从 create_react_agent 返回的消息中还原项目黑板格式的 tool_results。"""
    tool_calls_by_id = {}
    tool_records = {}

    for message in messages:
        for tool_call in getattr(message, "tool_calls", []) or []:
            tool_calls_by_id[tool_call["id"]] = tool_call

        if isinstance(message, ToolMessage):
            tool_call = tool_calls_by_id.get(message.tool_call_id, {})
            try:
                result = json.loads(message.content)
            except (TypeError, ValueError):
                result = message.content

            tool_records[message.tool_call_id] = {
                "tool_name": tool_call.get("name", "unknown_tool"),
                "arguments": tool_call.get("args", {}),
                "result": result,
                "source": "create_react_agent",
            }

    return tool_records










def realtime_agent(state: MetroAgentState) -> dict:
    """实时告警 Agent —— 用 LangGraph create_react_agent 执行工具调用循环。

    外层仍然适配项目的 MetroAgentState：
      - 输入：user_input / messages / tool_results
      - 输出：agents_output.realtime / tool_results / context_allocations

    内层由 create_react_agent 负责：
      LLM → tool_call → ToolNode → LLM → final answer
    """
    historical_tool_context = realtime_tool_context_builder.build_text(
        state.get("tool_results", {})
    )
    historical_tool_tokens = realtime_context_assembler.count_text(
        historical_tool_context
    )
    tool_max = AGENT_CONTEXT_PROFILES["realtime"]["tool_max"]

    assembly = realtime_context_assembler.build(
        state=state,
        system_prompt=REALTIME_AGENT_PROMPT,
        requested={
            "tool": max(historical_tool_tokens, tool_max),
        },
        mode="standard",
    )
    allocation = assembly["allocation"]

    limited_historical_tools = realtime_context_assembler.truncate_text(
        historical_tool_context,
        allocation["tool_tokens"],
    )

    context_messages = []
    if limited_historical_tools:
        context_messages.append(
            SystemMessage(
                content=(
                    "以下是历史工具结果，只能用于补全线路、站点、系统和"
                    "告警编号。查询当前状态时必须重新调用工具，不得把历史"
                    "结果当作实时结果：\n\n"
                    f"{limited_historical_tools}"
                )
            )
        )

    history_messages = assembly["messages"]
    if not history_messages:
        history_messages = [
            HumanMessage(content=state["user_input"])
        ]

    react_messages = [
        *context_messages,
        *history_messages,
    ]

    react_result = get_realtime_react_agent().invoke(
        {
            "messages": react_messages,
        }
    )
    react_result_messages = react_result.get("messages", [])
    answer = (
        str(react_result_messages[-1].content).strip()
        if react_result_messages
        else "实时状态查询失败，未得到模型回答。"
    )
    tool_records = extract_tool_records_from_react_messages(
        react_result_messages
    )


    if not tool_records:
        fallback_query = infer_alarm_query_from_text(state["user_input"])
        if fallback_query:
            tool_result = query_alarm_tool.invoke(fallback_query)
            tool_call_id = f"fallback_query_alarm_{uuid4().hex}"
            tool_records[tool_call_id] = {
                "tool_name": "query_alarm_tool",
                "arguments": fallback_query,
                "result": tool_result,
                "source": "deterministic_fallback",
            }
            answer = format_alarm_tool_result(tool_result)

    state_update = {
        "agents_output": {
            "realtime": answer,
        },
        "context_allocations": {
            "realtime": allocation,
        },
        LLM_USAGE_RECORDS_KEY: capture_response_usages(
            react_result,
            fallback_model="metro-ops-qwen2.5-7b",
            call_name="realtime.react",
        ),
    }

    if tool_records:
        state_update["tool_results"] = tool_records

    return state_update
