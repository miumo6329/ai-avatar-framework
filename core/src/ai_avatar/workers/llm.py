"""LLMWorker: LLM呼び出しとストリーミング応答管理"""
from __future__ import annotations

import json
import logging
from typing import Any

from ai_avatar.conversation_manager import ConversationManager
from ai_avatar.event_bus import EventBus
from ai_avatar.workers.base import BaseWorker

logger = logging.getLogger(__name__)


class LLMWorker(BaseWorker):
    """LLMWorker（Claude Sonnet / Anthropic API）。

    購読するイベント:
    - stt.final        → コンテキスト構築 → LLM APIリクエスト
    - memory.context   → RAG検索結果を保持（将来実装）
    - turn.interrupt   → 進行中のLLMリクエストをキャンセル

    発行するイベント:
    - llm.response_chunk: テキストチャンク（action=respond時のみ）
    - llm.response_done:  応答完了
    """

    def __init__(
        self,
        event_bus: EventBus,
        config: dict[str, Any],
        personality: dict[str, Any],
        conversation_manager: ConversationManager,
    ) -> None:
        super().__init__(event_bus, config)
        self._personality = personality
        self._cm = conversation_manager
        self._client: Any = None
        self._current_task: Any = None
        self._history: list[dict[str, str]] = []
        self._rag_context: str | None = None

    async def setup(self) -> None:
        import anthropic
        api_key = self._config.get("api_key")
        self._client = anthropic.AsyncAnthropic(api_key=api_key) if api_key else anthropic.AsyncAnthropic()
        self.subscribe("stt.final")
        self.subscribe("memory.context")
        self.subscribe("turn.interrupt")
        logger.info("[LLMWorker] ready")

    async def teardown(self) -> None:
        if self._current_task and not self._current_task.done():
            self._current_task.cancel()

    async def _handle(self, event_type: str, data: Any) -> None:
        if event_type == "stt.final":
            text = data.get("text", "")
            if text:
                import asyncio
                self._current_task = asyncio.create_task(
                    self._respond(text), name="llm-respond"
                )
        elif event_type == "memory.context":
            self._rag_context = data.get("context", "")
        elif event_type == "turn.interrupt":
            if self._current_task and not self._current_task.done():
                self._current_task.cancel()
                logger.info("[LLMWorker] response cancelled by interrupt")

    # ── 応答生成 ──────────────────────────────────────────────────

    async def _respond(self, user_text: str) -> None:
        import asyncio

        # 蓄積発話 + 今回の発話を結合
        accumulated = self._cm.pending_utterance + [user_text]
        full_input = "".join(accumulated)

        messages = self._build_messages(full_input)
        system_prompt = self._build_system_prompt()
        logger.debug("[LLMWorker] system_prompt:\n%s", system_prompt)
        model = self._config.get("model", "claude-sonnet-4-6")
        temperature = self._config.get("temperature", 0.7)
        max_tokens = self._config.get("max_tokens", 1024)

        tool = self._reply_tool()
        collected_text = ""

        try:
            async with self._client.messages.stream(
                model=model,
                max_tokens=max_tokens,
                temperature=temperature,
                system=system_prompt,
                messages=messages,
                tools=[tool],
                tool_choice={"type": "tool", "name": "reply"},
            ) as stream:
                partial_json = ""
                last_sent_len = 0

                async for event in stream:
                    if event.type == "content_block_delta":
                        delta = event.delta
                        if delta.type == "input_json_delta":
                            partial_json += delta.partial_json
                            new_text = _extract_text_delta(partial_json, last_sent_len)
                            if new_text:
                                last_sent_len += len(new_text)
                                collected_text += new_text
                                await self._bus.publish("llm.response_chunk", {"text": new_text})

                # 完全なJSON確定後にactionを確認
                final_msg = await stream.get_final_message()

        except asyncio.CancelledError:
            return
        except Exception:
            logger.exception("[LLMWorker] API error")
            self._set_status_degraded()
            return

        # tool_useブロックからactionとtextを取得
        action, response_text = _parse_reply(final_msg)
        logger.info("[LLMWorker] action=%s text=%r", action, response_text[:50] if response_text else "")

        if action == "wait":
            # 発話が途中 → pending_utteranceに蓄積
            self._cm.accumulate_utterance(user_text)
            await self._bus.publish("llm.response_done", {"text": "", "action": "wait"})
            return

        # action == "respond"
        # 会話履歴に追加
        self._history.append({"role": "user", "content": full_input})
        self._history.append({"role": "assistant", "content": response_text})
        self._rag_context = None

        await self._bus.publish("llm.response_done", {"text": response_text, "action": "respond"})

    # ── プロンプト構築 ────────────────────────────────────────────

    def _build_system_prompt(self) -> str:
        name = self._personality.get("name", "アシスタント")
        description = self._personality.get("description", "")
        speaking_style = self._personality.get("speaking_style", "")
        constraints = self._personality.get("constraints", "")

        character = f"あなたは{name}です。\n{description}"
        if speaking_style:
            style_lines = speaking_style.strip().splitlines()
            character += "\n\n話し方:\n" + "\n".join(f"- {l}" for l in style_lines if l.strip())
        if constraints:
            constraint_lines = constraints.strip().splitlines()
            character += "\n\n制約:\n" + "\n".join(f"- {l}" for l in constraint_lines if l.strip())

        rules = (
            "\n\n## 応答ルール\n"
            "- ユーザーの発話が意味的に完結している場合は action=respond を返すこと。\n"
            "- 発話が途中（「えーと」「それで」「あの...」など間投詞・文末不完全）の場合は action=wait を返すこと。\n"
            "- 応答は会話の流れに自然に続く長さにすること。長々と説明しない。\n"
            "- 知覚情報や記憶が提供された場合は、不自然に列挙せず会話に自然に織り込むこと。"
        )
        return character + rules

    def _build_messages(self, current_input: str) -> list[dict[str, Any]]:
        messages: list[dict[str, Any]] = []

        # 会話履歴
        for turn in self._history:
            messages.append({"role": turn["role"], "content": turn["content"]})

        # 現在のユーザー入力（RAGコンテキストがあれば付与）
        user_content = ""
        if self._rag_context:
            user_content += f"## 関連する過去の記憶\n{self._rag_context}\n\n"
        user_content += f"Human: {current_input}"
        messages.append({"role": "user", "content": user_content})

        return messages

    @staticmethod
    def _reply_tool() -> dict[str, Any]:
        return {
            "name": "reply",
            "description": "ユーザーへの応答を返す。発話が途中の場合はwaitを返す。",
            "input_schema": {
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": ["respond", "wait"],
                        "description": (
                            "respond: ユーザーの発話が意味的に完結している。応答テキストを返す。\n"
                            "wait: 発話が途中（「えーと」などの間投詞、文末が不完全）。textは空文字で返す。"
                        ),
                    },
                    "text": {
                        "type": "string",
                        "description": "action=respond の場合の応答テキスト。action=wait の場合は空文字。",
                    },
                },
                "required": ["action", "text"],
            },
        }

    def _set_status_degraded(self) -> None:
        from ai_avatar.workers.base import WorkerStatus
        self._set_status(WorkerStatus.DEGRADED)


