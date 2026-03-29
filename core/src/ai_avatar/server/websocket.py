"""WebSocketServer: AdapterとPython Core間の通信"""
from __future__ import annotations

import asyncio
import base64
import json
import logging
import time
import uuid
from typing import Any

import websockets
from websockets.server import ServerConnection

from ai_avatar.event_bus import EventBus

logger = logging.getLogger(__name__)


class WebSocketServer:
    """WebSocketサーバー。

    Adapter → Core:
      - connection.hello → connection.ready を返し、session_idを発行
      - audio.input     → EventBusに audio.input を発行
      - vision.frame    → EventBusに vision.frame を発行

    Core → Adapter (EventBus購読):
      - stt.partial / stt.final → Adapterに転送
      - llm.response_chunk / llm.response_done → Adapterに転送
      - tts.audio_chunk → Adapterに転送
      - reaction.expression → expression.set として転送
      - reaction.animation  → animation.play として転送
      - tts.stop            → Adapterに転送
      - state.update        → Adapterに転送
    """

    def __init__(self, event_bus: EventBus, host: str = "localhost", port: int = 8765) -> None:
        self._bus = event_bus
        self._host = host
        self._port = port
        self._connection: ServerConnection | None = None
        self._session_id: str | None = None
        self._server: Any = None

    async def start(self) -> None:
        # Core → Adapter イベントを購読
        self._bus.subscribe("stt.partial", self._relay_stt_partial)
        self._bus.subscribe("stt.final", self._relay_stt_final)
        self._bus.subscribe("llm.response_chunk", self._relay_llm_chunk)
        self._bus.subscribe("llm.response_done", self._relay_llm_done)
        self._bus.subscribe("tts.audio_chunk", self._relay_tts_audio)
        self._bus.subscribe("tts.stop", self._relay_tts_stop)
        self._bus.subscribe("reaction.expression", self._relay_expression)
        self._bus.subscribe("reaction.animation", self._relay_animation)
        self._bus.subscribe("state.update", self._relay_state)

        self._server = await websockets.serve(
            self._handle_connection,
            self._host,
            self._port,
        )
        logger.info("[WebSocketServer] listening on ws://%s:%d", self._host, self._port)

    async def stop(self) -> None:
        if self._server:
            self._server.close()
            await self._server.wait_closed()

    # ── 接続処理 ──────────────────────────────────────────────────

    async def _handle_connection(self, ws: ServerConnection) -> None:
        if self._connection is not None:
            logger.warning("[WebSocketServer] already connected, rejecting new connection")
            await ws.close(1008, "Already connected")
            return

        self._connection = ws
        logger.info("[WebSocketServer] adapter connected: %s", ws.remote_address)

        try:
            async for raw in ws:
                await self._dispatch(raw)
        except websockets.exceptions.ConnectionClosed:
            pass
        finally:
            logger.info("[WebSocketServer] adapter disconnected")
            self._connection = None
            self._session_id = None
            await self._bus.publish("connection.disconnected", {})

    async def _dispatch(self, raw: str | bytes) -> None:
        try:
            msg = json.loads(raw)
        except json.JSONDecodeError:
            logger.warning("[WebSocketServer] invalid JSON: %r", raw)
            return

        msg_type = msg.get("type", "")
        payload = msg.get("payload", {})

        match msg_type:
            case "connection.hello":
                await self._on_hello(payload)
            case "audio.input":
                await self._on_audio_input(payload)
            case "vision.frame":
                await self._bus.publish("vision.frame", payload)
            case _:
                logger.debug("[WebSocketServer] unknown message type: %s", msg_type)

    # ── Adapter → Core ────────────────────────────────────────────

    async def _on_hello(self, payload: dict[str, Any]) -> None:
        self._session_id = str(uuid.uuid4())
        logger.info("[WebSocketServer] hello from adapter, session=%s", self._session_id)
        await self._send("connection.ready", {"session_id": self._session_id})
        await self._bus.publish("connection.hello", {
            "session_id": self._session_id,
            **payload,
        })

    async def _on_audio_input(self, payload: dict[str, Any]) -> None:
        # Base64デコードしてバイト列に変換
        raw_b64 = payload.get("data", "")
        try:
            audio_bytes = base64.b64decode(raw_b64)
        except Exception:
            logger.warning("[WebSocketServer] invalid base64 in audio.input")
            return

        await self._bus.publish("audio.input", {
            "data": audio_bytes,
            "format": payload.get("format", "pcm_16bit"),
            "sample_rate": payload.get("sample_rate", 16000),
            "channels": payload.get("channels", 1),
            "is_speech_start": payload.get("is_speech_start", False),
            "is_speech_end": payload.get("is_speech_end", False),
        })

    # ── Core → Adapter (relay) ────────────────────────────────────

    async def _relay_stt_partial(self, _: str, data: dict[str, Any]) -> None:
        await self._send("stt.partial", {"text": data.get("text", ""), "is_final": False})

    async def _relay_stt_final(self, _: str, data: dict[str, Any]) -> None:
        await self._send("stt.final", {"text": data.get("text", ""), "is_final": True})

    async def _relay_llm_chunk(self, _: str, data: dict[str, Any]) -> None:
        await self._send("llm.response", {"chunk": data.get("text", ""), "is_final": False})

    async def _relay_llm_done(self, _: str, data: dict[str, Any]) -> None:
        await self._send("llm.done", {"full_text": data.get("text", "")})

    async def _relay_tts_audio(self, _: str, data: dict[str, Any]) -> None:
        audio_bytes: bytes = data.get("data", b"")
        await self._send("tts.audio", {
            "data": base64.b64encode(audio_bytes).decode(),
            "format": data.get("format", "pcm_16bit"),
            "sample_rate": data.get("sample_rate", 24000),
            "channels": data.get("channels", 1),
            "is_final": data.get("is_final", False),
        })

    async def _relay_tts_stop(self, _: str, data: dict[str, Any]) -> None:
        await self._send("tts.stop", {})

    async def _relay_expression(self, _: str, data: dict[str, Any]) -> None:
        await self._send("expression.set", data)

    async def _relay_animation(self, _: str, data: dict[str, Any]) -> None:
        await self._send("animation.play", data)

    async def _relay_state(self, _: str, data: dict[str, Any]) -> None:
        await self._send("state.update", data)

    # ── 送信ヘルパー ──────────────────────────────────────────────

    async def _send(self, msg_type: str, payload: dict[str, Any]) -> None:
        if self._connection is None:
            return
        msg = json.dumps({
            "type": msg_type,
            "timestamp": time.time(),
            "payload": payload,
        }, ensure_ascii=False)
        try:
            await self._connection.send(msg)
        except websockets.exceptions.ConnectionClosed:
            logger.debug("[WebSocketServer] send failed: connection closed")
