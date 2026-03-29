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
| audio.input | WebSocketServer | STTWorker, ConversationManager | 発話音声チャンク（VAD済み、is_speech_start / is_speech_end フラグ付き） |
| stt.partial | STTWorker | WebSocketServer | 中間テキスト |
| stt.clause | STTWorker | ReactionWorker | 節区切りテキスト |
| stt.final | STTWorker | LLMWorker, MemoryWorker, ConversationManager | 確定テキスト |
| llm.response_chunk | LLMWorker | TTSWorker, ReactionWorker, WebSocketServer | テキストチャンク |
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
| worker.status | 各Worker (BaseWorker) | Engine, WebSocketServer | WorkerStatus（稼働状態変化通知） |
| connection.disconnected | WebSocketServer | MemoryWorker | WebSocket切断検知（セッション終了トリガー） |

---

### 3. BaseWorker

全Workerの基底クラス。

**責務:**
- ライフサイクル管理（start / stop）
- EventBusへの接続
- エラーハンドリング
- 稼働状態管理（READY / DEGRADED / DOWN）と `worker.status` イベント発行

```python
class BaseWorker:
    def __init__(self, event_bus: EventBus, config: dict): ...
    async def start(self) -> None: ...
    async def stop(self) -> None: ...
    async def on_event(self, event_type: str, data: Any) -> None: ...
    async def reset(self) -> None: ...  # 内部状態クリア、READY復帰
```

稼働状態とタイムアウト・リトライの詳細は `timeout-resilience-design.md` を参照。

---

### 4. STTWorker

音声データをテキストに変換。処理パイプライン・アダプター設計・節区切り検出・内部状態の詳細は `stt-processing-design.md` を参照。

VAD（発話区間検出）はAdapter側の責務であり、STTWorkerにはVAD済みの発話音声のみが届く。

**責務:**
- 発話音声チャンク受信→テキスト変換（STTAdapter経由）
- 中間結果の逐次出力（ストリーミングSTT対応時）
- 節区切り検出（`stt.clause` 発行）
- 確定結果の出力

**発行するイベント:**
- `stt.partial` - 中間テキスト（ストリーミングSTT対応時のみ）
- `stt.clause` - 節区切りテキスト（句読点検出 or vad.pause受信時）
- `stt.final` - 確定テキスト

**エンジン種別:**
- 一括変換型（Whisper等）: `stt.partial` / `stt.clause` 非対応。`stt.final` のみ発行
- ストリーミング型（Google STT / sherpa-onnx / SenseVoice等）: `stt.partial` / `stt.clause` / `stt.final` をリアルタイム発行

エンジンごとの発行イベントの違いはSTTAdapter実装で吸収する。`stt.yaml` には記載しない。

エンジン選択は `stt.yaml` の `stt.engine` 値で決定される。

---

### 5. LLMWorker

LLMへのリクエストとストリーミング応答の管理。**テキスト生成のみ**を責務とする。
感情・アニメーションの判定はReactionWorkerに分離されている。

**責務:**
- コンテキスト構築（人格 + 記憶 + 知覚 + ユーザー発話）
- LLMへのストリーミングリクエスト
- テキスト応答のストリーミング送出
- 「応答すべきか」の判断制御（respond / wait）

コンテキスト構成要素（システムプロンプト・Adapter Capabilities・Perception Snapshot・RAG検索結果・会話履歴・現在の発話）とトークン配分優先度の詳細は `llm-conversation-design.md` を参照。

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

**使用ライブラリ候補:**
- OpenAI API（GPT-4o等）
- Anthropic API（Claude等）
- ローカルLLM（Ollama経由）

---

### 5.5. ReactionWorker

ローカル軽量モデル（0.5B-1Bクラス）による表情・アニメーション専任Worker。
ユーザー・アバター問わず流れてくるテキストを常時受け取り、リアルタイムに表情・アニメーションを決定する。

**責務:**
- ユーザー発話（stt.partial / stt.clause / stt.final）への表情・アニメーション判定
- アバター応答（llm.response_chunk）への表情・アニメーション判定
- 相槌の判定（ユーザー発話中のみ）

**購読するイベント:**
- `stt.partial` / `stt.clause` / `stt.final` - ユーザー発話テキスト
- `llm.response_chunk` - アバター応答のテキストチャンク

**発行するイベント:**
- `reaction.expression` - 表情指示（WebSocketServerへ）
- `reaction.animation` - アニメーション指示（WebSocketServerへ）
- `reaction.backchannel` - 相槌指示（TTSWorkerへ、ユーザー発話中のみ）

データ構造・軽量モデル選定方針・処理フローの詳細は `llm-conversation-design.md` を参照。

---

### 6. TTSWorker

テキストを音声に変換。詳細なバッファリング設計は `tts-buffering-design.md` を参照。

