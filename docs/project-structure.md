# プロジェクト構成

## フレームワークリポジトリ (ai-avatar-framework)

```
ai-avatar-framework/
├── docs/                              # 設計ドキュメント
│   ├── architecture.md                #   アーキテクチャ全体設計
│   ├── project-structure.md           #   本ドキュメント
│   ├── protocol.md                    #   WebSocketメッセージプロトコル仕様
│   ├── components.md                  #   コンポーネント詳細設計
│   ├── llm-conversation-design.md     #   会話制御設計（ターンテイキング・割り込み・コンテキスト構築）
│   ├── tts-buffering-design.md        #   TTS音声バッファリング設計
│   └── timeout-resilience-design.md   #   タイムアウト・リトライ・障害復旧設計
│
├── core/                              # Python AIコア（pipパッケージ）
│   ├── pyproject.toml                 #   パッケージ定義
│   ├── src/
│   │   └── ai_avatar/
│   │       ├── __init__.py
│   │       ├── engine.py              #   Engine - メインオーケストレーター
│   │       ├── event_bus.py           #   EventBus - 非同期イベント配信
│   │       ├── config.py              #   設定読み込み
│   │       ├── workers/
│   │       │   ├── __init__.py
│   │       │   ├── base.py            #   BaseWorker - ワーカー基底クラス
│   │       │   ├── listener.py        #   ListenerWorker - 音声入力+VAD
│   │       │   ├── stt.py             #   STTWorker - 音声認識
│   │       │   ├── llm.py             #   LLMWorker - LLM呼び出し
│   │       │   ├── reaction.py        #   ReactionWorker - 表情・アニメーション判定
│   │       │   ├── tts.py             #   TTSWorker - 音声合成
│   │       │   ├── vision.py          #   VisionWorker - 視覚認識
│   │       │   └── memory.py          #   MemoryWorker - RAG管理
│   │       ├── conversation_manager.py #   ConversationManager - 会話状態管理
│   │       ├── perception_manager.py   #   PerceptionManager - センサー情報集約
│   │       ├── memory/
│   │       │   ├── __init__.py
│   │       │   └── rag_engine.py      #   RAGEngine - 記憶機能
│   │       └── server/
│   │           ├── __init__.py
│   │           └── websocket.py       #   WebSocketサーバー
│   └── tests/
│       └── ...
│
├── bridges/
│   └── unity-package/                 # Unity Bridge Base（UPMパッケージ）
│       ├── package.json               #   UPMパッケージ定義
│       ├── Runtime/
│       │   ├── AiAvatarBridge.asmdef
│       │   ├── Connection/
│       │   │   ├── WebSocketClient.cs
│       │   │   └── MessageRouter.cs
│       │   ├── Interfaces/
│       │   │   ├── IExpressionHandler.cs
│       │   │   ├── IAnimationHandler.cs
│       │   │   ├── IAudioHandler.cs
│       │   │   └── IVisionProvider.cs
│       │   └── Messages/
│       │       └── MessageTypes.cs    #   メッセージ型定義
│       └── README.md
│
├── proto/                             # 共通プロトコル定義
│   └── messages.json                  #   メッセージスキーマ（JSON Schema）
│
├── .gitignore
└── README.md
```

## アバタープロジェクト（例: avatar-foo）

