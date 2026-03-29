"""ConversationManager: 会話状態マシンとイベント優先度判断"""
from __future__ import annotations

import asyncio
import logging
from enum import Enum
from typing import Any

from ai_avatar.event_bus import EventBus

logger = logging.getLogger(__name__)


class ConversationState(str, Enum):
    IDLE = "idle"
    LISTENING = "listening"
    PROCESSING = "processing"
    SPEAKING = "speaking"
    INTERRUPTED = "interrupted"


class ConversationManager:
    """会話状態マシン。

    購読するイベント:
    - audio.input: is_speech_start / is_speech_end フラグで状態遷移
    - stt.final: LISTENING → PROCESSING
    - llm.response_chunk: PROCESSING → SPEAKING（最初のチャンク）
    - llm.response_done: SPEAKING → IDLE
    - tts.stop: 任意 → INTERRUPTED → LISTENING

    発行するイベント:
    - turn.interrupt: 割り込み検出時
    - state.update: 状態変化時（WebSocketServerへ）
    """

    INTERRUPT_GRACE_MS = 300  # 割り込み判定猶予時間

    def __init__(self, event_bus: EventBus) -> None:
        self._bus = event_bus
        self._state = ConversationState.IDLE
        self._interrupt_task: asyncio.Task | None = None
        # wait蓄積バッファ: action=wait のターンのstt.finalを蓄積
        self._pending_utterance: list[str] = []

    async def start(self) -> None:
        self._bus.subscribe("audio.input", self._on_audio_input)
        self._bus.subscribe("stt.final", self._on_stt_final)
        self._bus.subscribe("llm.response_chunk", self._on_llm_response_chunk)
        self._bus.subscribe("llm.response_done", self._on_llm_response_done)
        self._bus.subscribe("tts.stop", self._on_tts_stop)
        logger.info("[ConversationManager] started, state=%s", self._state)

    async def stop(self) -> None:
        self._bus.unsubscribe("audio.input", self._on_audio_input)
        self._bus.unsubscribe("stt.final", self._on_stt_final)
        self._bus.unsubscribe("llm.response_chunk", self._on_llm_response_chunk)
        self._bus.unsubscribe("llm.response_done", self._on_llm_response_done)
        self._bus.unsubscribe("tts.stop", self._on_tts_stop)

    # ── イベントハンドラ ──────────────────────────────────────────

    async def _on_audio_input(self, _: str, data: dict[str, Any]) -> None:
        is_speech_start = data.get("is_speech_start", False)
        is_speech_end = data.get("is_speech_end", False)

        if is_speech_start:
            if self._state == ConversationState.SPEAKING:
                # アバター発話中に音声検出 → 猶予付き割り込み
                await self._schedule_interrupt()
            elif self._state == ConversationState.IDLE:
                await self._transition(ConversationState.LISTENING)

        if is_speech_end:
            if self._interrupt_task and not self._interrupt_task.done():
                # 割り込み猶予中にis_speech_endが来ることはほぼないが念のためキャンセル
                self._interrupt_task.cancel()

    async def _on_stt_final(self, _: str, data: dict[str, Any]) -> None:
        if self._state in (ConversationState.LISTENING, ConversationState.INTERRUPTED):
            await self._transition(ConversationState.PROCESSING)

    async def _on_llm_response_chunk(self, _: str, data: dict[str, Any]) -> None:
        if self._state == ConversationState.PROCESSING:
            await self._transition(ConversationState.SPEAKING)

    async def _on_llm_response_done(self, _: str, data: dict[str, Any]) -> None:
        # action=wait の場合はSPEAKINGに遷移しないのでここは通らないが念のため
        if self._state in (ConversationState.SPEAKING, ConversationState.PROCESSING):
            self._pending_utterance.clear()
            await self._transition(ConversationState.IDLE)

    async def _on_tts_stop(self, _: str, data: dict[str, Any]) -> None:
        await self._transition(ConversationState.INTERRUPTED)
        # すぐにLISTENINGへ
        await self._transition(ConversationState.LISTENING)

    # ── 割り込み制御 ──────────────────────────────────────────────

    async def _schedule_interrupt(self) -> None:
        """猶予時間後に割り込みイベントを発行する"""
        if self._interrupt_task and not self._interrupt_task.done():
            return  # 既にスケジュール済み

        async def _do_interrupt() -> None:
            await asyncio.sleep(self.INTERRUPT_GRACE_MS / 1000)
            logger.info("[ConversationManager] interrupt triggered")
            await self._bus.publish("turn.interrupt", {})
            await self._bus.publish("tts.stop", {})

        self._interrupt_task = asyncio.create_task(_do_interrupt(), name="interrupt-grace")

    # ── 状態遷移 ──────────────────────────────────────────────────

    async def _transition(self, new_state: ConversationState) -> None:
        if self._state == new_state:
            return
        logger.info("[ConversationManager] %s → %s", self._state, new_state)
        self._state = new_state
        await self._bus.publish("state.update", {"conversation_state": new_state.value})

    @property
    def state(self) -> ConversationState:
        return self._state

    @property
    def pending_utterance(self) -> list[str]:
        """action=wait で蓄積した発話バッファ"""
        return self._pending_utterance

    def accumulate_utterance(self, text: str) -> None:
        """LLMがwaitを返したターンのstt.finalを蓄積する"""
        self._pending_utterance.append(text)
