"""
RedisCompressionQueue —— 基于 Redis 的可靠压缩任务队列
====================================================
实现"可靠队列"（Reliable Queue）模式，将压缩任务异步化，确保
任务不会因 Worker 崩溃而丢失。

设计动机：
  对话压缩（摘要生成）需要调 LLM，耗时较长，不应阻塞用户回复。
  此外，如果压缩 Worker 在处理中途崩溃，任务不能丢失。
  Redis 的 BRPOPLPUSH 原子操作天然支持这种场景。

可靠队列模式（BRPOPLPUSH 模式）：
  1. enqueue:   LPUSH 将任务推入 pending 队列
  2. reserve:   BRPOPLPUSH 原子地将任务从 pending 弹出并推入 processing 备份队列
  3. process:   Worker 执行压缩
  4. ack:       成功 → LREM 从 processing 中删除
  5. requeue:   失败 → LREM + LPUSH 将任务从 processing 移回 pending

  这样即使 Worker 崩溃，processing 队列中残留的任务可被
  定期扫描并重新入队，不会丢失。

存储结构（Redis key 命名规范）：
  metro:compression:pending     → List  [pending 任务队列]
  metro:compression:processing  → List  [正在处理的任务备份]
  metro:compression:source:{thread_id}:{round_id} → String [原始消息 JSON]
  metro:compression:result:{job_id}               → String [压缩结果 JSON]
"""

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from langchain_core.messages import (
    BaseMessage,
    messages_from_dict,
    messages_to_dict,
)
from redis import Redis

from metro_agent.config import (
    SHORT_TERM_MEMORY_REDIS_URL,
    SHORT_TERM_MEMORY_TTL_MINUTES,
)






@dataclass
class ReservedCompressionJob:
    """已被当前 Worker 预占的压缩任务。

    由 reserve() 返回，包含原始 JSON 和已解析的 dict，
    同时保留两者是为了：
      - raw_payload: 用于 ack/requeue 时的精确匹配（LREM 需要原始值）
      - payload:     用于读取任务元数据（job_id / round_id / source_ref 等）
    """
    raw_payload: str
    payload: dict[str, Any]






