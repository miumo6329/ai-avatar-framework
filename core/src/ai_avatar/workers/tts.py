"""TTSWorker: テキスト→音声変換ワーカー（VOICEVOX）"""
from __future__ import annotations

import asyncio
import logging
import re
from abc import ABC, abstractmethod
from typing import Any

from ai_avatar.event_bus import EventBus
from ai_avatar.workers.base import BaseWorker

logger = logging.getLogger(__name__)

# 文の区切り文字（句読点）
_SENTENCE_END_PATTERN = re.compile(r"[。！？]")
# 強制分割の文字数上限
_MAX_CHUNK_CHARS = 40


class TTSAdapter(ABC):
    """TTSエンジンの共通インターフェース"""

    @abstractmethod
    async def setup(self) -> None:
        pass

    @abstractmethod
    async def synthesize(self, text: str) -> bytes:
        """テキストをPCM音声バイト列に変換する"""

    @abstractmethod
    def audio_format(self) -> dict[str, Any]:
        """{"format": ..., "sample_rate": ..., "channels": ...}"""

    @abstractmethod
    async def teardown(self) -> None:
        pass


class TTSWorker(BaseWorker):
    """TTSWorker。

    購読するイベント:
    - llm.response_chunk → テキストバッファに蓄積、文確定でTTS合成
    - llm.response_done  → バッファ残りを合成してフラッシュ
    - tts.stop / turn.interrupt → バッファクリア

    発行するイベント:
    - tts.audio_chunk: 合成済み音声チャンク
    """

    def __init__(self, event_bus: EventBus, config: dict[str, Any], adapter: TTSAdapter) -> None:
        super().__init__(event_bus, config)
        self._adapter = adapter
        self._text_buffer = ""
        self._synthesis_queue: asyncio.Queue[str | None] = asyncio.Queue()
        self._sender_task: asyncio.Task | None = None

    async def setup(self) -> None:
        await self._adapter.setup()
        self.subscribe("llm.response_chunk")
        self.subscribe("llm.response_done")
        self.subscribe("tts.stop")
        self.subscribe("turn.interrupt")
        self._sender_task = asyncio.create_task(self._sender_loop(), name="tts-sender")
        logger.info("[TTSWorker] ready")

    async def teardown(self) -> None:
        if self._sender_task:
            self._sender_task.cancel()
        await self._adapter.teardown()

    async def _handle(self, event_type: str, data: Any) -> None:
        if event_type == "llm.response_chunk":
            self._text_buffer += data.get("text", "")
            await self._flush_sentences(final=False)
        elif event_type == "llm.response_done":
            if data.get("action") == "respond":
                # 残りのバッファを送出
                await self._flush_sentences(final=True)
                await self._synthesis_queue.put(None)  # sentinel: 送信完了
        elif event_type in ("tts.stop", "turn.interrupt"):
            await self._clear()

    async def _flush_sentences(self, *, final: bool) -> None:
        """バッファから文を切り出してキューに投入する"""
        while True:
            m = _SENTENCE_END_PATTERN.search(self._text_buffer)
            if m:
                sentence = self._text_buffer[:m.end()]
                self._text_buffer = self._text_buffer[m.end():]
                await self._synthesis_queue.put(sentence)
            elif final and self._text_buffer.strip():
                await self._synthesis_queue.put(self._text_buffer.strip())
                self._text_buffer = ""
                break
            elif len(self._text_buffer) >= _MAX_CHUNK_CHARS:
                # 読点か空白で強制分割
                split_at = _MAX_CHUNK_CHARS
                for sep in ["、", " ", "　"]:
                    idx = self._text_buffer.rfind(sep, 0, _MAX_CHUNK_CHARS)
                    if idx != -1:
                        split_at = idx + len(sep)
                        break
                await self._synthesis_queue.put(self._text_buffer[:split_at])
                self._text_buffer = self._text_buffer[split_at:]
            else:
                break

    async def _clear(self) -> None:
        """バッファとキューをクリアする"""
        self._text_buffer = ""
        while not self._synthesis_queue.empty():
            try:
                self._synthesis_queue.get_nowait()
            except asyncio.QueueEmpty:
                break

    async def _sender_loop(self) -> None:
        """キューから文を取り出してTTS合成→送信するループ"""
        is_last = False
        while True:
            try:
                sentence = await self._synthesis_queue.get()
            except asyncio.CancelledError:
                return

            if sentence is None:
                # sentinelはis_finalフラグを立てた最後のチャンク送信を示す
                await self._bus.publish("tts.audio_chunk", {
                    "data": b"",
                    **self._adapter.audio_format(),
                    "is_final": True,
                })
                continue

            logger.debug("[TTSWorker] synthesizing: %r", sentence)
            try:
                audio = await self._adapter.synthesize(sentence)
            except asyncio.CancelledError:
                return
            except Exception:
                logger.exception("[TTSWorker] synthesis error")
                continue

            # 次のキューにsentinelがあるかを確認（is_final判定に使う）
            is_final = self._synthesis_queue.empty()
            await self._bus.publish("tts.audio_chunk", {
                "data": audio,
                **self._adapter.audio_format(),
                "is_final": is_final,
            })


# ── VoicevoxAdapter ────────────────────────────────────────────────────────────

class VoicevoxAdapter(TTSAdapter):
    """VOICEVOX互換APIを使ったTTSAdapter。

    VOICEVOX Engine（localhost:50021）にHTTPリクエストして音声を合成する。
    返却される音声はWAV形式。WebSocket送信前にヘッダを除去してPCMに変換する。
    """

    def __init__(self, config: dict[str, Any]) -> None:
        self._config = config
        self._client: Any = None
        self._base_url: str = ""
        self._speaker_id: int = 1

    async def setup(self) -> None:
        import httpx
        host = self._config.get("host", "localhost")
        port = self._config.get("port", 50021)
        self._base_url = f"http://{host}:{port}"
        self._speaker_id = self._config.get("speaker_id", 1)
        self._speed_scale = self._config.get("speed_scale", 1.0)
        self._pitch_scale = self._config.get("pitch_scale", 0.0)
        self._client = httpx.AsyncClient(base_url=self._base_url, timeout=30.0)
        logger.info("[VoicevoxAdapter] base_url=%s speaker_id=%d", self._base_url, self._speaker_id)

    async def synthesize(self, text: str) -> bytes:
        import io
        import wave

        # Step 1: audio_query
        r = await self._client.post(
            "/audio_query",
            params={"text": text, "speaker": self._speaker_id},
        )
        r.raise_for_status()
        query = r.json()
        query["speedScale"] = self._speed_scale
        query["pitchScale"] = self._pitch_scale

        # Step 2: synthesis
        r = await self._client.post(
            "/synthesis",
            params={"speaker": self._speaker_id},
            json=query,
        )
        r.raise_for_status()
        wav_bytes = r.content

        # WAVヘッダを除去してPCMを返す
        with io.BytesIO(wav_bytes) as bio:
            with wave.open(bio, "rb") as wf:
                self._sample_rate = wf.getframerate()
                self._channels = wf.getnchannels()
                pcm = wf.readframes(wf.getnframes())
        return pcm

    def audio_format(self) -> dict[str, Any]:
        return {
            "format": "pcm_16bit",
            "sample_rate": getattr(self, "_sample_rate", 24000),
            "channels": getattr(self, "_channels", 1),
        }

    async def teardown(self) -> None:
        if self._client:
            await self._client.aclose()
