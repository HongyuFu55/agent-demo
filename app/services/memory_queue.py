"""基于 Redis Stream 的生产级长期记忆解耦队列与防抖服务."""

import asyncio
import json
import time
from typing import Any
import structlog

from app.core.cache import cache_service
from app.core.config import settings
from app.services.memory import memory_service

logger = structlog.get_logger(__name__)


class MemoryQueueService:
    """生产级长期记忆任务解耦队列服务 (支持 15 秒滑动窗口防抖与 Redis Stream XADD 投递)."""

    def __init__(self) -> None:
        """初始化队列与防抖配置."""
        self.stream_name = settings.VALKEY_MEMORY_STREAM_NAME
        self.dlq_name = settings.VALKEY_MEMORY_DLQ_NAME
        self.debounce_seconds = 15  # 15 秒用户级滑动窗口防抖

    async def enqueue(
        self,
        user_id: str | None,
        messages: list[dict[str, Any]],
        metadata: dict[str, Any] | None = None,
    ) -> None:
        """将聊天对话投递到 Valkey/Redis Stream 队列，自带 15 秒滑动窗口防抖."""
        if not user_id or not messages:
            return

        str_user_id = str(user_id)
        payload = {
            "user_id": str_user_id,
            "messages": messages,
            "metadata": metadata or {},
            "timestamp": time.time(),
        }

        # 检查 Valkey/Redis 连通性；若 Redis 未配置，降级为本进程后台任务
        r = getattr(cache_service, "redis", None)
        if r is None:
            logger.debug("valkey_not_available_fallback_in_process", user_id=str_user_id)
            asyncio.create_task(memory_service.add(user_id, messages, metadata))
            return

        try:
            # 1. 用户级 15 秒滑动窗口防抖锁
            debounce_key = f"memory_debounce:{str_user_id}"
            buffer_key = f"memory_buffer:{str_user_id}"

            # 追加当前发言到缓冲区
            await r.rpush(buffer_key, json.dumps(payload, ensure_ascii=False))
            await r.expire(buffer_key, 60)

            # 设置防抖锁
            is_new_window = await r.set(debounce_key, "1", ex=self.debounce_seconds, nx=True)

            if is_new_window:
                # 开启 15 秒延时，延时结束后打包投递 Stream 队列
                asyncio.create_task(self._delay_and_push_stream(str_user_id, buffer_key))
                logger.info("memory_debouncing_window_started", user_id=str_user_id, seconds=self.debounce_seconds)
            else:
                logger.info("memory_debouncing_message_buffered", user_id=str_user_id)

        except Exception as e:
            logger.warning("enqueue_memory_task_failed_fallback_direct", user_id=str_user_id, error=str(e))
            asyncio.create_task(memory_service.add(user_id, messages, metadata))

    async def _delay_and_push_stream(self, user_id: str, buffer_key: str) -> None:
        """延时 15 秒后，将防抖缓冲区中的全部对话聚合打包，投递至 Redis Stream."""
        await asyncio.sleep(self.debounce_seconds)
        try:
            r = getattr(cache_service, "redis", None)
            if r is None:
                return

            raw_items = await r.lrange(buffer_key, 0, -1)
            await r.delete(buffer_key)

            if not raw_items:
                return

            # 合并滑动窗口内的对话
            aggregated_messages: list[dict[str, Any]] = []
            latest_metadata: dict[str, Any] = {}

            for item_str in raw_items:
                try:
                    data = json.loads(item_str)
                    msgs = data.get("messages", [])
                    aggregated_messages.extend(msgs)
                    if data.get("metadata"):
                        latest_metadata.update(data["metadata"])
                except Exception:
                    pass

            if not aggregated_messages:
                return

            # 投递任务至 Redis Stream (XADD)
            task_payload = {
                "payload": json.dumps(
                    {
                        "user_id": user_id,
                        "messages": aggregated_messages,
                        "metadata": latest_metadata,
                        "created_at": time.time(),
                    },
                    ensure_ascii=False,
                )
            }

            msg_id = await r.xadd(self.stream_name, task_payload)
            logger.info(
                "memory_task_stream_pushed",
                user_id=user_id,
                stream=self.stream_name,
                msg_id=msg_id.decode() if isinstance(msg_id, bytes) else str(msg_id),
                message_count=len(aggregated_messages),
            )

        except Exception as e:
            logger.exception("delay_push_memory_stream_failed", user_id=user_id, error=str(e))


# 全局单例
memory_queue_service = MemoryQueueService()