**責務:**
- LLMストリーミングテキストの蓄積と文単位への分割（句読点 + 文字数上限）
- TTSAdapter経由での音声合成
- 1つ先読み（prefetch-1）による合成キュー管理
- 割り込み時のバッファクリア

**合成方式:**
- VOICEVOX / AivisSpeech の一括合成を前提（VOICEVOX互換API）
- 1つ先読み: 再生中に次の1文を合成開始し、文間ギャップを最小化
- キュー上限なし（対話応答は短文のため不要）
- アンダーラン時は特別な処理なし（音声準備でき次第再生）

**リップシンク・表情:**
- リップシンクはUnity側の音声駆動機能に委譲
- 表情との厳密な同期は現時点では実装しない

**使用ライブラリ候補:**
- VOICEVOX（日本語特化、ローカル）
- AivisSpeech（VOICEVOX互換API）

**tts.yaml と TTSアダプター設計:**

`tts.yaml` はTTSエンジンの種別と固有設定を記述する。

```yaml
tts:
  engine: "voicevox"          # TTSエンジン種別

  voicevox:
    host: "localhost"
    port: 50021
    speaker_id: 3
    speed_scale: 1.0
    pitch_scale: 0.0

  google:
    language_code: "ja-JP"
    voice_name: "ja-JP-Neural2-B"
    speaking_rate: 1.0
```

TTSエンジンごとに能力（感情パラメータ、音素情報の有無等）が大きく異なるため、
共通インターフェースは最小限（テキスト→音声バイナリ）とし、エンジン固有の設定はYAML側に閉じ込める。

```
TTSWorker
  └── TTSAdapter (共通インターフェース: text → audio bytes)
        ├── VoicevoxAdapter
        ├── GoogleTTSAdapter
        └── ...（将来追加）
```

エンジン選択は `tts.yaml` の `tts.engine` 値で決定される。

---

### 7. VisionWorker

カメラ映像から環境を認識。

**責務:**
- 映像フレーム受信
- 軽量VLMによる画像認識
- 認識結果のテキスト化→LLMコンテキストへ注入

**使用ライブラリ候補:**
- moondream（軽量VLM）
- SmolVLM（軽量VLM）
- LLaVA（ローカル）
- OpenAI GPT-4o Vision（クラウド）

**動作モード:**
- 定期認識: 一定間隔で映像を分析
- イベント駆動: 大きな変化を検出した時のみ分析

認識結果は `perception.update` イベントでPerceptionManagerに登録する。

---

### 8. ConversationManager

会話の状態管理とイベント優先度判断を専任するコンポーネント。
LLMWorkerに全責務を押し込むと肥大化するため、状態管理・優先度判断・割り込み制御を分離している。

**責務:**
- 会話状態マシンの管理（IDLE / LISTENING / PROCESSING / SPEAKING / INTERRUPTED）
- イベント優先度に基づく処理判断
- 割り込み制御（猶予付き中断: 300ms）
- 知覚トリガーによる能動発話の許可/却下判断

状態遷移図・イベント優先度体系・割り込みフローの詳細は `llm-conversation-design.md` を参照。

---

### 9. PerceptionManager

全センサーの最新状態を集約・保持するコンポーネント。
各センサーWorkerが `perception.update` でpush、LLMWorkerがコンテキスト構築時に `get_snapshot()` でpullする非対称パターン。

**責務:**
- 各センサーWorkerからの知覚情報を受信・保持
- TTL（有効期限）による古い知覚の自動無効化
- LLMWorkerへの最新知覚スナップショット提供
- 閾値超え検出による `perception.trigger` 発行

新しいセンサーの追加手順: 新しいWorkerを作り、`perception.update` イベントを発行するだけ。LLMWorkerの修正は不要。

インターフェース定義・`PerceptionEntry` データ構造・センサーごとのTTL目安・能動発話制御の詳細は `llm-conversation-design.md` を参照。

---

### 10. MemoryWorker

会話の記憶管理を担うWorker。会話バッファの管理、要約生成、RAG検索の制御を行う。
RAGEngine（§12）を内部的に利用してベクトルDBとのやり取りを行う。

**責務:**
- 会話バッファの蓄積（ターン単位で蓄積し、トピック単位で要約・保存）
- 要約タイミングの判断（トピック変化検出 / バッファ上限 / セッション終了）
- 要約LLMの呼び出し（軽量モデル）
- RAG検索の制御（`stt.final` 時に検索を実行し、結果をLLMWorkerへ送信）
- RAGEngineへの保存・検索指示

**購読するイベント:**
- `stt.final` - ユーザー発話確定テキスト（RAG検索トリガー + 会話バッファへの蓄積）
- `llm.response_done` - アバター応答完了（会話バッファへの蓄積 + 要約判定）
- `connection.disconnected` - WebSocket切断（残バッファを要約・保存してセッション終了）

