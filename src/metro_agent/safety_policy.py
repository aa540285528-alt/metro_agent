from __future__ import annotations


def safety_refusal_response(user_input: str) -> str | None:
    """Return a stable refusal for requests that must not reach an operational agent."""
    normalized = user_input.strip()
    if "密码" in normalized:
        return "为保障账号安全，无法提供设备登录密码。请通过经授权的账号管理流程申请或重置凭据。"
    if "删除" in normalized and "告警" in normalized:
        return "为避免影响故障追溯，无法直接执行删除告警操作。请按授权流程由具备权限的人员核实后处理。"
    if "配置命令" in normalized and any(keyword in normalized for keyword in ("基站", "设备", "传输", "直接")):
        return "为避免未经验证的变更风险，无法提供可直接执行的基站配置命令。请通过授权变更流程获取已审核的操作方案。"
    return None


def is_high_risk_safety_request(user_input: str) -> bool:
    return safety_refusal_response(user_input) is not None


def is_diagnosis_request(user_input: str) -> bool:
    normalized = user_input.strip()
    diagnosis_terms = (
        "故障", "异常", "排查", "定位", "恢复", "怎么处理", "如何处理",
        "如何检查", "丢包", "衰减", "过载", "丢失",
    )
    return any(term in normalized for term in diagnosis_terms)
