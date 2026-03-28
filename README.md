# AI Avatar Framework

リアルタイム会話AIアバターのためのフレームワーク。

## 概要

本フレームワークはライブラリとして機能を提供し、アバタープロジェクトから呼び出されて動作します。フレームワーク自体にエントリーポイントはありません。

## 構成

| ディレクトリ | 内容 | 提供方法 |
|-------------|------|---------|
| `core/` | Python AIコア（Engine, Workers, RAGEngine等） | uv add |
| `adapters/unity-package/` | Unity Adapter Base（WebSocketClient, インターフェース定義） | UPM |
| `proto/` | 共通メッセージプロトコル定義 | 参照 |
| `docs/` | 設計ドキュメント | - |

## 設計ドキュメント

- [アーキテクチャ設計](docs/architecture.md) - 全体設計、設計原則
- [プロジェクト構成](docs/project-structure.md) - ディレクトリ構造、UPM参照方法
- [プロトコル仕様](docs/protocol.md) - WebSocketメッセージ定義
- [コンポーネント詳細](docs/components.md) - 各Worker・Adapterの責務
- [会話制御設計](docs/llm-conversation-design.md) - ターンテイキング・コンテキスト構築・割り込み処理
- [STT音声認識処理設計](docs/stt-processing-design.md) - VAD・認識パイプライン・節区切り検出・アダプター設計
- [TTS音声バッファリング設計](docs/tts-buffering-design.md) - テキスト分割・合成キュー・先読み方式
- [タイムアウト・障害復旧設計](docs/timeout-resilience-design.md) - タイムアウト仕様・リトライ・Worker稼働状態管理

## アバタープロジェクトとの関係

```
ai-avatar-framework (本リポ)     avatar-{name} (別リポ)
  仕組みを提供 ──────────────►   設定 + データ + 見た目
  Python Core → pip install       brain/ (Python設定+データ)
  Unity Adapter → UPM参照         unity/ (Unityプロジェクト)
```
