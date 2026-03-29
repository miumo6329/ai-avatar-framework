"""STTWorker: 音声認識ワーカー（Whisper一括変換型）"""
from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from typing import Any, Callable, Coroutine

from ai_avatar.event_bus import EventBus
from ai_avatar.workers.base import BaseWorker

logger = logging.getLogger(__name__)

OnFinal = Callable[[str], Coroutine[Any, Any, None]]
OnPartial = Callable[[str], Coroutine[Any, Any, None]]
OnClause = Callable[[str], Coroutine[Any, Any, None]]


class STTAdapter(ABC):
    """STTエンジンの共通インターフェース"""

    on_partial: OnPartial | None = None
    on_clause: OnClause | None = None
    on_final: OnFinal | None = None

    @abstractmethod
    async def setup(self) -> None:
        """モデルロード等の初期化"""

    @abstractmethod
    async def on_audio_chunk(
        self,
        chunk: bytes,
        *,
        is_speech_start: bool = False,
        is_speech_end: bool = False,
        sample_rate: int = 16000,
    ) -> None:
        """音声チャンクを受け取る（VAD済み）"""

    @abstractmethod
    async def teardown(self) -> None:
        """リソース解放"""


class STTWorker(BaseWorker):
    """STTWorker。

    audio.input を購読し、STTAdapterに渡す。
    Adapterのコールバック経由で stt.partial / stt.clause / stt.final を発行する。
    """

    def __init__(self, event_bus: EventBus, config: dict[str, Any], adapter: STTAdapter) -> None:
        super().__init__(event_bus, config)
        self._adapter = adapter
        self._adapter.on_partial = self._on_partial
        self._adapter.on_clause = self._on_clause
        self._adapter.on_final = self._on_final

    async def setup(self) -> None:
        await self._adapter.setup()
        self.subscribe("audio.input")
        self.subscribe("turn.interrupt")

    async def teardown(self) -> None:
        await self._adapter.teardown()

    async def _handle(self, event_type: str, data: Any) -> None:
        if event_type == "audio.input":
            await self._adapter.on_audio_chunk(
                data["data"],
                is_speech_start=data.get("is_speech_start", False),
                is_speech_end=data.get("is_speech_end", False),
                sample_rate=data.get("sample_rate", 16000),
            )
        elif event_type == "turn.interrupt":
            await self._adapter.teardown()
            await self._adapter.setup()

    async def _on_partial(self, text: str) -> None:
        await self._bus.publish("stt.partial", {"text": text})

    async def _on_clause(self, text: str) -> None:
        await self._bus.publish("stt.clause", {"text": text})

    async def _on_final(self, text: str) -> None:
        logger.info("[STTWorker] final: %r", text)
        await self._bus.publish("stt.final", {"text": text})


# ── WhisperAdapter ─────────────────────────────────────────────────────────────

class WhisperAdapter(STTAdapter):
    """faster-whisperを使った一括変換型STTAdapter。

    is_speech_end=True 受信時にバッファ全体をWhisperに送り、stt.finalのみ発行する。
    stt.partial / stt.clause は発行しない。
    """

    def __init__(self, config: dict[str, Any]) -> None:
        self._config = config
        self._model: Any = None
        self._buffer: bytearray = bytearray()
        self._sample_rate: int = 16000

    async def setup(self) -> None:
        from faster_whisper import WhisperModel  # type: ignore[import-untyped]

        model_size = self._config.get("model", "large-v3")
        device = self._config.get("device", "auto")
        compute_type = self._config.get("compute_type", "auto")
        logger.info("[WhisperAdapter] loading model=%s device=%s", model_size, device)
        self._model = WhisperModel(model_size, device=device, compute_type=compute_type)
        self._buffer = bytearray()
        logger.info("[WhisperAdapter] model loaded")

    async def on_audio_chunk(
        self,
        chunk: bytes,
        *,
        is_speech_start: bool = False,
        is_speech_end: bool = False,
        sample_rate: int = 16000,
    ) -> None:
        if is_speech_start:
            self._sample_rate = sample_rate
        self._buffer.extend(chunk)

        if is_speech_end and self._buffer:
            await self._transcribe()
            self._buffer = bytearray()

    async def _transcribe(self) -> None:
        import asyncio

        import numpy as np

        # PCM16 → numpy float32
        pcm = bytes(self._buffer)
        audio = np.frombuffer(pcm, dtype=np.int16).astype(np.float32) / 32768.0

        # Whisperは16kHz前提。必要ならリサンプリング
        if self._sample_rate != 16000:
            logger.info("[WhisperAdapter] resampling %dHz → 16000Hz", self._sample_rate)
            original_len = len(audio)
            target_len = int(original_len * 16000 / self._sample_rate)
            audio = np.interp(
                np.linspace(0, original_len, target_len),
                np.arange(original_len),
                audio,
            )

        language = self._config.get("language", "ja")

        loop = asyncio.get_running_loop()
        segments, _ = await loop.run_in_executor(
            None,
            lambda: self._model.transcribe(audio, language=language),
        )
        text = "".join(seg.text for seg in segments).strip()
        if text and self.on_final:
            await self.on_final(text)

    async def teardown(self) -> None:
        self._buffer = bytearray()
