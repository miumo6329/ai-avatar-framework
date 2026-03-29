"""BaseWorker: 全Workerの基底クラス"""
from __future__ import annotations

import asyncio
import logging
from enum import Enum
from typing import Any

from ai_avatar.event_bus import EventBus

logger = logging.getLogger(__name__)


class WorkerStatus(str, Enum):
    READY = "ready"
    DEGRADED = "degraded"
    DOWN = "down"


class BaseWorker:
    """Worker共通の基盤。

    サブクラスは以下を実装する:
    - setup(): 初期化処理（モデルロード等）
    - _handle(event_type, data): イベントハンドラ
    - teardown(): 終了処理
    """

    def __init__(self, event_bus: EventBus, config: dict[str, Any]) -> None:
        self._bus = event_bus
        self._config = config
        self._status = WorkerStatus.DOWN
        self._subscriptions: list[tuple[str, Any]] = []

    # ── ライフサイクル ────────────────────────────────────────────

    async def start(self) -> None:
        logger.info("[%s] starting", self.__class__.__name__)
        try:
            await self.setup()
            self._set_status(WorkerStatus.READY)
            logger.info("[%s] ready", self.__class__.__name__)
        except Exception:
            self._set_status(WorkerStatus.DOWN)
            logger.exception("[%s] failed to start", self.__class__.__name__)
            raise

    async def stop(self) -> None:
        logger.info("[%s] stopping", self.__class__.__name__)
        for event_type, handler in self._subscriptions:
            self._bus.unsubscribe(event_type, handler)
        self._subscriptions.clear()
        try:
            await self.teardown()
        except Exception:
            logger.exception("[%s] error during teardown", self.__class__.__name__)
        self._set_status(WorkerStatus.DOWN)

    async def reset(self) -> None:
        """内部状態をクリアしてREADYに復帰する。サブクラスでオーバーライド可。"""
        self._set_status(WorkerStatus.READY)

    # ── サブクラスが実装するフック ─────────────────────────────────

    async def setup(self) -> None:
        pass

    async def teardown(self) -> None:
        pass

    # ── イベント購読ヘルパー ──────────────────────────────────────

    def subscribe(self, event_type: str) -> None:
        """event_typeを購読し、_handleにルーティングする"""
        async def handler(et: str, data: Any) -> None:
            await self._handle(et, data)

        self._bus.subscribe(event_type, handler)
        self._subscriptions.append((event_type, handler))

    async def _handle(self, event_type: str, data: Any) -> None:
        """受信イベントの処理。サブクラスでオーバーライドする。"""

    # ── 稼働状態管理 ──────────────────────────────────────────────

    def _set_status(self, status: WorkerStatus) -> None:
        if self._status == status:
            return
        self._status = status
        asyncio.create_task(
            self._bus.publish("worker.status", {
                "worker": self.__class__.__name__,
                "status": status.value,
            }),
            name=f"worker.status:{self.__class__.__name__}",
        )

    @property
    def status(self) -> WorkerStatus:
        return self._status
