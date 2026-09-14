"""
记忆服务层（Memory Service）
===========================
记忆系统的编排层，串联策展 → 策略 → 冲突检测 → 存储的完整流水线。

处理流程（每轮对话执行一次 process_turn）：
  1. 规则修改检测     → 如检测到规则修改意图，立即返回（不走记忆流程）
  2. 策展提取         → Curator 从对话中提取 MemoryCandidate 列表
  3. 初评（无 relation）→ Policy 做关键词级快速分流（ignore / ask_confirmation）
  4. 向量检索         → 对初评通过者，搜索同类别的已有活跃记忆
  5. 冲突检测         → 将候选与已有记忆比对，获取 relation
  6. 复评（带 relation）→ Policy 结合冲突检测结果做最终裁决
  7. 执行             → auto_save 写入 ChromaDB / ask_confirmation 加入待办 / ignore 记录原因

依赖：
  - curator:      从对话提取候选记忆
  - conflict_detector: 判断新旧记忆的关系
  - policy:       策略分流裁决
  - chroma_store: 向量存储与检索
  - models:       数据模型与工厂函数
"""
import logging


from pydantic import BaseModel, Field
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from metro_agent.config import MEMORY_SEARCH_LIMIT
from metro_agent.memory.long_term.chroma_store import build_memory_store
from metro_agent.memory.long_term.conflict_detector import detect_conflict
from metro_agent.memory.long_term.curator import build_memory_curator
from metro_agent.memory.long_term.memory_intent import is_memory_query
from metro_agent.memory.long_term.models import (
    MemoryCandidate,
    MemoryRecord,
    create_memory_record,
)
from metro_agent.memory.long_term.policy import (
    evaluate_memory,
    is_rule_change_request,
)

logger = logging.getLogger(__name__)




class PendingMemory(BaseModel):
    """等待用户确认的待处理记忆"""

    candidate: MemoryCandidate
    existing_memory_id: str | None = None
    existing_content: str | None = None
    relation: str | None = None
    reason: str





class MemoryTurnResult(BaseModel):
    """单轮对话的记忆处理结果汇总"""

    saved: list[MemoryRecord] = Field(default_factory=list)
    pending: list[PendingMemory] = Field(default_factory=list)
    ignored: list[str] = Field(default_factory=list)
    rule_change_requested: bool = False





class MemoryService:
    """
    记忆系统编排器，管理完整的记忆处理流水线。

    内部持有：
      - curator: 记忆策展器（提取候选项）
      - store:   Chroma 向量存储（写入 & 检索）

    使用方式：
      service = MemoryService()
      result = service.process_turn(
          user_id="user-001",
          user_text="我喜欢简短回答",
          assistant_text="好的，我会注意。",
      )
    """

    def __init__(self):

        self.curator = build_memory_curator()
        self.store = build_memory_store()
    
    def process_turn(
        self,
        *,
        user_id: str,
        user_text: str,
        assistant_text: str,
    ) -> MemoryTurnResult:
        """
        处理一轮对话的记忆流水线。

        完整流程见模块 docstring。返回 MemoryTurnResult 汇总所有处理结果。
        """
        result = MemoryTurnResult()
        if is_memory_query(user_text):
            result.ignored.append("记忆查询不写入长期记忆")
            return result


        if is_rule_change_request(user_text):
            result.rule_change_requested = True
            return result


        candidates = self.curator.extract(
            user_text=user_text,
            assistant_text=assistant_text,
        )


        for candidate in candidates:
            source_evidence = user_text.strip()
            if not source_evidence:
                result.ignored.append("用户原话为空")
                continue
            candidate = candidate.model_copy(
                update={
                    "evidence": source_evidence,
                }
            )
            if not candidate.content.strip():
                result.ignored.append("候选记忆内容为空")
                continue


            preliminary = evaluate_memory(
                user_text=user_text,
                candidate=candidate,

            )


            if preliminary.action == "ignore":
                result.ignored.append(preliminary.reason)
                continue


            if preliminary.action == "ask_confirmation":
                result.pending.append(PendingMemory(
                    candidate=candidate,
                    reason=preliminary.reason,
                ))
                continue





            memories = self.store.search(
                user_id=user_id,
                query=candidate.content,
                limit=MEMORY_SEARCH_LIMIT,
            )


            same_category = [
                memory for memory in memories
                if memory.metadata.get("category") == candidate.category
            ]

            existing = same_category[0] if same_category else None
            relation = None



            if existing:
                try:
                    conflict = detect_conflict(
                        user_text=user_text,
                        existing=existing.content,
                        candidate=candidate.content,
                    )
                    relation = conflict.relation
                except Exception:
                    logger.exception("长期记忆冲突检测失败")

                    result.pending.append(PendingMemory(
                        candidate=candidate,
                        existing_memory_id=existing.memory_id,
                        existing_content=existing.content,
                        relation="conflict",
                        reason="冲突检测暂时不可用，未自动保存",
                    ))
                    continue

            decision = evaluate_memory(
                user_text=user_text,
                candidate=candidate,
                relation=relation,
            )



            if decision.action == "auto_save":

                source = (
                    "explicit"
                    if candidate.category == "explicit"
                    else "automatic"
                )
                record = create_memory_record(
                    user_id=user_id,
                    category=candidate.category,
                    content=candidate.content,
                    source=source,
                    sensitivity=candidate.sensitivity,
                    evidence=candidate.evidence,
                    target_date=candidate.target_date,
                    valid_until=candidate.valid_until,
                )
                self.store.upsert(record)
                result.saved.append(record)

            elif decision.action == "ask_confirmation":

                result.pending.append(PendingMemory(
                    candidate=candidate,
                    existing_memory_id=(
                        existing.memory_id if existing else None
                    ),
                    existing_content=(
                        existing.content if existing else None
                    ),
                    relation=relation,
                    reason=decision.reason,
                ))

            else:

                result.ignored.append(decision.reason)

        return result
