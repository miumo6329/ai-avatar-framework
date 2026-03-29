"""test_client.py: Unity Adapterの代わりにPython CoreへWAVファイルを送信するテストクライアント。

使い方:
  # 1. Python Coreを起動（別ターミナル）
  #    cd core && uv run python -m ai_avatar.engine  ← またはアバターのmain.py

  # 2. このスクリプトを実行
  #    uv run python tests/test_client.py path/to/speech.wav

WAVファイル（16kHz, 16bit, mono推奨）を受け取り、
Adapterとして接続 → audio.inputで送信 → tts.audioを受信して再生/保存する。
"""
from __future__ import annotations

import argparse
import asyncio
import base64
import io
import json
import logging
import struct
import time
import wave
from pathlib import Path

import websockets

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

CHUNK_SIZE = 4096  # 送信チャンクサイズ（バイト）
SERVER_URL = "ws://localhost:8765"


async def run(wav_path: Path, output_path: Path | None = None) -> None:
    async with websockets.connect(SERVER_URL) as ws:
        logger.info("Connected to %s", SERVER_URL)

        # 1. connection.hello
        await ws.send(json.dumps({
            "type": "connection.hello",
            "timestamp": time.time(),
            "payload": {
                "adapter_type": "test_client",
                "capabilities": ["audio_input", "audio_output"],
                "expressions": {},
                "animations": {},
            },
        }))

        # 2. connection.ready 待ち
        raw = await ws.recv()
        msg = json.loads(raw)
        assert msg["type"] == "connection.ready", f"Expected connection.ready, got {msg['type']}"
        session_id = msg["payload"]["session_id"]
        logger.info("Session started: %s", session_id)

        # 3. WAVファイルを読み込む（実際のサンプルレートを取得）
        audio_chunks, sample_rate = _load_wav_chunks(wav_path)
        logger.info("Sending %d audio chunks from %s (sample_rate=%d)", len(audio_chunks), wav_path, sample_rate)

        # 4. audio.input 送信（Adapterのように発話区間として送る）
        for i, chunk in enumerate(audio_chunks):
            is_start = i == 0
            is_end = i == len(audio_chunks) - 1
            payload = {
                "data": base64.b64encode(chunk).decode(),
                "format": "pcm_16bit",
                "sample_rate": sample_rate,
                "channels": 1,
                "is_speech_start": is_start,
                "is_speech_end": is_end,
            }
            await ws.send(json.dumps({
                "type": "audio.input",
                "timestamp": time.time(),
                "payload": payload,
            }))
            await asyncio.sleep(0.01)  # 少し間を置く

        logger.info("Audio sent. Waiting for response...")

        # 5. 応答受信ループ（tts.audioまたはllm.doneまで）
        collected_audio: list[bytes] = []
        async with asyncio.timeout(30):
            async for raw in ws:
                msg = json.loads(raw)
                msg_type = msg["type"]
                payload = msg.get("payload", {})

                if msg_type == "stt.final":
                    logger.info("[STT] %r", payload.get("text", ""))

                elif msg_type == "llm.response":
                    print(payload.get("chunk", ""), end="", flush=True)

                elif msg_type == "llm.done":
                    print()  # 改行
                    logger.info("[LLM] done: %r", payload.get("full_text", "")[:60])

                elif msg_type == "tts.audio":
                    pcm = base64.b64decode(payload["data"])
                    if pcm:
                        collected_audio.append(pcm)
                    if payload.get("is_final"):
                        logger.info("[TTS] received all audio (%d bytes)", sum(len(c) for c in collected_audio))
                        break

                elif msg_type == "state.update":
                    logger.info("[State] %s", payload.get("conversation_state", ""))

        # 6. 音声を保存 or 再生
        if collected_audio:
            audio_data = b"".join(collected_audio)
            if output_path:
                _save_wav(audio_data, output_path, sample_rate=24000)
                logger.info("Saved to %s", output_path)
            else:
                _play_audio(audio_data, sample_rate=24000)


def _load_wav_chunks(path: Path) -> tuple[list[bytes], int]:
    """WAVファイルをPCMチャンク列として読み込む。実際のサンプルレートも返す。"""
    with wave.open(str(path), "rb") as wf:
        n_channels = wf.getnchannels()
        sampwidth = wf.getsampwidth()
        framerate = wf.getframerate()
        pcm = wf.readframes(wf.getnframes())

    logger.info("WAV: %dch, %dbit, %dHz", n_channels, sampwidth * 8, framerate)

    # ステレオ→モノラル変換（簡易）
    if n_channels == 2:
        samples = struct.unpack(f"<{len(pcm)//2}h", pcm)
        mono = [int((samples[i] + samples[i+1]) / 2) for i in range(0, len(samples), 2)]
        pcm = struct.pack(f"<{len(mono)}h", *mono)

    chunks = [pcm[i:i+CHUNK_SIZE] for i in range(0, len(pcm), CHUNK_SIZE)]
    return chunks, framerate


def _save_wav(pcm: bytes, path: Path, sample_rate: int = 24000) -> None:
    with wave.open(str(path), "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)  # 16bit
        wf.setframerate(sample_rate)
        wf.writeframes(pcm)


def _play_audio(pcm: bytes, sample_rate: int = 24000) -> None:
    """PCMをプラットフォームネイティブに再生する（簡易実装）"""
    try:
        import sounddevice as sd  # type: ignore[import-untyped]
        import numpy as np
        audio = np.frombuffer(pcm, dtype=np.int16).astype(np.float32) / 32768.0
        sd.play(audio, samplerate=sample_rate)
        sd.wait()
    except ImportError:
        # sounddeviceが無ければWAVとして保存
        out = Path("output.wav")
        _save_wav(pcm, out, sample_rate)
        logger.info("sounddevice not installed. Saved audio to %s", out)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="AI Avatar test client")
    parser.add_argument("wav", type=Path, help="Input WAV file (16kHz, 16bit, mono)")
    parser.add_argument("--output", "-o", type=Path, default=None, help="Output WAV file (default: play audio)")
    args = parser.parse_args()

    asyncio.run(run(args.wav, args.output))
