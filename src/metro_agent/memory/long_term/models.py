"""
长期记忆系统 — 数据模型

定义记忆记录的数据结构（MemoryRecord）和工厂函数（create_memory_record）。
记忆系统用于跨会话复用用户信息，包括身份、偏好、计划、需求等。
"""

from datetime import date, datetime, timezone
from typing import Literal
from uuid import uuid4
from pydantic import BaseModel


# ============================================================
# 记忆类别
# 分类存储不同类型的长期记忆，用于检索优先级和价值计算
# ============================================================
MemoryCategory = Literal[
    "identity",    # 身份记忆：用户姓名、角色、所属组织等
    "preference",  # 偏好记忆：用户喜欢/不喜欢的方式（语言、风格等）
    "plan",        # 用户计划：用户正在进行的任务或计划
    "goal",        # 用户目标：长期目标或意图
    "requirement", # 用户需求：明确提出的跨会话需求
    "explicit",    # 显式记忆：用户明确要求记住的内容（优先级最高）
]

# ============================================================
# 记忆来源
# ============================================================
MemorySource = Literal[
    "explicit",   # 显式创建：用户明确说"记住这个"
    "automatic",  # 自动提取：系统从对话中自动识别并提取
]

# ============================================================
# 记忆敏感度
# ============================================================
MemorySensitivity = Literal[
    "normal",     # 普通敏感度，常规存储
    "sensitive",  # 敏感信息，需要额外保护（加密、脱敏等）
]

# ============================================================
# 记忆状态
# 用于记忆的生命周期管理
# ============================================================
MemoryStatus = Literal[
    "active",      # 活跃：正常使用中
    "superseded",  # 已取代：被新版本记忆覆盖（通过 previous_id 链追溯）
    "archived",    # 已归档：不再活跃但保留以备查
    "deleted",     # 已删除：逻辑删除，不再参与检索
]

# ============================================================
# 记忆类别 → 初始重要性映射
# 不同类别有不同的基础权重，影响记忆的检索排序和留存策略
# ============================================================
INITIAL_IMPORTANCE = {
    "explicit":    0.90,  # 用户明确要求记住 → 最高优先级
    "identity":    0.85,  # 用户身份信息
    "requirement": 0.80,  # 长期要求
    "goal":        0.80,  # 长期目标
    "plan":        0.70,  # 用户计划（时效性较强，略低）
    "preference":  0.60,  # 普通偏好（可塑性较大，最低）
}



# ============================================================
# 记忆记录数据模型
# 每条记忆包含内容、元数据、统计信息和价值评分
# ============================================================
class MemoryRecord(BaseModel):
    """单条长期记忆的完整记录"""

    # ---- 核心标识 ----
    memory_id: str        # 记忆唯一标识（UUID）
    user_id: str          # 所属用户标识

    # ---- 内容与分类 ----
    category: MemoryCategory  # 记忆类别（identity/preference/plan/goal/requirement/explicit）
    content: str              # 记忆正文内容
    source: MemorySource      # 来源（explicit=用户显式 / automatic=系统自动提取）
    sensitivity: MemorySensitivity  # 敏感度等级
    evidence: str             # 证据：提取该记忆时依据的用户原话

    # ---- 时间戳（UTC） ----
    created_at: datetime                      # 创建时间
    updated_at: datetime                      # 最后更新时间
    last_accessed_at: datetime | None = None  # 最后被检索/访问的时间

    # ---- 统计信息 ----
    access_count: int = 0    # 被检索到的总次数（含未使用的）
    retrieved_count: int = 0  # 被系统检索并放入上下文的次数
    used_count: int = 0       # 实际被用于生成回答的次数

    # ---- 价值评分（0.0 ~ 1.0） ----
    initial_value: float  # 初始价值 = importance + source_bonus（显式来源+0.05），上限 1.0
    current_value: float  # 当前价值，随时间和使用情况衰减或提升
    importance: float     # 基础重要性（来自 INITIAL_IMPORTANCE 表）
    utility: float = 0.0  # 效用性评分（根据使用频率和结果反馈动态调整）

    # ---- 状态与版本 ----
    pinned: bool = False                # 是否置顶（置顶记忆 initial_value 设为 1.0）
    status: MemoryStatus = "active"     # 生命周期状态
    version: int = 1                    # 版本号（每次更新递增）

    # ---- 时间约束 ----
    target_date: date | None = None   # 目标日期（计划/目标类记忆的截止时间）
    valid_until: date | None = None   # 有效期截止日期（过期后自动转为 archived）

    # ---- 版本链 ----
    previous_id: str | None = None    # 上一版本记忆 ID（被 replace 时建立版本链）


# ============================================================
# 工厂函数：创建记忆记录
# 自动填充时间戳、ID、价值评分等字段，确保创建逻辑一致
# ============================================================
def create_memory_record(
    user_id: str,
    category: MemoryCategory,
    content: str,
    source: MemorySource,
    sensitivity: MemorySensitivity,
    evidence: str,
    # ---- 可选参数 ----
    memory_id: str | None = None,     # 不提供则自动生成 UUID
    now: datetime | None = None,      # 不提供则使用当前 UTC 时间
    pinned: bool = False,             # 是否置顶
    target_date: date | None = None,  # 计划/目标的目标日期
    valid_until: date | None = None,  # 有效期截止日期
    previous_id: str | None = None,   # 被替换的旧记忆 ID
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
    # 时间戳：默认当前 UTC 时间
    timestamp = now or datetime.now(timezone.utc)

    # ID：不提供则自动生成 UUID4
    record_id = memory_id or str(uuid4())

    # 基础重要性：根据类别查表
    importance = INITIAL_IMPORTANCE[category]

    # 来源加成：显式记忆比自动提取更有价值
    source_bonus = 0.05 if source == "explicit" else 0.0

    # 初始价值 = 重要性 + 来源加成，上限 1.0
    initial_value = min(importance + source_bonus, 1.0)

    # 置顶记忆的当前价值设为 1.0（满分），否则用初始价值
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
