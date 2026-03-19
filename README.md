# AI Avatar Framework

リアルタイム会話AIアバターのためのフレームワーク。

## 概要

本フレームワークはライブラリとして機能を提供し、アバタープロジェクトから呼び出されて動作します。フレームワーク自体にエントリーポイントはありません。

## 構成

| ディレクトリ | 内容 | 提供方法 |
|-------------|------|---------|
| `core/` | Python AIコア（Engine, Workers, RAGEngine等） | pip install |
| `adapters/unity-package/` | Unity Adapter Base（WebSocketClient, インターフェース定義） | UPM |
| `proto/` | 共通メッセージプロトコル定義 | 参照 |
| `docs/` | 設計ドキュメント | - |

## 設計ドキュメント

- [アーキテクチャ設計](docs/architecture.md) - 全体設計、設計原則
- [プロジェクト構成](docs/project-structure.md) - ディレクトリ構造、UPM参照方法
- [プロトコル仕様](docs/protocol.md) - WebSocketメッセージ定義
- [コンポーネント詳細](docs/components.md) - 各Worker・Adapterの責務

## アバタープロジェクトとの関係

```
ai-avatar-framework (本リポ)     avatar-{name} (別リポ)
  仕組みを提供 ──────────────►   設定 + データ + 見た目
  Python Core → pip install       brain/ (Python設定+データ)
  Unity Adapter → UPM参照         unity/ (Unityプロジェクト)
```