class RedisCompressionQueue:
    """基于 Redis 的可靠压缩任务队列。

    使用两个 Redis List 实现任务可靠性：
      - PENDING_KEY:    待处理任务队列（FIFO）
      - PROCESSING_KEY: 处理中任务备份列表（用于崩溃恢复）

    BRPOPLPUSH 保证 reserve 操作的原子性：弹出 + 备份在一条 Redis 命令中完成，
    不会出现"弹出成功但备份失败"的中间状态。
    """


    PENDING_KEY = "metro:compression:pending"
    PROCESSING_KEY = "metro:compression:processing"
    SOURCE_PREFIX = "metro:compression:source"
    RESULT_PREFIX = "metro:compression:result"

    def __init__(self):
        """初始化 Redis 连接。

        decode_responses=True：从 Redis 读取的数据自动解码为 str，
        省去手动 .decode('utf-8') 的步骤。
        """
        self.client = Redis.from_url(
            SHORT_TERM_MEMORY_REDIS_URL,
            decode_responses=True,
        )

        self.ttl_seconds = (
            SHORT_TERM_MEMORY_TTL_MINUTES * 60
        )





    def enqueue(
        self,
        thread_id: str,
        job: dict,
        messages: list[BaseMessage],
    ) -> str:
        """将压缩任务和原始消息写入 Redis 队列。

        原子操作（pipeline）：
          1. 将原始消息序列化后存入 SOURCE_PREFIX key（带 TTL）
          2. 将任务载荷 LPUSH 到 pending 队列

        分离存储的考虑：
          - 队列载荷保持精简（只含元数据 + source_ref），
            方便 reserve 时快速读取
          - 原始消息体积可能很大（几十 KB），单独存储，
            Worker 在真正处理时才通过 load_source 按需加载

        Args:
            thread_id: 对话线程 ID（用于隔离不同用户的队列）。
            job: CompressionJob 字典（来自 compression_detection_node）。
            messages: 该轮次的原始消息列表。

        Returns:
            source_ref: 原始消息的 Redis key，Worker 通过它加载消息。
        """
        job_id = job["job_id"]
        round_id = job["round_id"]


        source_ref = (
            f"{self.SOURCE_PREFIX}:"
            f"{thread_id}:{round_id}"
        )


        source_payload = {
            "thread_id": thread_id,
            "round_id": round_id,
            "round_number": job["round_number"],
            "messages": messages_to_dict(messages),
            "created_at": datetime.now(
                timezone.utc
            ).isoformat(),
        }


        queue_payload = {
            "job_id": job_id,
            "thread_id": thread_id,
            "round_id": round_id,
            "round_number": job["round_number"],
            "base_summary_version": job.get(
                "base_summary_version",
                0,
            ),
            "source_ref": source_ref,
        }

        raw_queue_payload = json.dumps(
            queue_payload,
            ensure_ascii=False,
        )


        pipeline = self.client.pipeline()


        pipeline.set(
            source_ref,
            json.dumps(
                source_payload,
                ensure_ascii=False,
            ),
            ex=self.ttl_seconds,
        )



        pipeline.lpush(
            self.PENDING_KEY,
            raw_queue_payload,
        )

        pipeline.execute()

        return source_ref





    def reserve(
        self,
        timeout: int = 5,
    ) -> ReservedCompressionJob | None:
        """原子地预占一个待处理任务。

        使用 BRPOPLPUSH（阻塞式右弹出 + 左推入）：
          - 从 PENDING_KEY 尾部弹出一个任务（FIFO 出队）
          - 同时将其推入 PROCESSING_KEY 头部（备份）

        这两个操作在 Redis 服务端原子执行，保证：
          - 任务不会被多个 Worker 同时取走
          - 即使 Worker 取走后崩溃，任务仍在 PROCESSING_KEY 中可恢复

        Args:
            timeout: 阻塞等待超时（秒）。0 表示永久阻塞。
                    设为 5 秒让 Worker 定期检查是否需要退出。

        Returns:
            ReservedCompressionJob（含 raw_payload 和解析后的 payload），
            如果超时无任务则返回 None。
        """
        raw_payload = self.client.brpoplpush(
            self.PENDING_KEY,
            self.PROCESSING_KEY,
            timeout=timeout,
        )

        if raw_payload is None:
            return None

        return ReservedCompressionJob(
            raw_payload=raw_payload,
            payload=json.loads(raw_payload),
        )





    def load_source(
        self,
        source_ref: str,
    ) -> list[BaseMessage]:
        """根据 source_ref 加载原始消息。

        读取时自动刷新 TTL（读即续期），确保正在处理中的消息
        不会在处理过程中过期。

        Args:
            source_ref: enqueue 返回的原始消息 Redis key。

        Returns:
            反序列化后的 BaseMessage 列表。

        Raises:
            KeyError: 当原始消息不存在或已过期时抛出。
        """
        raw_source = self.client.get(source_ref)

        if raw_source is None:
            raise KeyError(
                f"压缩原文不存在或已过期：{source_ref}"
            )


        self.client.expire(
            source_ref,
            self.ttl_seconds,
        )

        source_payload = json.loads(raw_source)

        return messages_from_dict(
            source_payload["messages"]
        )





    def save_result(
        self,
        job_id: str,
        result: dict,
    ) -> str:
        """保存压缩结果到 Redis。

        结果与队列分离存储，通过 job_id 关联。

        Args:
            job_id: 压缩任务 ID。
            result: 压缩结果字典（如 {"summary": "...", "token_count": 120}）。

        Returns:
            result_ref: 结果存储的 Redis key，可供 get_result 读取。
        """
        result_ref = (
            f"{self.RESULT_PREFIX}:{job_id}"
        )

        self.client.set(
            result_ref,
            json.dumps(result, ensure_ascii=False),
            ex=self.ttl_seconds,
        )

        return result_ref

    def get_result(
        self,
        job_id: str,
    ) -> dict | None:
        """读取压缩结果。

        Args:
            job_id: 压缩任务 ID。

        Returns:
            压缩结果字典，如果不存在或已过期则返回 None。
        """
        result_ref = (
            f"{self.RESULT_PREFIX}:{job_id}"
        )
        raw_result = self.client.get(result_ref)

        if raw_result is None:
            return None

        return json.loads(raw_result)





    def acknowledge(
        self,
        reserved_job: ReservedCompressionJob,
    ) -> None:
        """确认任务完成，从 processing 备份队列中移除。

        调用时机：Worker 成功生成摘要并保存结果后。
        必须传入 reserve 时返回的原始 raw_payload，
        LREM 通过精确匹配字符串来删除。

        Args:
            reserved_job: reserve() 返回的预占任务。
        """
        self.client.lrem(
            self.PROCESSING_KEY,
            1,
            reserved_job.raw_payload,
        )

    def requeue(
        self,
        reserved_job: ReservedCompressionJob,
    ) -> None:
        """任务处理失败，将其从 processing 移回 pending。

        使用 pipeline 保证原子性：
          1. LREM 从 processing 中删除
          2. LPUSH 推回 pending 队列头部（优先重试）

        调用时机：Worker 处理失败（如 LLM 调用超时），
        希望稍后重试时调用。

        Args:
            reserved_job: reserve() 返回的预占任务。
        """
        pipeline = self.client.pipeline()


        pipeline.lrem(
            self.PROCESSING_KEY,
            1,
            reserved_job.raw_payload,
        )

        pipeline.lpush(
            self.PENDING_KEY,
            reserved_job.raw_payload,
        )

        pipeline.execute()