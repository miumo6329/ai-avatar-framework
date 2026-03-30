# STT音声認識処理設計

## 概要

STTWorkerにおける音声チャンク受信、テキスト変換、節区切り検出の設計を定義する。
VAD（Voice Activity Detection）はAdapter側の責務とし、CoreにはVAD済みの発話音声のみが届く前提。
エンジン種別（一括変換型・ストリーミング型）の差異はSTTAdapterで吸収し、上位レイヤーに対して統一インターフェースを提供する。

---

## 設計方針：VADはUnity Bridge側の責務

マイクデバイスはUnity Bridgeが管理するため、VADもUnity Bridge側で実施する。
Unity BridgeはVADで発話区間を検出し、**発話音声のみ**をCoreに送信する。無音区間はネットワーク転送しない。

```
Unity Bridge（VRヘッドセット等）         Python Core（ホストPC等）
──────────────────────────────           ──────────────────────────
[マイク]
  ↓ 生PCM音声（全区間）
  → VAD判定（Silero等）
  ↓ 発話区間のみ抽出
  → audio.input (is_speech_start=true)  →  WebSocketServer
  → audio.input (チャンク...)                    ↓
  → audio.input (is_speech_end=true)    →  STTWorker
                                               ├── WhisperAdapter
                                               └── GoogleSTTAdapter
```

この設計の利点:
- UnityとCoreが別マシン（VRヘッドセット等）でもネットワーク効率が良い
- CoreはSTTに特化でき、VADライブラリへの依存が不要
- どのSTTエンジンを使っても「VAD済み音声を受け取る」前提で統一できる

---

## Unity Bridge側VAD実装

Unity Bridgeの責務はマイク入力からVADを経て発話音声を送信すること。
フレームワークはUnity向けの参照実装を提供する。

### Unity向けVADライブラリ候補

| ライブラリ | 方式 | 精度 | 備考 |
|-----------|-----|------|------|
| **Silero VAD（ONNX）** | Unity Sentisでモデル実行 | 高 | 推奨 |
| WebRTC VAD | Cライブラリ → Native Plugin | 中〜高 | 軽量。Unity向けラッパーあり |
| エネルギー閾値 | RMS振幅の閾値判定 | 低 | 簡易実装。ノイズ環境に弱い |

### 他プラットフォームでの対応

ROS等の他Bridgeも同様にBridge側でVADを実装する。
各プラットフォームのネイティブ音声APIや既存VADライブラリを活用してよい。

### VADパラメータ

Unity Bridge側の実装に委ねるが、以下を設定可能にすることを推奨:

| パラメータ | 目安 | 説明 |
|-----------|------|------|
| `silence_duration_ms` | 800ms | 発話終了と判定する無音時間 |
| `min_speech_duration_ms` | 100ms | 最短発話時間（ノイズ除去） |
| `interrupt_grace_ms` | 300ms | 割り込み判定の猶予時間（SPEAKING中に発話検出してから送信するまでの待機） |

---

## 音声入力フォーマット

Unity Bridgeから送信される `audio.input` の仕様。**発話区間のみ送信**する。

```json
{
  "type": "audio.input",
  "timestamp": 1234567890.123,
  "payload": {
    "data": "base64-encoded-audio-chunk",
    "format": "pcm_16bit",
    "sample_rate": 16000,
    "channels": 1,
    "is_speech_start": true,
    "is_speech_end": false
  }
}
```

| フラグ | 省略時 | 説明 |
|-------|-------|------|
| `is_speech_start` | false | 発話区間の最初のチャンク。ConversationManagerの割り込み検出に使用 |
| `is_speech_end` | false | 発話区間の最後のチャンク。WhisperAdapterの変換トリガー |

### エンジン別の送信パターン

**一括変換型（Whisper）向け:**
```
is_speech_start=true のチャンク（発話開始）
中間チャンク × N（省略可。Unity Bridgeが内部バッファに蓄積してもよい）
is_speech_end=true のチャンク（全発話音声を含めて一括送信も可）
```

**ストリーミング型（Google STT）向け:**
```
is_speech_start=true のチャンク（Streamingセッション開始トリガー）
中間チャンク × N（リアルタイム転送）
is_speech_end=true のチャンク（セッション終了）
```

---

## STT処理パイプライン

### 全体フロー

