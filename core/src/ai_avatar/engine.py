"""Engine: フレームワークのオーケストレーター"""
from __future__ import annotations

import asyncio
import logging
import signal
from pathlib import Path
from typing import Any

from ai_avatar.config import Config
from ai_avatar.conversation_manager import ConversationManager
from ai_avatar.event_bus import EventBus
from ai_avatar.server.websocket import WebSocketServer
from ai_avatar.workers.llm import AnthropicAdapter, LLMWorker
from ai_avatar.workers.stt import STTWorker, WhisperAdapter
from ai_avatar.workers.tts import TTSWorker, VoicevoxAdapter

logger = logging.getLogger(__name__)


class Engine:
    """アバタープロジェクトのエントリーポイントから呼び出されるオーケストレーター。

    使用例（avatar-foo/brain/main.py）:
        from ai_avatar.engine import Engine
        import asyncio

        async def main():
            engine = Engine(config_dir="./config", data_dir="./data")
            await engine.run()

        asyncio.run(main())
    """

    def __init__(self, config_dir: str | Path, data_dir: str | Path = "./data") -> None:
        self._config_dir = Path(config_dir)
        self._data_dir = Path(data_dir)
        self._config: Config | None = None
        self._bus: EventBus | None = None
        self._workers: list[Any] = []
        self._ws_server: WebSocketServer | None = None
        self._cm: ConversationManager | None = None

    async def run(self) -> None:
        """起動してCtrl+Cで停止するまでブロックする"""
        await self._startup()
        try:
            stop_event = asyncio.Event()
            loop = asyncio.get_running_loop()
            for sig in (signal.SIGINT, signal.SIGTERM):
                try:
                    loop.add_signal_handler(sig, stop_event.set)
                except NotImplementedError:
                    # Windows では add_signal_handler が使えない場合がある
                    pass
            logger.info("[Engine] running. Press Ctrl+C to stop.")
            await stop_event.wait()
        except (KeyboardInterrupt, asyncio.CancelledError):
            pass
        finally:
            await self._shutdown()

    # ── 起動・停止 ────────────────────────────────────────────────

    async def _startup(self) -> None:
        logger.info("[Engine] starting up")

        self._config = Config(self._config_dir)
        personality = self._config.get("personality", {})
        logger.info("[Engine] personality name=%r", personality.get("name", "<not set>"))
        self._bus = EventBus()

        # ConversationManager
        self._cm = ConversationManager(self._bus)
        await self._cm.start()

        # Workers
        stt_worker = self._build_stt_worker()
        llm_worker = self._build_llm_worker()
        tts_worker = self._build_tts_worker()
        self._workers = [stt_worker, llm_worker, tts_worker]

        for worker in self._workers:
            await worker.start()

        # WebSocketServer
        server_config = self._config.get("server", {})
        host = server_config.get("host", "localhost")
        port = server_config.get("port", 8765)
        self._ws_server = WebSocketServer(self._bus, host=host, port=port)
        await self._ws_server.start()

        logger.info("[Engine] startup complete")

    async def _shutdown(self) -> None:
        logger.info("[Engine] shutting down")
        if self._ws_server:
            await self._ws_server.stop()
        for worker in reversed(self._workers):
            await worker.stop()
        if self._cm:
            await self._cm.stop()
        logger.info("[Engine] shutdown complete")

    # ── Worker構築 ────────────────────────────────────────────────

    def _build_stt_worker(self) -> STTWorker:
        assert self._config and self._bus
        stt_config = self._config.get("stt", {})
        engine = stt_config.get("engine", "whisper")

        if engine == "whisper":
            whisper_config = stt_config.get("whisper", {})
            adapter = WhisperAdapter(whisper_config)
        else:
            raise ValueError(f"Unsupported STT engine: {engine}")

        return STTWorker(self._bus, stt_config, adapter)

    def _build_llm_worker(self) -> LLMWorker:
        assert self._config and self._bus and self._cm
        llm_config = self._config.get("llm", {})
        personality = self._config.get("personality", {})
        engine = llm_config.get("engine", "anthropic")

        if engine == "anthropic":
            anthropic_config = llm_config.get("anthropic", {})
            adapter = AnthropicAdapter(anthropic_config)
        else:
            raise ValueError(f"Unsupported LLM engine: {engine}")

        return LLMWorker(self._bus, llm_config, personality, self._cm, adapter)

    def _build_tts_worker(self) -> TTSWorker:
        assert self._config and self._bus
        tts_config = self._config.get("tts", {})
        engine = tts_config.get("engine", "voicevox")

        if engine == "voicevox":
            voicevox_config = tts_config.get("voicevox", {})
            adapter = VoicevoxAdapter(voicevox_config)
        else:
            raise ValueError(f"Unsupported TTS engine: {engine}")

        return TTSWorker(self._bus, tts_config, adapter)