```
avatar-foo/
├── brain/                             # Pythonコア設定 + データ
│   ├── main.py                        #   エントリーポイント
│   ├── pyproject.toml                 #   依存定義（ai-avatar-framework含む、uv管理）
│   ├── config/
│   │   ├── personality.yaml           #   人格・システムプロンプト
│   │   ├── tts.yaml                   #   TTS設定（エンジン種別、声質、速度等）
│   │   ├── stt.yaml                   #   STT設定（エンジン種別、モデル等）
│   │   ├── memory.yaml                #   RAG設定
│   │   ├── vision.yaml                #   視覚認識設定
│   │   ├── llm.yaml                   #   LLMモデル・パラメータ設定
│   │   ├── health.yaml                #   Worker稼働状態閾値設定（任意、デフォルト値あり）
│   │   └── timeouts.yaml              #   タイムアウト設定（任意、デフォルト値あり）
│   ├── skills/                        #   カスタムスキル
│   │   └── ...
│   └── data/                          #   ランタイムデータ（gitignore推奨）
│       └── memory_db/                 #   RAG DB実体
│
├── unity/                             # Unityプロジェクト
│   ├── Assets/
│   │   ├── Models/                    #   3Dモデル
│   │   ├── Animations/                #   アニメーションクリップ
│   │   ├── Materials/                 #   マテリアル
│   │   ├── Scenes/
│   │   │   └── Main.unity
│   │   └── Scripts/
│   │       ├── Handlers/
│   │       │   ├── FooExpressionHandler.cs   # IExpressionHandler実装
│   │       │   ├── FooAnimationHandler.cs    # IAnimationHandler実装
│   │       │   └── FooAudioHandler.cs        # IAudioHandler実装
│   │       └── FooUnityBridge.cs             # 各Handlerの組み立て
│   ├── Packages/
│   │   └── manifest.json             #   ← ai-avatar-framework UPM参照
│   └── ProjectSettings/
│
├── .gitignore
└── README.md
```

## アバタープロジェクトのディレクトリ分離理由

アバターリポを `brain/` と `unity/` に分離する理由：

- **Unityプロジェクト内にPythonファイルやRAG DBを混在させない**
  - Unityは `Assets/` 配下のファイルをインポートしようとする。Pythonファイルや `.sqlite3` 等は不要な警告を出す
  - Unityプロジェクトの `.gitignore` とPythonプロジェクトの `.gitignore` が競合する
- **起動プロセスが独立** - brain(Python)とunity(Unity Editor)は別プロセスで起動し、WebSocketで接続するだけ
- **brain/ だけ差し替えればROS2に移行可能** - unity/ を ros2/ に置き換える設計

## RAGの分離方針

RAGは「仕組み」と「データ」を分離する：

| 所在 | 内容 | 理由 |
|------|------|------|
| フレームワーク (`core/`) | RAGEngine（Embedding生成、ベクトル検索、メタデータフィルタ）、MemoryWorker（会話要約・検索制御） | 仕組みは全アバター共通 |
| アバター (`brain/data/`) | ベクトルDB実体、ナレッジ文書 | 記憶内容はアバター固有 |
| アバター (`brain/config/`) | memory.yaml（DB設定、Embeddingモデル指定） | 設定もアバター固有 |

同じRAGEngineで、アバターAはAの記憶、アバターBはBの記憶を持てる。

## UPMパッケージの参照方法

アバターのUnityプロジェクトからフレームワークのUnity Bridge Baseを参照する方法。
開発フェーズに応じて3段階で運用する：

### Phase 1: 開発初期（ローカルパス参照）

フレームワークのファイル編集が即座にUnity側に反映される。最も開発効率が高い。

```json
// avatar-foo/unity/Packages/manifest.json
{
  "dependencies": {
    "com.yourname.ai-avatar-bridge": "file:../../../ai-avatar-framework/bridges/unity-package"
  }
}
```

### Phase 2: 開発中期（ブランチ参照）

フレームワークがある程度安定した段階。git pushすれば反映。

```json
{
  "dependencies": {
    "com.yourname.ai-avatar-bridge": "https://github.com/yourname/ai-avatar-framework.git?path=adapters/unity-package#main"
  }
}
```

### Phase 3: 安定運用（タグ参照）

バージョン管理が明確。アバター側でタグ番号を書き換えて更新。

```json
{
  "dependencies": {
    "com.yourname.ai-avatar-bridge": "https://github.com/yourname/ai-avatar-framework.git?path=adapters/unity-package#v1.2.0"
  }
}
```

## 起動方法

```bash
# 1. 脳を起動
cd avatar-foo/brain
uv sync                             # 初回 or 依存更新時
uv run python main.py               # WebSocketサーバー起動

# 2. Unityを起動
# Unity Hubで avatar-foo/unity/ を開く → Play
```
