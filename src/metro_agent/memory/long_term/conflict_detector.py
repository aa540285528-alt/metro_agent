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






class ConflictDecision(BaseModel):
    """记忆冲突检测的判定结果"""


    relation: Literal[
        "duplicate",
        "complement",
        "replace",
        "conflict",
        "unrelated",
    ]


    has_conflict: bool



    requires_confirmation: bool



    reason: str






def build_conflict_detector():
    return build_Chat_DeepseekLLM().with_structured_output(
        ConflictDecision,
        method="json_mode",
    )


emb_model = EMBEDDING_MODEL





def calculate_similarity(existing: str, candidate: str) -> float:
    """
    计算已有记忆与候选记忆之间的余弦相似度。

    流程：
      1. 用 embedding 模型将两条文本批量向量化
      2. 计算余弦相似度（内积 / 模长乘积）
      3. 分母为 0 时返回 0.0（防御性处理）

    返回：0.0 ~ 1.0 之间的相似度分数
    """

    vectors = np.asarray(
        emb_model.embed_documents([existing, candidate]),
        dtype=float,
    )
    existing_vector = vectors[0]
    candidate_vector = vectors[1]


    denominator = (
        np.linalg.norm(existing_vector)
        * np.linalg.norm(candidate_vector)
    )
    if denominator == 0:
        return 0.0
    return float(
        np.dot(existing_vector, candidate_vector) / denominator
    )





def detect_conflict(
    user_text: str,
    existing: str,
    candidate: str,
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

    similarity = calculate_similarity(existing, candidate)
    if similarity >= MEMORY_DUPLICATE_THRESHOLD:

        return ConflictDecision(
            relation="duplicate",
            has_conflict=False,
            requires_confirmation=False,
            reason=f"语义相似度为 {similarity:.2f}",
        )
    detector = build_conflict_detector()

    decision = detector.invoke([
        SystemMessage(content=CONFLICT_PROMPT),
        HumanMessage(content=f"""
        用户原话：{user_text}
        已有记忆：{existing}
        候选记忆：{candidate}
        语义相似度:{similarity:.2f}"""),
    ])



    has_conflict = decision.relation in {"replace", "conflict"}
    return decision.model_copy(
        update={
            "has_conflict": has_conflict,
            "requires_confirmation": has_conflict,
        }
    )