# ── ユーティリティ ─────────────────────────────────────────────────────────────

def _extract_text_delta(partial_json: str, already_sent: int) -> str:
    """部分的なJSONから "text" フィールドの増分を抽出する。

    例: '{"action": "respond", "text": "今日は' → "今日は"（の増分）
    """
    # "text": " の後の文字列を抽出する簡易パーサー
    marker = '"text":'
    idx = partial_json.find(marker)
    if idx == -1:
        return ""

    after_marker = partial_json[idx + len(marker):].lstrip()
    if not after_marker.startswith('"'):
        return ""

    # 開き引用符の直後から文字列を取り出す
    content = after_marker[1:]
    # エスケープされた引用符に注意しながら、閉じ引用符を探す
    # 部分JSONなので閉じ引用符がない場合は現在のcontent全体が増分候補
    result = []
    i = 0
    while i < len(content):
        c = content[i]
        if c == "\\":
            if i + 1 < len(content):
                escape_map = {"n": "\n", "t": "\t", "r": "\r", '"': '"', "\\": "\\"}
                result.append(escape_map.get(content[i + 1], content[i + 1]))
                i += 2
            else:
                break
        elif c == '"':
            break  # 閉じ引用符（完全なJSON）
        else:
            result.append(c)
            i += 1

    full_text = "".join(result)
    return full_text[already_sent:]


def _parse_reply(message: Any) -> tuple[str, str]:
    """Anthropic APIのfinal_messageからaction, textを取り出す"""
    for block in message.content:
        if block.type == "tool_use" and block.name == "reply":
            action = block.input.get("action", "respond")
            text = block.input.get("text", "")
            return action, text
    return "respond", ""
