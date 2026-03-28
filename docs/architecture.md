# AI Avatar Framework - アーキテクチャ設計

## 概要

リアルタイム会話AIアバターのためのフレームワーク。
本フレームワークはライブラリとして機能を提供し、アバタープロジェクトから呼び出されて動作する。

## 設計原則

1. **コアとプラットフォームの分離** - 脳（AIロジック）と身体（描画/駆動）をWebSocketで接続
2. **メカニズムとデータの分離** - フレームワークは仕組みを提供、アバターが設定とデータを保持
3. **プラットフォーム非依存** - Unity / ROS2 等、アダプター層の差し替えでプラットフォーム対応
4. **エントリーポイントはアバター側** - フレームワーク自体は起動しない。アバターが起動し、フレームワークを呼び出す

## システム全体像

```mermaid
graph TB
  subgraph framework["ai-avatar-framework (本リポ)"]
    subgraph core["Python Core (uv addで提供)"]
      Engine
      EventBus
      ConversationManager
      PerceptionManager
      Workers
      RAGEngine
      WebSocketServer
    end
    subgraph adapter["Unity Adapter Base (UPMで提供)"]
      WebSocketClient
      MessageRouter
      IExpressionHandler
      IAnimationHandler
    end
  end

  subgraph avatar["avatar-foo (アバタープロジェクト)"]
    subgraph brain["brain/ — エントリーポイント"]
      main.py
      config["config/\npersonality.yaml\ntts.yaml\nstt.yaml\nmemory.yaml"]
      data["data/\nmemory_db/"]
    end
    subgraph unity["unity/ — 描画・入出力"]
      UnityProject["Unityプロジェクト\n3Dモデル\nシーン"]
      FooHandlers["FooExpressionHandler.cs\nFooAnimationHandler.cs"]
    end
  end

  core -- "uv add\n仕組みを提供" --> brain
  adapter -- "UPM参照\nインターフェースを提供" --> unity
  brain <--> |"WebSocket"| unity

  style framework fill:#e8f4f8,stroke:#2196F3
  style avatar fill:#fff3e0,stroke:#FF9800
```

## Python Core コンポーネント

### Engine

フレームワークの中心。設定を読み込み、各Workerを起動し、WebSocketサーバーを立ち上げる。

```python
# アバター側での利用イメージ
from ai_avatar import Engine

engine = Engine(config_dir="./config", data_dir="./data")
engine.run()
```

### EventBus

コンポーネント間の非同期イベント配信。asyncioベース。

主要イベント:
- `audio.input` - 発話音声チャンク受信（VAD済み、is_speech_start / is_speech_end フラグ付き）
- `stt.partial` / `stt.clause` / `stt.final` - 音声認識結果
- `llm.response_chunk` / `llm.response_done` - LLM応答（ストリーミング）
- `reaction.expression` / `reaction.animation` / `reaction.backchannel` - 表情・アニメーション・相槌指示（ReactionWorker）
- `tts.audio_chunk` - 音声合成結果
- `perception.update` / `perception.trigger` - 知覚情報
- `turn.interrupt` / `turn.cancel` / `tts.stop` - 割り込み制御
- `memory.context` - RAG検索結果
- `worker.status` - Worker稼働状態変化通知（READY / DEGRADED / DOWN）

### ConversationManager

会話の状態管理とイベント優先度判断を専任するコンポーネント。
会話状態マシン（IDLE / LISTENING / PROCESSING / SPEAKING / INTERRUPTED）を管理し、
割り込み制御やイベント優先度に基づく判断を行う。
詳細は `llm-conversation-design.md` を参照。

### PerceptionManager

全センサー（視覚・触覚・距離等）の最新状態を集約・保持するコンポーネント。
各センサーWorkerが `perception.update` で知覚を登録し、LLMWorkerがコンテキスト構築時に `get_snapshot()` で最新状態を取得する（push/pull非対称パターン）。
新しいセンサー追加時にLLMWorkerの修正が不要な拡張性を持つ。
詳細は `llm-conversation-design.md` を参照。

### Workers

