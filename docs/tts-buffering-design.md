# TTS音声バッファリング設計

## 概要

TTSWorkerにおけるテキスト分割、音声合成キュー管理、バッファリング戦略の設計を定義する。
現時点ではVOICEVOX / AivisSpeech（VOICEVOX互換API）の一括合成方式を前提とする。

---

## テキストチャンキング

LLMWorkerからストリーミングで受信する `llm.response_chunk` を、TTS合成に適した単位に分割する。

### 分割ルール

1. **句読点分割（基本）**: 句読点（。！？）を文の区切りとして検出し、1文単位でTTSに送る
2. **文字数分割（補助）**: 句読点が出現しないまま一定文字数を超えた場合、読点（、）やその他の自然な区切りで強制分割する

詳細な閾値・分割ロジックは実装時にチューニングする。

### チャンク蓄積フロー

```
llm.response_chunk（トークン単位で到着）
  ↓
テキストバッファに蓄積
  ↓
句読点検出 or 文字数上限 → 1文として確定
  ↓
TTS合成キューへ投入
```

---

## 音声合成パイプライン

### 1つ先読み方式

VOICEVOX APIは並列リクエストに対応していないため、**1つ先読み（prefetch-1）** を採用する。
現在再生中の音声チャンクがある間に、次の1文の合成を開始しておく。

```
時間軸 →

sentence₁ [===合成===]
audio₁               [========再生========]
sentence₂             [===合成===]
audio₂                                    [========再生========]
sentence₃                                  [===合成===]
audio₃                                                         [====再生====]
```

再生と合成がオーバーラップすることで、文間のギャップを最小化する。

### キュー上限

キュー上限は設けない。対話アプリにおけるアバターの1回の応答は比較的短い文章であり、
テキスト生成が再生速度を大幅に上回り続けるシチュエーションは想定されない。

### アンダーラン時の挙動

合成が再生に追いつかない場合（次の音声チャンクが未完成）、特別な処理は行わない。
音声が準備でき次第、再生を再開する。自然な「間」として許容する。

---

## 割り込み時のフラッシュ

`turn.interrupt` / `tts.stop` 受信時:

1. **テキストバッファのクリア**: 未確定のテキストチャンクを破棄
2. **合成キューのクリア**: 未送信の合成済み音声チャンクを破棄
3. **合成中リクエストのキャンセル**: VOICEVOX APIへのHTTPリクエストのキャンセル可否はAPI仕様次第（実装時に調査）
4. **Unity Bridge側再生停止**: `tts.stop` WebSocketメッセージによりUnity Bridge側の再生バッファもクリア（protocol.mdで既定済み）

---

## 初回発話レイテンシ

LLMの最初のテキストチャンクからTTS音声の再生開始までのレイテンシを最小化するため、
最初の文が句読点で確定した時点で即座に合成を開始する。

```
LLM応答開始
  ↓ llm.response_chunk（トークン単位で到着）
  ↓ テキストバッファに蓄積
  ↓ 最初の句読点検出 → sentence₁ 確定
  ↓ TTS合成開始（VOICEVOX API呼び出し）
  ↓ 合成完了 → tts.audio_chunk 送信
  ↓ Unity Bridge側で再生開始 ← ここまでが初回レイテンシ
```

VOICEVOX一括合成の場合、部分合成（文が確定する前に合成開始）は不可能なため、
初回レイテンシは「最初の文の確定待ち + VOICEVOX合成時間」となる。

---

## リップシンク・表情同期

- **リップシンク**: Unity側の音声駆動リップシンク機能に委譲する。Core側からviseme情報等は送信しない
- **表情同期**: ReactionWorkerが `llm.response_chunk` から判定する表情は、音声再生との厳密な同期は行わない（現時点では実装しない）

---

## 音声フォーマット

現時点ではVOICEVOX / AivisSpeech のみを対象とし、両者はVOICEVOX APIに準拠する。
将来的に異なるTTSエンジンを追加する場合、フォーマット変換・リサンプリングの責務はAdapter（TTSAdapter）側に持たせる。

Core側は統一フォーマットを定義せず、TTSAdapterが `tts.audio_chunk` のペイロードに適切なフォーマット情報（sample_rate, channels, format）を付与する。

---

## TTSWorker内部状態

```python
class TTSWorkerState:
    text_buffer: str                    # LLMチャンクの蓄積バッファ
    synthesis_queue: asyncio.Queue      # 合成待ちの文リスト
    current_synthesis: asyncio.Task     # 現在合成中のタスク（キャンセル用）
    is_playing: bool                    # Unity Bridge側で再生中かどうか
```

### 状態遷移

```
IDLE ──(llm.response_chunk)──► BUFFERING
BUFFERING ──(文確定)──► SYNTHESIZING
SYNTHESIZING ──(合成完了)──► SENDING
SENDING ──(キュー空 & LLM完了)──► IDLE

任意の状態 ──(tts.stop / turn.interrupt)──► IDLE（全バッファクリア）
```
