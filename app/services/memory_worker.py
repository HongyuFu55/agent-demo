"""独立生产级长期记忆 Worker 进程 (Memory Worker).

基于 Redis Stream 消费者组 (Consumer Group + XACK)，具备消息不丢失 (PEL 自动恢复)、
3 次指数避退重试、死信队列 (DLQ)、并发信号量限流与 SIGTERM 优雅停机全套工业级最佳实践。
"""

import asyncio
import json
import os
import signal
import sys
import time
from typing import Any
import structlog

from app.core.cache import cache_service
from app.core.config import settings
from app.services.memory import memory_service

logger = structlog.get_logger(__name__)


class ProductionMemoryWorker:
    """生产级长期记忆 Stream Worker 消费者."""

    def __init__(
        self,
        concurrent_limit: int = 5,
        max_retries: int = 3,
    ) -> None:
        """初始化 Worker 运行参数与信号控制."""
        self.stream_name = settings.VALKEY_MEMORY_STREAM_NAME
        self.dlq_name = settings.VALKEY_MEMORY_DLQ_NAME
        self.group_name = settings.VALKEY_MEMORY_CONSUMER_GROUP
        self.consumer_name = f"worker-{os.getpid()}"
        self.semaphore = asyncio.Semaphore(concurrent_limit)
        self.max_retries = max_retries
        self.is_running = True

    async def init_stream_group(self) -> None:
        """初始化 Redis Stream 消费者组，若不存在则创建."""
        r = getattr(cache_service, "redis", None)
        if r is None:
            raise RuntimeError("Valkey/Redis 未连接，Worker 无法启动")

        try:
            # 创建消费者组 (MKSTREAM 标志自动创建 Stream)
            await r.xgroup_create(self.stream_name, self.group_name, id="0", mkstream=True)
            logger.info("memory_worker_stream_group_created", stream=self.stream_name, group=self.group_name)
        except Exception as e:
            if "BUSYGROUP" in str(e):
                logger.debug("memory_worker_group_already_exists", group=self.group_name)
            else:
                logger.warning("init_stream_group_failed", error=str(e))

    def setup_signal_handlers(self) -> None:
        """注册 SIGTERM / SIGINT 信号钩子实现优雅停机."""
        loop = asyncio.get_running_loop()

        def _signal_handler(sig_name: str) -> None:
            logger.info("memory_worker_graceful_shutdown_initiated", signal=sig_name)
            self.is_running = False

        for sig in (signal.SIGTERM, signal.SIGINT):
            try:
                loop.add_signal_handler(sig, lambda s=sig.name: _signal_handler(s))
            except NotImplementedError:
                # Windows 系统不支持 add_signal_handler 兼容性降级
                pass

    async def recover_pending_messages(self) -> None:
        """进程启动时主动恢复未 ACK 的 Pending 消息 (PEL 绝不丢数据)."""
        r = getattr(cache_service, "redis", None)
        if r is None:
            return

        logger.info("memory_worker_recovering_pending_messages_start")
        try:
            # ID '0' 专门读取当前 Consumer 的 Pending 历史消息
            pending_streams = await r.xreadgroup(
                self.group_name,
                self.consumer_name,
                {self.stream_name: "0"},
                count=10,
            )
            if pending_streams:
                for stream_name, message_list in pending_streams:
                    for msg_id, fields in message_list:
                        await self._process_single_message(msg_id, fields, is_pending=True)
        except Exception as e:
            logger.warning("recover_pending_messages_failed", error=str(e))

    async def _process_single_message(
        self,
        msg_id: bytes | str,
        fields: dict[Any, Any],
        is_pending: bool = False,
    ) -> None:
        """处理单条 Redis Stream 消息 (带 Semaphore 限流、重试与 ACK/DLQ)."""
        str_msg_id = msg_id.decode() if isinstance(msg_id, bytes) else str(msg_id)
        r = getattr(cache_service, "redis", None)
        if r is None:
            return

        async with self.semaphore:
            raw_payload = fields.get(b"payload") or fields.get("payload")
            if not raw_payload:
                # 破损空消息直接 ACK 移除
                await r.xack(self.stream_name, self.group_name, str_msg_id)
                return

            try:
                data = json.loads(raw_payload)
                user_id = data.get("user_id")
                messages = data.get("messages", [])
                metadata = data.get("metadata", {})
            except Exception:
                # 解析失败不可逆错误，投递死信队列并 ACK
                await r.xadd(self.dlq_name, {"poison_pill": str(raw_payload), "reason": "invalid_json"})
                await r.xack(self.stream_name, self.group_name, str_msg_id)
                return

            # 执行 3 次指数退避重试
            success = False
            for attempt in range(1, self.max_retries + 1):
                try:
                    logger.info("memory_worker_processing_task", msg_id=str_msg_id, user_id=user_id, attempt=attempt)
                    await memory_service.add(user_id=user_id, messages=messages, metadata=metadata)
                    success = True
                    break
                except Exception as e:
                    logger.warning("memory_worker_task_attempt_failed", msg_id=str_msg_id, attempt=attempt, error=str(e))
                    if attempt < self.max_retries:
                        await asyncio.sleep(2**attempt)

            if success:
                # 处理成功发送 XACK
                await r.xack(self.stream_name, self.group_name, str_msg_id)
                logger.info("memory_worker_task_ack_success", msg_id=str_msg_id, user_id=user_id)
            else:
                # 连续 3 次失败投递死信队列 DLQ 并 ACK 移除
                logger.error("memory_worker_task_max_retries_exceeded_moving_to_dlq", msg_id=str_msg_id, user_id=user_id)
                await r.xadd(self.dlq_name, {"payload": str(raw_payload), "failed_at": time.time()})
                await r.xack(self.stream_name, self.group_name, str_msg_id)

    async def run(self) -> None:
        """Worker 独立主循环."""
        logger.info("memory_worker_starting", consumer=self.consumer_name)
        await cache_service.initialize()
        await memory_service.initialize()
        await self.init_stream_group()
        self.setup_signal_handlers()

        # 启动时恢复未 ACK 历史消息
        await self.recover_pending_messages()

        logger.info("memory_worker_listening_stream", stream=self.stream_name, group=self.group_name)

        r = getattr(cache_service, "redis", None)
        if r is None:
            raise RuntimeError("Valkey/Redis 实例不存在，无法轮询消费")

        while self.is_running:
            try:
                # '>' 代表从消费者组拉取最新未分配消息 (阻塞 2 秒)
                streams = await r.xreadgroup(
                    self.group_name,
                    self.consumer_name,
                    {self.stream_name: ">"},
                    count=5,
                    block=2000,
                )

                if streams:
                    for _, message_list in streams:
                        for msg_id, fields in message_list:
                            if not self.is_running:
                                break
                            asyncio.create_task(self._process_single_message(msg_id, fields))
            except Exception as e:
                logger.exception("memory_worker_loop_error", error=str(e))
                await asyncio.sleep(2.0)

        logger.info("memory_worker_graceful_shutdown_complete")


async def main() -> None:
    worker = ProductionMemoryWorker()
    await worker.run()


if __name__ == "__main__":
    asyncio.run(main())
