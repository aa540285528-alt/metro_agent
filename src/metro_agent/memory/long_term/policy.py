"""
记忆策略层（Policy）
===================
根据规则对记忆候选项进行分流裁决，决定每条候选项应该：
  - auto_save        → 直接写入
  - ask_confirmation  → 需要用户确认
  - ignore           → 丢弃
  - route_rule_change → 路由到规则修改流程

裁决优先级（从上到下依次判断，命中即返回）：
  1. 规则修改请求 → route_rule_change
  2. 临时状态     → ignore
  3. 敏感信息     → ask_confirmation
  4. 语义重复     → ignore（duplicate）
  5. 替换/冲突    → ask_confirmation（replace / conflict）
  6. 默认通过     → auto_save

设计原则：
  - 关键词匹配做快速初筛（省 LLM 调用），冲突检测结果（relation）做精细判断
  - 每条辅助函数独立可测，不依赖外部状态
"""

import sys
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dataclasses import dataclass
from typing import Literal
from metro_agent.memory.long_term.models import MemoryCandidate






MemoryAction = Literal[
    "auto_save",
    "ask_confirmation",
    "ignore",
    "route_rule_change",
]





@dataclass(frozen=True)
class PolicyDecision:
    """策略裁决结果，不可变"""

    action: MemoryAction
    reason: str








TRANSIENT_TIME_WORDS = (
    "当前",
    "实时",
    "现在",
    "此刻",
)


TRANSIENT_STATE_WORDS = (
    "告警",
    "设备状态",
    "链路状态",
    "接口状态",
    "在线状态",
)


RULE_ACTION_WORDS = (
    "写入",
    "添加",
    "修改",
    "更新",
    "删除",
)


RULE_TARGET_WORDS = (
    "规则",
    "规则文件",
    "Metro_Agent_Rules.md",
)


SENSITIVE_WORDS = (
    "员工编号",
    "工号",
    "身份证",
    "手机号",
    "电话号码",
    "账号",
    "密码",
    "口令",
    "token",
    "密钥",
    "邮箱",
    "微信号",
    "银行卡号",
    "家庭住址",
    "家庭成员",
    "生日",
)







def is_rule_change_request(user_text: str) -> bool:
    """
    检测用户是否在请求修改系统规则文件。

    匹配逻辑：同时包含动作词（写入/修改/删除...）和目标词（规则/规则文件...）。
    仅在 user_text 中检测（不在 candidate.content 中检测），
    因为规则修改是用户意图，不是记忆内容。
    """
    return (
        any(word in user_text for word in RULE_ACTION_WORDS)
        and any(word in user_text for word in RULE_TARGET_WORDS)
    )


def is_transient_memory(text: str) -> bool:
    """
    检测文本是否描述临时/瞬时状态。

    匹配逻辑：同时包含时间词（"当前"等）和状态词（"告警"等）。
    例如"当前告警"、"实时设备状态"——这些不应跨会话保留。
    """
    has_time = any(word in text for word in TRANSIENT_TIME_WORDS)
    has_state = any(word in text for word in TRANSIENT_STATE_WORDS)
    return has_time and has_state


def is_sensitive_memory(
    text: str,
    candidate: MemoryCandidate,
) -> bool:
    """
    检测记忆是否包含敏感信息。

    两种触发条件（满足任一即视为敏感）：
      1. MemoryCandidate 自身标记为 sensitivity="sensitive"
      2. 文本中出现敏感关键词（员工编号、身份证、密码等）
    """
    if candidate.sensitivity == "sensitive":
        return True
    return any(word.lower() in text.lower() for word in SENSITIVE_WORDS)





def evaluate_memory(
    *,
    user_text: str,
    candidate: MemoryCandidate,
    relation: str | None = None,
) -> PolicyDecision:
    """
    对单条候选记忆进行策略裁决。

    判断规则（按优先级，命中即返回）：
      1. 规则修改 → is_rule_change_request(user_text)
      2. 临时状态 → is_transient_memory(combined_text)
      3. 敏感信息 → is_sensitive_memory(combined_text, candidate)
      4. 语义重复 → relation == "duplicate" → ignore
      5. 替换冲突 → relation in {"replace", "conflict"} → ask_confirmation
      6. 默认通过 → auto_save

    参数：
      user_text:  用户原话，规则修改只用此项检测（用户意图不应混入记忆内容）
      candidate:  从对话中提取的记忆候选项
      relation:   与已有记忆的关系，由 conflict_detector.detect_conflict() 返回
    """

    combined_text = f"{user_text}\n{candidate.content}"


    if is_rule_change_request(user_text):
        return PolicyDecision(
            action="route_rule_change",
            reason="检测到规则修改请求，必须由用户确认后处理",
        )


    if is_transient_memory(combined_text):
        return PolicyDecision(
            action="ignore",
            reason="当前告警或临时状态不进入长期记忆",
        )


    if is_sensitive_memory(combined_text, candidate):
        return PolicyDecision(
            action="ask_confirmation",
            reason="记忆包含敏感信息，需要用户确认",
        )


    if relation == "duplicate":
        return PolicyDecision(
            action="ignore",
            reason="已有语义重复的长期记忆",
        )


    if relation in {"replace", "conflict"}:
        return PolicyDecision(
            action="ask_confirmation",
            reason="新记忆可能替换或冲突已有记忆",
        )


    return PolicyDecision(
        action="auto_save",
        reason="普通且无冲突的长期记忆",
    )