```
audio.input（発話区間のみ）
  ↓
WebSocketServer → EventBus → STTWorker
  ├── (一括型) is_speech_end 受信 → バッファを一括変換
  └── (ストリーミング型) is_speech_start でセッション開始、チャンクをリアルタイム転送
        ↓
節区切り検出
  ├── stt.clause 発行（ReactionWorker が購読）
  └── stt.final 発行（LLMWorker / MemoryWorker が購読）
```

### エンジン種別による処理の違い

| 項目 | 一括変換型（Whisper等） | ストリーミング型（Google STT / sherpa-onnx等） |
|-----|----------------------|-------------------------------|
| 入力 | is_speech_end までバッファ | チャンクをリアルタイム転送 |
| `stt.partial` | 非対応（発行しない） | リアルタイム発行 |
| `stt.clause` | **非対応（発行しない）** | 中間結果から節検出して発行 |
| `stt.final` | is_speech_end 後に発行 | API の is_final=true 時に発行 |
| ReactionWorkerの反応 | 発話終了後にのみ可能 | 発話中にリアルタイム |

`stt.clause` を発行するかどうかはエンジン種別によって異なり、STTAdapter実装で吸収する。

---

## STTAdapter 設計

STTWorkerはAdapterに音声チャンクを渡すだけとし、API呼び出しの実装差異はAdapter内に閉じ込める。
VAD処理はAdapter側で実施済みのため、STTAdapter内にVADは含まない。
`stt.clause` を発行するかどうかもAdapterの実装に委ねる（`stt.yaml` には記載しない）。

```
STTWorker
  └── STTAdapter (共通インターフェース: audio_chunk → callback(text, is_final))
        ├── WhisperAdapter          ← 一括変換型
        │     ├── チャンクを内部バッファに蓄積
        │     ├── is_speech_end 受信 → バッファ全体をWhisper APIに送信
        │     └── stt.partial / stt.clause は発行しない
        ├── GoogleSTTAdapter        ← ストリーミング型（クラウド）
        │     ├── is_speech_start 受信 → Streamingセッション開始
        │     ├── チャンクをリアルタイムでStreaming APIに転送
        │     ├── stt.partial / stt.clause をリアルタイム発行
        │     └── is_speech_end 受信 → セッション終了 → stt.final 発行
        ├── SherpaOnnxAdapter       ← ストリーミング型（ローカル）
        │     └── GoogleSTTAdapterと同等の挙動。ローカル推論
        └── SenseVoiceAdapter       ← ストリーミング型（ローカル）
              └── GoogleSTTAdapterと同等の挙動。ローカル推論
```

### STTエンジン候補

| エンジン | 種別 | 動作場所 | 日本語精度 | 備考 |
|---------|-----|---------|----------|------|
| **Whisper / faster-whisper** | 一括変換 | ローカル | 高 | stt.clause非対応。OpenAI API版も可 |
| **Google Cloud STT** | ストリーミング | クラウド | 高 | stt.partial対応。従量課金 |
| **sherpa-onnx** | ストリーミング | ローカル | 高 | Zipformer等複数モデル対応。推奨 |
| **SenseVoice（FunASR）** | ストリーミング | ローカル | 高 | Alibaba製。感情認識も可能 |
| **Vosk** | ストリーミング | ローカル | 中 | 軽量。枯れた実績あり |

### 共通インターフェース（概念）

```python
class STTAdapter(ABC):
    async def on_audio_chunk(
        self,
        chunk: bytes,
        is_speech_start: bool = False,
        is_speech_end: bool = False
    ) -> None:
        """音声チャンクを受け取る。VAD処理は含まない（Adapter側で実施済み）"""
        ...

    # コールバック（STTWorkerが登録）
    on_partial: Callable[[str], None]   # 中間結果
    on_clause: Callable[[str], None]    # 節区切り
    on_final: Callable[[str], None]     # 確定結果
```

---

## 節区切り検出（stt.clause）

ユーザーが話している間、ReactionWorkerへリアルタイムにテキストを供給するために節区切りを検出する。
LLMへのリクエストには使用せず、**表情・アニメーション生成のための先行処理専用**。

### 検出条件

1. **句読点出現**: STT中間結果に `。、？！` が出現した時点で節確定
2. **テキスト量増加**: 前回送信時から一定文字数（目安: 10〜15字）以上増加した場合に強制分割

> 一括変換型（Whisper等）は `stt.clause` を発行しない。発話終了後に `stt.final` のみ発行する。
> ストリーミング型（Google STT / sherpa-onnx等）はSTT中間結果から節を検出し、`stt.clause` をリアルタイム発行する。
> この違いはSTTAdapter実装で吸収し、`stt.yaml` には記載しない。

