# コンポーネント詳細設計

## Python Core コンポーネント

### 1. Engine

フレームワークのオーケストレーター。アバタープロジェクトのエントリーポイントから呼び出される。

**責務:**
- 設定ファイル（yaml）の読み込み
- 各Workerの生成・起動・停止
- EventBusの初期化
- WebSocketサーバーの起動

**状態遷移:**
```
初期化 → 設定読み込み → Worker起動 → サーバー起動 → 稼働中 → 停止
```

**設定の注入:**
```python
engine = Engine(config_dir="./config", data_dir="./data")
# config_dir: personality.yaml, tts.yaml 等が置かれたディレクトリ
# data_dir: RAG DB等のランタイムデータディレクトリ
```

---

### 2. EventBus

コンポーネント間の非同期メッセージング。asyncioベース。

**責務:**
- イベントの発行（publish）
- イベントの購読（subscribe）
- ワイルドカード購読（例: `audio.*`）

**インターフェース:**
```python
class EventBus:
    async def publish(self, event_type: str, data: Any) -> None: ...
    def subscribe(self, event_type: str, handler: Callable) -> None: ...
    def unsubscribe(self, event_type: str, handler: Callable) -> None: ...
```

**イベント一覧:**

| イベント | 発行元 | 購読先 | データ |
|---------|--------|--------|--------|
| audio.input | WebSocketServer | ListenerWorker | 音声チャンクバイナリ |
| vad.speech_start | ListenerWorker | ConversationManager | - |
| vad.pause | ListenerWorker | STTWorker | 発話中の短い間（300-500ms）を検出 |
| vad.speech_end | ListenerWorker | STTWorker, ConversationManager | 音声バッファ |
| stt.partial | STTWorker | WebSocketServer | 中間テキスト |
| stt.clause | STTWorker | LLMWorker, MemoryWorker | 節区切りテキスト |
| stt.final | STTWorker | LLMWorker, MemoryWorker, ConversationManager | 確定テキスト |
| llm.response_chunk | LLMWorker | TTSWorker, WebSocketServer | テキストチャンク |
| llm.response_done | LLMWorker | MemoryWorker, WebSocketServer, ConversationManager | 完全テキスト |
| reaction.expression | ReactionWorker | WebSocketServer | 感情パラメータ |
| reaction.animation | ReactionWorker | WebSocketServer | アニメーション指示 |
| reaction.backchannel | ReactionWorker | TTSWorker | 相槌指示（ユーザー発話中のみ） |
| tts.audio_chunk | TTSWorker | WebSocketServer | 音声チャンク |
| tts.stop | ConversationManager | TTSWorker, WebSocketServer | 音声再生の即時停止 |
| perception.update | 各センサーWorker | PerceptionManager | PerceptionEntry |
| perception.trigger | PerceptionManager | ConversationManager | 閾値超えの知覚イベント |
| turn.interrupt | ConversationManager | LLMWorker, TTSWorker | 現在の応答を中断 |
| turn.cancel | LLMWorker | TTSWorker | LLM応答ストリームの停止 |
| memory.context | MemoryWorker | LLMWorker | RAG検索結果 |

---

### 3. BaseWorker

全Workerの基底クラス。

**責務:**
- ライフサイクル管理（start / stop）
- EventBusへの接続
- エラーハンドリング

```python
class BaseWorker:
    def __init__(self, event_bus: EventBus, config: dict): ...
    async def start(self) -> None: ...
    async def stop(self) -> None: ...
    async def on_event(self, event_type: str, data: Any) -> None: ...
```

---

### 4. ListenerWorker

常時音声を受信し、VAD（Voice Activity Detection）で発話区間を検出。

**責務:**
- WebSocket経由の音声チャンク受信
- VADによる発話開始/終了検出
- 音声バッファの管理

**使用ライブラリ候補:**
- Silero VAD（軽量、精度良好）
- WebRTC VAD

**状態遷移:**
```
待機中 ──(音声検出)──► 発話中 ──(短い間)──► pause検出 ──(再開)──► 発話中
  ▲                      │                                        │
  │                      └──(長い無音)──► 発話終了 ◄──(長い無音)────┘
  └────────────────────────────────────────┘
```

発行するイベント:
- `vad.speech_start` - 発話開始検出
- `vad.pause` - 発話中の短い間（300-500ms）検出。節区切り検出の補助
- `vad.speech_end` - 発話終了検出（長い無音）

**パラメータ（config設定可能）:**
- `vad_threshold`: 発話判定の閾値
- `silence_duration_ms`: 発話終了と判定する無音時間
- `pause_duration_ms`: pause判定の無音時間（300-500ms）
- `min_speech_duration_ms`: 最短発話時間（ノイズ除去）
- `interrupt_grace_ms`: 割り込み判定の猶予時間（デフォルト300ms）

