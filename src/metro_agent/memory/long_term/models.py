"""
长期记忆系统 — 数据模型

定义记忆记录的数据结构（MemoryRecord）和工厂函数（create_memory_record）。
记忆系统用于跨会话复用用户信息，包括身份、偏好、计划、需求等。
"""

from datetime import date, datetime, timezone
from typing import Literal
from uuid import uuid4
from pydantic import BaseModel






MemoryCategory = Literal[
    "identity",
    "preference",
    "plan",
    "goal",
    "requirement",
    "explicit",
]




MemorySource = Literal[
    "explicit",
    "automatic",
]




MemorySensitivity = Literal[
    "normal",
    "sensitive",
]





MemoryStatus = Literal[
    "active",
    "superseded",
    "archived",
    "deleted",
]





INITIAL_IMPORTANCE = {
    "explicit":    0.90,
    "identity":    0.85,
    "requirement": 0.80,
    "goal":        0.80,
    "plan":        0.70,
    "preference":  0.60,
}







class MemoryRecord(BaseModel):
    """单条长期记忆的完整记录"""


    memory_id: str
    user_id: str


    category: MemoryCategory
    content: str
    source: MemorySource
    sensitivity: MemorySensitivity
    evidence: str


    created_at: datetime
    updated_at: datetime
    last_accessed_at: datetime | None = None


    access_count: int = 0
    retrieved_count: int = 0
    used_count: int = 0


    initial_value: float
    current_value: float
    importance: float
    utility: float = 0.0


    pinned: bool = False
    status: MemoryStatus = "active"
    version: int = 1


    target_date: date | None = None
    valid_until: date | None = None


    previous_id: str | None = None






def create_memory_record(
    user_id: str,
    category: MemoryCategory,
    content: str,
    source: MemorySource,
    sensitivity: MemorySensitivity,
    evidence: str,

    memory_id: str | None = None,
    now: datetime | None = None,
    pinned: bool = False,
    target_date: date | None = None,
    valid_until: date | None = None,
    previous_id: str | None = None,
    confirmed: bool = False,
) -> MemoryRecord:
    """
    创建一条新的记忆记录。

    自动处理：
    - 时间戳生成（created_at / updated_at 初始相同）
    - UUID 生成（未提供 memory_id 时）
    - 初始价值计算：importance + source_bonus（显式来源 +0.05），上限 1.0
    - pinned 记忆的 current_value 设为首值 1.0

    返回：填充完整的 MemoryRecord 实例，可直接存入数据库。
    """
    normalized_content = content.strip()
    normalized_evidence = evidence.strip()
    if not normalized_content:
        raise ValueError("长期记忆内容不能为空")
    if not normalized_evidence:
        raise ValueError("长期记忆证据不能为空")
    
    if sensitivity == "sensitive" and not confirmed:
        raise ValueError("敏感记忆必须经过用户确认")

    timestamp = now or datetime.now(timezone.utc)


    record_id = memory_id or str(uuid4())


    importance = INITIAL_IMPORTANCE[category]


    source_bonus = 0.05 if source == "explicit" else 0.0


    initial_value = min(importance + source_bonus, 1.0)


    current_value = 1.0 if pinned else initial_value

    return MemoryRecord(
        memory_id=record_id,
        user_id=user_id,
        category=category,
        content=normalized_content,
        source=source,
        sensitivity=sensitivity,
        evidence=normalized_evidence,
        created_at=timestamp,
        updated_at=timestamp,
        initial_value=initial_value,
        current_value=current_value,
        importance=importance,
        pinned=pinned,
        target_date=target_date,
        valid_until=valid_until,
        previous_id=previous_id,
    )

class MemoryCandidate(BaseModel):
    category: MemoryCategory
    content: str
    sensitivity: MemorySensitivity
    evidence: str
    target_date: date | None = None
    valid_until: date | None = None
