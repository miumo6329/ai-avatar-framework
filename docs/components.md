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
| vad.speech_start | ListenerWorker | ConversationState | - |
| vad.speech_end | ListenerWorker | STTWorker, ConversationState | 音声バッファ |
| stt.partial | STTWorker | WebSocketServer | 中間テキスト |
| stt.final | STTWorker | LLMWorker, MemoryWorker | 確定テキスト |
| llm.response_chunk | LLMWorker | TTSWorker, WebSocketServer | テキストチャンク |
| llm.response_done | LLMWorker | MemoryWorker, WebSocketServer | 完全テキスト |
| llm.expression | LLMWorker | WebSocketServer | 感情パラメータ |
| llm.animation | LLMWorker | WebSocketServer | アニメーション指示 |
| tts.audio_chunk | TTSWorker | WebSocketServer | 音声チャンク |
| vision.observation | VisionWorker | LLMWorker | 環境認識テキスト |
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
待機中 ──(音声検出)──► 発話中 ──(無音検出)──► 発話終了
  ▲                                              │
  └──────────────────────────────────────────────┘
```

**パラメータ（config設定可能）:**
- `vad_threshold`: 発話判定の閾値
- `silence_duration_ms`: 発話終了と判定する無音時間
- `min_speech_duration_ms`: 最短発話時間（ノイズ除去）

---

### 5. STTWorker

音声データをテキストに変換。

**責務:**
- 音声バッファ受信→テキスト変換
- 中間結果の逐次出力（ストリーミングSTT対応時）
- 確定結果の出力

**使用ライブラリ候補:**
- OpenAI Whisper API（クラウド、高精度）
- faster-whisper（ローカル、高速）
- Google Cloud Speech-to-Text（ストリーミング対応）

---

### 6. LLMWorker

LLMへのリクエストとストリーミング応答の管理。リアルタイム会話の核心。

**責務:**
- コンテキスト構築（人格 + 記憶 + 視覚情報 + ユーザー発話）
- LLMへのストリーミングリクエスト
- 応答のパース（テキスト、感情、アニメーション指示の分離）
- 「応答すべきか」の判断制御

**コンテキスト構築:**
```
システムプロンプト（personality.yaml）
+ RAG検索結果（MemoryWorkerから）
+ 視覚認識結果（VisionWorkerから）
+ 直近の会話履歴
+ ユーザー発話テキスト
```

**応答フォーマット（LLMに要求する出力形式）:**
```json
{
  "action": "respond",
  "text": "今日はいい天気ですね。",
  "emotion": "smile",
  "emotion_intensity": 0.8,
  "animation": "nod"
}
```

actionの値:
- `respond` - 応答する
- `wait` - まだ聞いている（応答しない）
- `think` - 考え中（短い応答の後、詳しく考える）

**使用ライブラリ候補:**
- OpenAI API（GPT-4o等）
- Anthropic API（Claude等）
- ローカルLLM（Ollama経由）

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

---

### 9. MemoryWorker

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