---

### 5. STTWorker

音声データをテキストに変換。

**責務:**
- 音声バッファ受信→テキスト変換
- 中間結果の逐次出力（ストリーミングSTT対応時）
- 節区切り検出（`stt.clause` 発行）
- 確定結果の出力

**発行するイベント:**
- `stt.partial` - 中間テキスト（ストリーミングSTT対応時のみ）
- `stt.clause` - 節区切りテキスト（句読点検出 or vad.pause受信時）
- `stt.final` - 確定テキスト

**節区切り検出条件（STT/VAD併用、実装時にチューニング）:**
- STT中間結果に句読点（。、？！）が出現
- vad.pause を受信（STT中間結果が更新されない短い間）
- STT中間結果のテキスト長が前回送信時から一定量増加

**使用ライブラリ候補:**
- OpenAI Whisper API（クラウド、高精度）
- faster-whisper（ローカル、高速）
- Google Cloud Speech-to-Text（ストリーミング対応）

---

### 6. LLMWorker

LLMへのリクエストとストリーミング応答の管理。**テキスト生成のみ**を責務とする。
感情・アニメーションの判定はReactionWorkerに分離されている。

**責務:**
- コンテキスト構築（人格 + 記憶 + 知覚 + ユーザー発話）
- LLMへのストリーミングリクエスト
- テキスト応答のストリーミング送出
- 「応答すべきか」の判断制御（respond / wait / think）

**コンテキスト構築（本応答時）:**
```
① システムプロンプト（personality.yaml）     ← 必ず含める
② Adapter Capabilities（connection.hello）  ← 必ず含める
③ Perception Snapshot（PerceptionManager）  ← ttl内のもの
④ RAG検索結果（MemoryWorker、先行検索済み）   ← 関連度でフィルタ
⑤ 直近の会話履歴                             ← 圧縮対象
⑥ ユーザー発話テキスト（stt.final）           ← 必ず含める
```
トークン不足時は⑤→④→③の順に削減する。詳細は `llm-conversation-design.md` を参照。

**応答フォーマット（LLMに要求する出力形式）:**

LLMはプレーンテキストのみを返す。感情・アニメーション関連のフィールドは含まない。
```json
{
  "action": "respond",
  "text": "今日はいい天気ですね。"
}
```

actionの値:
- `respond` - 応答する（テキストのみ）
- `wait` - まだ聞いている（応答しない）
- `think` - 考え中（短い応答の後、詳しく考える）

**使用ライブラリ候補:**
- OpenAI API（GPT-4o等）
- Anthropic API（Claude等）
- ローカルLLM（Ollama経由）

---

### 6.5. ReactionWorker

ローカル軽量モデル（0.5B-1Bクラス）による表情・アニメーション専任Worker。
ユーザー・アバター問わず流れてくるテキストを常時受け取り、リアルタイムに表情・アニメーションを決定する。

**責務:**
- ユーザー発話（stt.partial / stt.clause / stt.final）への表情・アニメーション判定
- アバター応答（llm.response_chunk）への表情・アニメーション判定
- 相槌の判定（ユーザー発話中のみ）

**購読するイベント:**
- `stt.partial` - ユーザー発話の中間テキスト
- `stt.clause` - ユーザー発話の節区切りテキスト
- `stt.final` - ユーザー発話の確定テキスト
- `llm.response_chunk` - アバター応答のテキストチャンク

**発行するイベント:**
- `reaction.expression` - 表情指示（WebSocketServerへ）
- `reaction.animation` - アニメーション指示（WebSocketServerへ）
- `reaction.backchannel` - 相槌指示（TTSWorkerへ、ユーザー発話中のみ）

**入力データ構造:**
```python
@dataclass
class ReactionInput:
    text: str                  # テキスト断片
    speaker: "user" | "avatar" # 誰の発話か
```

**出力データ構造:**
```python
@dataclass
class ReactionResult:
    emotion: str              # "empathy", "joy", "neutral", ...
    animation: str | None     # "nod", "tilt_head", "lean_forward", None
    backchannel: str | None   # "うん", "へぇ", None（userの発話中のみ）
```

**軽量モデルの選定方針:**

推奨はローカル軽量LLM（Qwen2.5 0.5B, TinyLlama 1.1B等）。
プロンプトで感情/アニメーションのラベル体系を変更できるため、avatar projectごとの表情・アニメーションバリエーションの違いに対応可能。GPU使用時のレイテンシは15-40msで、TTS合成（数百ms）より十分高速。

詳細は `llm-conversation-design.md` を参照。

---

### 7. TTSWorker

テキストを音声に変換。

**責務:**
- テキストチャンク受信→音声変換
- ストリーミング出力（文単位でのチャンク送信）
- 音声フォーマット変換

