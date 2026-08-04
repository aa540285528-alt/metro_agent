"""
记忆冲突检测
============

功能：
  判断新候选记忆与已有记忆之间的关系类型，支持两级检测：
  1. 向量相似度快速初筛  → 高相似度直接判为 duplicate，避免 LLM 调用
  2. LLM 结构化判定       → 相似度不足时，由 LLM 细粒度判定五种关系

五种关系类型：
  duplicate   — 语义基本相同，去重合并
  complement  — 内容不同但可同时成立，双写保留
  replace     — 用户明确替换旧要求，覆盖旧记忆
  conflict    — 两者矛盾但用户未明确取舍，需人工裁决
  unrelated   — 无关，各自独立存储

依赖：
  - config.py: build_Ollama_qwenLLM(), CONFLICT_PROMPT, EMBEDDING_MODEL, MEMORY_DUPLICATE_THRESHOLD
  - EMBEDDING_MODEL 需支持 embed_documents([text, ...]) 方法（LangChain embedding 接口）
"""

import sys
from pathlib import Path

# 确保能导入项目根目录的 config 模块
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from typing import Literal
import numpy as np
from pydantic import BaseModel
from langchain.messages import SystemMessage, HumanMessage
from metro_agent.config import (
    build_Chat_DeepseekLLM,
    CONFLICT_PROMPT,
    EMBEDDING_MODEL,
    MEMORY_DUPLICATE_THRESHOLD,
)


# ============================================================
# 冲突检测结果模型
# LLM 通过 function_calling 输出符合此 schema 的结构化 JSON
# ============================================================
class ConflictDecision(BaseModel):
    """记忆冲突检测的判定结果"""

    # ---- 两个记忆之间的关系类型 ----
    relation: Literal[
        "duplicate",   # 含义基本相同 → 去重合并，保留一条
        "complement",  # 内容不同但可同时成立 → 都保留
        "replace",     # 用户明确表示改变/取消/替换 → 用新的覆盖旧的
        "conflict",    # 两者矛盾且用户未指定保留哪个 → 需人工裁决
        "unrelated",   # 无直接关系 → 各自独立存储
    ]

    # ---- 是否存在冲突需要上层关注 ----
    has_conflict: bool

    # ---- 是否需要向用户发起确认询问 ----
    # 当 has_conflict=True 且无法自动裁决时置为 True
    requires_confirmation: bool

    # ---- 判定理由 ----
    # LLM 给出，便于日志追踪和人工审核
    reason: str


# ============================================================
# 构建 LLM 冲突检测器
# 强制输出 ConflictDecision 结构
# ============================================================
def build_conflict_detector():
    return build_Chat_DeepseekLLM().with_structured_output(
        ConflictDecision,
        method="json_mode",
    )

# 嵌入模型引用（从 config 注入，需支持 LangChain 接口：embed_documents）
emb_model = EMBEDDING_MODEL


# ============================================================
# 向量相似度计算（第一级：快速初筛）
# ============================================================
def calculate_similarity(existing: str, candidate: str) -> float:
    """
    计算已有记忆与候选记忆之间的余弦相似度。

    流程：
      1. 用 embedding 模型将两条文本批量向量化
      2. 计算余弦相似度（内积 / 模长乘积）
      3. 分母为 0 时返回 0.0（防御性处理）

    返回：0.0 ~ 1.0 之间的相似度分数
    """
    # 批量嵌入，一次调用得到两个向量
    vectors = np.asarray(
        emb_model.embed_documents([existing, candidate]),
        dtype=float,
    )
    existing_vector = vectors[0]
    candidate_vector = vectors[1]

    # 计算余弦相似度
    denominator = (
        np.linalg.norm(existing_vector)
        * np.linalg.norm(candidate_vector)
    )
    if denominator == 0:
        return 0.0
    return float(
        np.dot(existing_vector, candidate_vector) / denominator
    )


# ============================================================
# 冲突检测主函数（两级检测）
# ============================================================
def detect_conflict(
    user_text: str,   # 用户原始输入，用于辅助 LLM 理解用户意图
    existing: str,    # 已存储的旧记忆内容
    candidate: str,   # 待写入的新候选记忆内容
) -> ConflictDecision:
    """
    检测候选记忆与已有记忆之间的冲突关系（两级检测）。

    第一级 — 向量快速初筛：
      - 计算已有记忆和候选记忆的余弦相似度
      - 超过阈值 → 直接返回 duplicate，跳过 LLM 调用（省延迟、省成本）

    第二级 — LLM 细粒度判定：
      - 相似度不足阈值 → 调用 LLM 做语义级关系判断
      - LLM 返回 relation 后，额外修正 has_conflict 和 requires_confirmation：
        * replace / conflict → has_conflict=True, requires_confirmation=True
        * 其他类型           → 保持 LLM 原始输出

    返回：ConflictDecision，供上层 policy.evaluate_memory 使用
    """
    # ---- 第一级：向量相似度快速初筛 ----
    similarity = calculate_similarity(existing, candidate)
    if similarity >= MEMORY_DUPLICATE_THRESHOLD:
        # 高相似度直接判为重复，无需 LLM 参与
        return ConflictDecision(
            relation="duplicate",
            has_conflict=False,
            requires_confirmation=False,
            reason=f"语义相似度为 {similarity:.2f}",
        )
    detector = build_conflict_detector()
    # ---- 第二级：LLM 细粒度语义判定 ----
    decision = detector.invoke([
        SystemMessage(content=CONFLICT_PROMPT),
        HumanMessage(content=f"""
        用户原话：{user_text}
        已有记忆：{existing}
        候选记忆：{candidate}
        语义相似度:{similarity:.2f}"""),
    ])

    # 根据 relation 修正 has_conflict 和 requires_confirmation
    # replace 和 conflict 类型必须将这两个字段置为 True（即使 LLM 漏判）
    has_conflict = decision.relation in {"replace", "conflict"}
    return decision.model_copy(
        update={
            "has_conflict": has_conflict,
            "requires_confirmation": has_conflict,  # 有冲突就需要确认
        }
    )