各機能を担う非同期ワーカー。全Workerは同一プロセス内のasyncioタスクとして動作する。

| Worker | 種別 | 役割 |
|--------|------|------|
| STTWorker | Permanent | 音声→テキスト変換（STTアダプター経由、partial/clause/final発行） |
| LLMWorker | Permanent | LLM呼び出し（ストリーミング応答、テキスト生成のみ） |
| ReactionWorker | Permanent | 軽量ローカルモデルによる表情・アニメーション判定（常時テキストを受け取り推論） |
| TTSWorker | Permanent | テキスト→音声変換 |
| VisionWorker | Permanent | カメラ映像→環境認識（PerceptionManagerに登録） |
| MemoryWorker | Permanent | RAG検索・保存 |

### RAGEngine

記憶機能の仕組みを提供。

- Embedding生成
- ベクトル検索（ChromaDB等）
- チャンク分割
- 検索結果のLLMコンテキスト注入

データ実体（DB、ナレッジ文書）はアバター側が保持。

### WebSocketServer

Unity/ROS2アダプターとの通信層。JSONメッセージでやり取り。

## Unity Adapter Base

UPMパッケージとして提供。アバターのUnityプロジェクトから参照される。

### 提供するもの

- **WebSocketClient** - Python Coreとの接続管理
- **MessageRouter** - 受信メッセージの振り分け
- **インターフェース定義**
  - `IExpressionHandler` - 表情制御
  - `IAnimationHandler` - アニメーション制御
  - `IAudioHandler` - 音声入出力
  - `IVisionProvider` - カメラ映像送信

アバター側が各インターフェースを3Dモデルに合わせて実装する。

## リアルタイム応答フロー

イベント駆動 + パイプライン並列化方式を採用。固定間隔ポーリングではなく、
意味のある区切り（節区切り）をトリガーとする。詳細は `llm-conversation-design.md` を参照。

```
音声入力（Adapter側VADで発話区間のみ送信）
    │ audio.input (is_speech_start / is_speech_end)
    ▼
STTWorker（音声→テキスト変換）
    ├─ 発話中 + 節区切り検出 → STTが stt.clause 発行
    │                         → ReactionWorker（軽量モデルで相槌・表情判定）
    │
    └─ 発話終了(is_speech_end)→ stt.final発行
                               → MemoryWorkerがRAG検索開始（コンテキスト構築と並列実行）
                               → LLMに完全テキスト送信（RAG結果を待って注入）
                               → LLMがストリーミング応答開始
                               → TTS逐次変換 → 音声出力開始
                               → ReactionWorkerが応答チャンクから表情・アニメーション判定

割り込み（アバター発話中にユーザーが話し始めた場合）:
    → 300ms猶予後に中断確定
    → LLM/TTS停止 → LISTENING状態に遷移
```

## 提供形態

| コンポーネント | 提供方法 | 利用方法 |
|-------------|---------|---------|
| Python Core | uv add | `from ai_avatar import Engine` |
| Unity Adapter Base | UPM (git URL) | Packages/manifest.json で参照 |
| ROS2 Adapter | 将来対応 | - |

## アバタープロジェクト側の責務

| 項目 | 内容 |
|------|------|
| 人格定義 | personality.yaml にシステムプロンプト等を記述 |
| 声設定 | tts.yaml にTTSエンジン種別と設定を記述（詳細は `components.md` TTSWorker節） |
| 音声認識設定 | stt.yaml にSTTエンジン種別と設定を記述（詳細は `components.md` STTWorker節） |
| 記憶データ | data/ 配下にRAG DB実体を保持 |
| 3Dモデル | Unityプロジェクト内に配置 |
| 表情実装 | IExpressionHandler を3Dモデルに合わせて実装 |
| アニメーション実装 | IAnimationHandler を3Dモデルに合わせて実装 |
| カスタムスキル | skills/ にPythonスクリプトとして追加 |

## tts.yaml / stt.yaml とアダプター設計

TTS・STTエンジンの設定ファイル形式とアダプター設計の詳細は `components.md`（TTSWorker・STTWorkerの各節）を参照。