**使用ライブラリ候補:**
- VOICEVOX（日本語特化、ローカル）
- Style-BERT-VITS2（高品質、ローカル）
- OpenAI TTS API（クラウド）

---

### 8. VisionWorker

カメラ映像から環境を認識。

**責務:**
- 映像フレーム受信
- 軽量VLMによる画像認識
- 認識結果のテキスト化→LLMコンテキストへ注入

**使用ライブラリ候補:**
- moondream（軽量VLM）
- LLaVA（ローカル）
- OpenAI GPT-4o Vision（クラウド）

**動作モード:**
- 定期認識: 一定間隔で映像を分析
- イベント駆動: 大きな変化を検出した時のみ分析

認識結果は `perception.update` イベントでPerceptionManagerに登録する。

---

### 9. ConversationManager

会話の状態管理とイベント優先度判断を専任するコンポーネント。

**責務:**
- 会話状態マシンの管理（IDLE / LISTENING / PROCESSING / SPEAKING / INTERRUPTED）
- イベント優先度に基づく処理判断
- 割り込み制御（猶予付き中断: 300ms）
- 知覚トリガーによる能動発話の許可/却下判断

**購読するイベント:**
- `vad.speech_start` - ユーザー発話開始（割り込み判定を含む）
- `vad.speech_end` - ユーザー発話終了
- `stt.final` - 確定テキスト（状態遷移用）
- `llm.response_done` - LLM応答完了（状態遷移用）
- `tts.audio_chunk` (is_final=true) - TTS再生完了（状態遷移用）
- `perception.trigger` - 知覚トリガー（能動発話判定）

**発行するイベント:**
- `turn.interrupt` - 現在の応答を中断
- `tts.stop` - 音声再生の即時停止

詳細は `llm-conversation-design.md` を参照。

---

### 10. PerceptionManager

全センサーの最新状態を集約・保持するコンポーネント。

**責務:**
- 各センサーWorkerからの知覚情報を受信・保持
- TTL（有効期限）による古い知覚の自動無効化
- LLMWorkerへの最新知覚スナップショット提供
- 閾値超え検出による `perception.trigger` 発行

**インターフェース:**
```python
class PerceptionManager:
    def update(self, source: str, observation: PerceptionEntry) -> None: ...
    def get_snapshot(self) -> list[PerceptionEntry]: ...
    def get_snapshot_by_source(self, source: str) -> PerceptionEntry | None: ...
```

**データ構造:**
```python
@dataclass
class PerceptionEntry:
    source: str           # "vision", "tactile", ...
    text: str             # LLMコンテキストに注入するテキスト表現
    timestamp: float      # 観測時刻
    priority: int         # コンテキストのトークン配分時の優先度
    ttl: float            # 有効期限（秒）
```

新しいセンサーの追加手順: 新しいWorkerを作り、`perception.update` イベントを発行するだけ。LLMWorkerの修正は不要。

詳細は `llm-conversation-design.md` を参照。

---

### 11. MemoryWorker

RAGによる記憶機能。

**責務:**
- 会話内容のEmbedding生成・保存
- ユーザー発話に関連する記憶の検索
- 検索結果のLLMコンテキスト注入

**使用ライブラリ候補:**
- ChromaDB（ベクトルDB、ローカル）
- OpenAI Embeddings API

**データフロー:**
```
会話完了 → テキスト → Embedding → ベクトルDB保存（data/memory_db/）
新しい発話 → 検索クエリ → ベクトルDB検索 → 関連記憶 → LLMコンテキスト
```

---

## Unity Adapter Base コンポーネント

### WebSocketClient

Python CoreへのWebSocket接続を管理。

**責務:**
- 接続・再接続管理
- メッセージ送受信
- 音声データのBase64エンコード/デコード

### MessageRouter

受信メッセージを適切なHandlerに振り分け。

**責務:**
- メッセージtypeに基づくルーティング
- Handlerの登録・解除

### インターフェース定義

アバター側が実装する抽象インターフェース。

```csharp
public interface IExpressionHandler
{
    void SetExpression(string emotion, float intensity, float transitionMs);
    string[] GetSupportedExpressions();
}

public interface IAnimationHandler
{
    void PlayAnimation(string name, float speed, bool loop);
    string[] GetSupportedAnimations();
}

public interface IAudioHandler
{
    void PlayAudioChunk(byte[] audioData, int sampleRate, bool isFinal);
    void StartCapture(int sampleRate);  // マイク入力開始
    void StopCapture();
    event Action<byte[]> OnAudioCaptured;  // 音声チャンクコールバック
}

public interface IVisionProvider
{
    void StartCapture(int fps);
    void StopCapture();
    event Action<byte[]> OnFrameCaptured;  // フレームコールバック
}
```