**発行するイベント:**
- `memory.context` - RAG検索結果をLLMWorkerへ送信

**会話バッファと要約フロー:**
```
ターン蓄積:
  Turn 1: User「...」 Avatar「...」 → バッファに追加
  Turn 2: User「...」 Avatar「...」 → バッファに追加
  ...

要約トリガー（OR条件）:
  1. トピック変化検出 → 即座に要約・保存
  2. バッファがmax_buffer_turns（設定値）に到達 → 強制的に要約・保存
  3. セッション終了 → 残りバッファを要約・保存
```

要約済みのターンはコンテキストの会話履歴から削除し、トークンを節約する。
RAG検索でヒットすれば過去の要約がコンテキストに注入される。

**要約LLM（軽量モデル）の用途:**

MemoryWorker専用の軽量LLM（0.5B-1Bクラス）を使用する。以下の用途で使い回す。

| 用途 | 入力 | 出力 |
|------|------|------|
| 会話要約 | 会話バッファ（複数ターン） | 要約テキスト |
| トピック変化検出 | 前回の要約 + 現在のバッファ | yes / no |
| 検索クエリ構築 | ユーザー発話テキスト | 構造化クエリ（検索テキスト + メタデータフィルタ） |

検索クエリ構築の例:
```
入力: 「去年の12月15日に車の話したの覚えてる？」
出力: { query: "車の話題", date_filter: "2025-12-15" }
```

日付や固有名詞による検索は、ベクトル類似度だけでは精度が出ないため、
LLMで構造化クエリを生成し、メタデータフィルタとベクトル検索を併用する。

**RAG検索のタイミング:**

`stt.final` 受信時に1回のみ検索を実行する。先行検索は行わない。

```
stt.final
  ├── MemoryWorker: Embedding化 + ベクトルDB検索（~60ms）──┐
  ├── LLMWorker: コンテキスト構築開始                      │
  │    personality, perception, 履歴を組み立て             │
  │    ...RAG結果を待つ...                               ◄┘
  └── LLMWorker: RAG結果を注入 → LLM APIリクエスト送信
```

検索レイテンシ（Embedding化 + ベクトル検索で30-60ms程度）はコンテキスト構築と並列に走るため、
LLM応答生成（TTFT: 200-500ms）に比べてボトルネックにならない。

**保存レコードの構造:**
```python
@dataclass
class MemoryRecord:
    text: str              # 要約テキスト
    embedding: list[float] # ベクトル
    timestamp: datetime    # 会話時刻
    metadata: dict         # session_id, topic, date等
```

**設定パラメータ（memory.yaml）:**
```yaml
buffer:
  max_buffer_turns: 5        # 要約を強制実行するターン数上限

rag:
  similarity_threshold: 0.7  # 検索結果の関連度フィルタ閾値
  max_results: 5             # 検索結果の最大件数

summary_llm:
  model: "qwen2.5:0.5b"     # 要約・クエリ構築に使用する軽量モデル
```

---

### 11. RAGEngine

ベクトルDBとのやり取りを担うインフラ層コンポーネント。EventBusには接続せず、MemoryWorkerから直接呼び出される。

**責務:**
- Embeddingモデルの呼び出し（テキスト → ベクトル変換）
- ベクトルDBへの保存
- ベクトル類似度検索 + メタデータフィルタリング

**インターフェース:**
```python
class RAGEngine:
    def __init__(self, db_path: str, embedding_model: str): ...

    async def store(self, text: str, metadata: dict) -> None:
        """テキストをEmbedding化してベクトルDBに保存"""

    async def search(self, query: str, n_results: int = 5,
                     metadata_filter: dict | None = None) -> list[SearchResult]:
        """ベクトル類似度検索。メタデータフィルタとの併用が可能"""
```

```python
@dataclass
class SearchResult:
    text: str           # 保存されたテキスト（要約）
    similarity: float   # 類似度スコア（0.0-1.0）
    metadata: dict      # 保存時のメタデータ
```

**使用ライブラリ候補:**
- ChromaDB（ベクトルDB、ローカル、メタデータフィルタ対応）
- OpenAI Embeddings API / ローカルEmbeddingモデル

**データ保存先:** アバタープロジェクトの `brain/data/memory_db/`

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
    void StartCapture(int sampleRate);   // マイク入力 + VAD開始
    void StopCapture();
    // VADで発話区間と判定した音声チャンクのコールバック
    // isSpeechStart: 発話区間の先頭チャンク
    // isSpeechEnd:   発話区間の末尾チャンク
    event Action<byte[], bool, bool> OnSpeechChunkCaptured;
}

public interface IVisionProvider
{
    void StartCapture(int fps);
    void StopCapture();
    event Action<byte[]> OnFrameCaptured;  // フレームコールバック
}
```