### 節区切りフロー（ストリーミング型）

```
STT中間結果「今日さ、仕事で嫌なことが」
  ↓ 読点「、」検出
  → stt.clause「今日さ、」 発行

STT中間結果「今日さ、仕事で嫌なことがあって、」
  ↓ 読点「、」検出
  → stt.clause「仕事で嫌なことがあって、」 発行（差分）

is_speech_end 受信 → 認識確定
  → stt.final「今日さ、仕事で嫌なことがあって、落ち込んでるんだよね。」 発行
```

---

## 発行イベント仕様

### `stt.partial`

```json
{
  "type": "stt.partial",
  "timestamp": 1234567890.123,
  "payload": {
    "text": "今日の天気は",
    "is_final": false
  }
}
```

> ストリーミング型エンジンのみ発行。一括変換型では発行しない。

### `stt.clause`

```json
{
  "type": "stt.clause",
  "timestamp": 1234567890.123,
  "payload": {
    "text": "今日さ、"
  }
}
```

> ReactionWorker が購読し、表情・アニメーション生成に使用。

### `stt.final`

```json
{
  "type": "stt.final",
  "timestamp": 1234567890.123,
  "payload": {
    "text": "今日さ、仕事で嫌なことがあって、落ち込んでるんだよね。",
    "is_final": true
  }
}
```

> LLMWorker・MemoryWorker が購読し、本応答生成・RAG検索をトリガーする。

---

## 割り込み時の挙動

アバター発話中（SPEAKING状態）にユーザーが話し始めた場合、Unity Bridgeの割り込み検出が起動する。

### 割り込み検出フロー

```
Unity Bridge: SPEAKING中に発話検出
  ↓ interrupt_grace_ms（デフォルト300ms）待機
  ↓ 発話が継続していれば割り込みと判定
  → audio.input (is_speech_start=true) を Core に送信

ConversationManager: SPEAKING状態中に is_speech_start=true を受信
  → turn.interrupt 発行
      ├── TTSWorker: テキストバッファ・合成キューをクリア
      └── tts.stop を Unity Bridge に送信
```

STTWorker側は割り込み時に特別な処理は行わない。
is_speech_start=true を受け取ったタイミングで通常通り認識セッションを開始する。

---

## 初回認識レイテンシ

ユーザー発話開始（Unity Bridge側VAD検出）から `stt.final` がLLMWorkerに届くまでの遅延。

### 一括変換型（Whisper）

```
Unity Bridge: 発話開始検出（VAD）
  ↓ ユーザー発話中（バッファ蓄積）
  ↓ 発話終了検出（VAD）→ audio.input (is_speech_end=true) 送信
  ↓ WhisperAdapter: バッファをWhisper APIに送信
  ↓ API処理（ネットワーク往復 + 推論時間）
  → stt.final 発行
```

レイテンシ = 発話時間 + Core受信遅延 + API処理時間（目安: 500ms〜1s）

### ストリーミング型（Google STT等）

```
Unity Bridge: 発話開始検出 → audio.input (is_speech_start=true) 送信
  ↓ GoogleSTTAdapter: Streamingセッション開始
  ↓ チャンクをリアルタイム転送
  ↓ 発話終了 → is_speech_end → セッション終了
  → stt.final 発行
```

レイテンシ = API応答時間（発話終了とほぼ同時、目安: 200〜400ms）

---

## STTWorker 内部状態

```python
class STTWorkerState:
    audio_buffer: bytes                 # 音声チャンクの蓄積バッファ（一括型）
    partial_text: str                   # 現在の中間テキスト
    last_clause_text: str               # 最後に stt.clause 発行した時点のテキスト
    current_recognition: asyncio.Task   # 認識中のタスク（キャンセル用）
```

### 状態遷移

```
IDLE ──(is_speech_start)──► RECOGNIZING
RECOGNIZING ──(is_speech_end + 変換完了)──► IDLE（stt.final 発行）

任意の状態 ──(turn.interrupt)──► IDLE（進行中の認識タスクをキャンセル）
```

---

## 設定ファイル（stt.yaml）

VAD設定はUnity Bridge側に委ねるため、stt.yamlはSTTエンジン設定のみを記述する。

```yaml
stt:
  engine: "whisper"  # "whisper" | "google"

  whisper:
    model: "large-v3"
    language: "ja"
    api_url: "http://localhost:9000"

  google:
    language_code: "ja-JP"
    model: "latest_long"
```
