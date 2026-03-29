"""EventBus: コンポーネント間の非同期イベント配信"""
from __future__ import annotations

import asyncio
import fnmatch
import logging
from collections import defaultdict
from typing import Any, Callable, Coroutine

logger = logging.getLogger(__name__)

Handler = Callable[[str, Any], Coroutine[Any, Any, None]]


class EventBus:
    def __init__(self) -> None:
        # event_type -> list of handlers
        self._handlers: dict[str, list[Handler]] = defaultdict(list)

    def subscribe(self, event_type: str, handler: Handler) -> None:
        """イベントを購読する。ワイルドカード可（例: "audio.*"）"""
        self._handlers[event_type].append(handler)

    def unsubscribe(self, event_type: str, handler: Handler) -> None:
        handlers = self._handlers.get(event_type, [])
        if handler in handlers:
            handlers.remove(handler)

    async def publish(self, event_type: str, data: Any = None) -> None:
        """イベントを発行し、一致する全ハンドラを並列実行する"""
        tasks: list[asyncio.Task] = []
        for pattern, handlers in self._handlers.items():
            if fnmatch.fnmatch(event_type, pattern):
                for handler in handlers:
                    task = asyncio.create_task(
                        self._safe_call(handler, event_type, data),
                        name=f"event:{event_type}",
                    )
                    tasks.append(task)
        if tasks:
            await asyncio.gather(*tasks)

    async def _safe_call(self, handler: Handler, event_type: str, data: Any) -> None:
        try:
            await handler(event_type, data)
        except Exception:
            logger.exception("EventBus handler error [%s] handler=%s", event_type, handler)
