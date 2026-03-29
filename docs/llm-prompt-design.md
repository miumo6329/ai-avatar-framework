# LLMWorker プロンプト設計

## 概要

本ドキュメントはLLMWorkerのプロンプト設計を記述する。
モデル依存のチューニング（[llm-conversation-design.md](llm-conversation-design.md) の「別途定義するもの」）に該当する。

**対象モデル:** Claude Sonnet（Anthropic API）
**出力形式:** ツール呼び出し強制（Structured Output）

> **[注意] 本ドキュメントは Claude Sonnet を前提として記述している。**
> 他の LLM に切り替える場合、以下のセクションが変更対象になる:
> - [出力形式: ツール呼び出し強制](#出力形式-ツール呼び出し強制) — 構造化出力の強制方法がモデルによって異なる
> - [APIリクエスト設定](#apiリクエスト設定) — モデル ID・パラメータ名がモデルによって異なる
> - [ストリーミング時のチャンク転送](#ストリーミング時のチャンク転送) — ストリーミングのイベント形式がモデルによって異なる
>
> システムプロンプト・ユーザーターンのメッセージ構造・コンテキスト構造はモデル非依存。

---

## 出力形式: ツール呼び出し強制

> **[モデル依存]** ここで示す方法は Claude API 固有。他モデルへの移行時の対応例:
> - OpenAI / Azure OpenAI: `response_format: {"type": "json_schema", "json_schema": {...}}` または `tool_choice: {"type": "function", ...}`
> - Ollama (ローカル): `format: <JSONスキーマ>` パラメータ（v0.4+）
> - スキーマ自体（`action` / `text` フィールド）はモデル非依存のため変更不要。

Claude APIの `tool_choice: {"type": "tool", "name": "reply"}` を使い、
毎回必ず以下のスキーマに従った出力を得る。

```python
REPLY_TOOL = {
    "name": "reply",
    "description": "ユーザーへの応答を返す。発話が途中の場合はwaitを返す。",
    "input_schema": {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["respond", "wait"],
                "description": (
                    "respond: ユーザーの発話が意味的に完結している。応答テキストを返す。\n"
                    "wait: 発話が途中（「えーと」などの間投詞、文末が不完全）。textは空文字で返す。"
                )
            },
            "text": {
                "type": "string",
                "description": "action=respond の場合の応答テキスト。action=wait の場合は空文字。"
            }
        },
        "required": ["action", "text"]
    }
}

tool_choice = {"type": "tool", "name": "reply"}
```

ツール呼び出し強制を使うことでスキーマ外の出力が発生しない。
不正JSON対応は原則不要だが、APIエラー時のリトライは[timeout-resilience-design.md](timeout-resilience-design.md)に従う。

---

## システムプロンプト

personality.yaml の内容をもとに構築する。固定部分とYAML注入部分を分けて管理する。

### テンプレート構造

```
{character_block}

{response_rules_block}
```

### character_block（personality.yaml から注入）

```
あなたは{name}です。
{description}

話し方:
{speaking_style}

制約:
{constraints}
```

**personality.yaml の対応フィールド例:**

```yaml
name: "タロウ"
description: |
  明るく親しみやすい性格のアシスタント。
  ユーザーの感情に共感しながら自然な会話を楽しむ。
speaking_style: |
  - 友達のような口調（ため口可）
  - 短めの文で話す
  - 相手の発言をさりげなく繰り返す
constraints: |
  - 政治・宗教・暴力的な話題は穏やかに回避する
  - 個人情報（住所・電話番号等）は教えない
```

### response_rules_block（固定）

```
## 応答ルール

- ユーザーの発話が意味的に完結している場合は action=respond を返すこと。
- 発話が途中（「えーと」「それで」「あの...」など間投詞・文末不完全）の場合は action=wait を返すこと。
- 応答は会話の流れに自然に続く長さにすること。長々と説明しない。
- 知覚情報や記憶が提供された場合は、不自然に列挙せず会話に自然に織り込むこと。
```

---

## コンテキスト構造

LLMに送信するコンテキストの構成要素と優先度の概要。

```
┌──────────────────────────────────────────┐
│ System Prompt (personality.yaml)         │ 固定  ─── 必ず含める
│    人格定義、応答ルール                    │
├──────────────────────────────────────────┤
│ Perception Snapshot                      │ 可変  ─── 削減優先度: 低（最初に削る）
│    vision / tactile 等の現在値            │        ttl切れは自動除外
├──────────────────────────────────────────┤
│ RAG Results                              │ 可変  ─── 削減優先度: 中
│    過去の会話要約（関連度でフィルタ）       │        類似度閾値以上のみ
├──────────────────────────────────────────┤
│ Conversation History                     │ 可変  ─── 削減優先度: 高（最後まで残す）
│    未要約の直近ターンのみ                  │        max_buffer_turnsターン
├──────────────────────────────────────────┤
│ Current Input                            │ 固定  ─── 必ず含める（末尾配置）
│    stt.final テキスト or 知覚トリガー内容  │        LLMが直前に見る内容
└──────────────────────────────────────────┘
```

トークン不足時の削減順序:

```python
CONTEXT_PRIORITY = [
    # 削れないもの（必ず含める）
    ("system_prompt",        MUST_INCLUDE),
    ("current_input",        MUST_INCLUDE),

    # 削れるもの（上から削る順 = 優先度低い順）
    ("perception_snapshot",  LOW),     # ttlで自然に量が制限される。最初に削る
    ("rag_results",          MEDIUM),  # 関連度の低いものから削る
    ("conversation_history", HIGH),    # wait蓄積中の文脈維持に必要。最後まで残す
]
```

---

## ユーザーターンのメッセージ構造

毎ターン、以下を結合して1つの `user` メッセージとして送る。
トークン不足時は**上から**削る（上位ほど優先度が低い）。
`input_block` を末尾に置く理由: LLMはコンテキスト末尾から生成を開始するため、
現在の発話が末尾にある方が応答の自然さが向上する（"lost in the middle"問題の回避）。

```
{perception_block}    ← 省略可（最初に削る。ttl切れでも自動除外）
{rag_block}           ← 省略可（次に削る。低スコアエントリから順に）
{history_block}       ← 省略可（最後の手段。古いターンから削る）
{input_block}         ← 必須（絶対に削らない。LLMが直前に見る内容）
```

**history_block を rag_block より優先する理由:**
wait蓄積中は「直近の会話の流れ」がないと respond/wait の判定精度が下がる。
過去の長期記憶（RAG）より直近の文脈を優先して保持する。

### perception_block

```
## 現在の知覚情報
{知覚エントリをテキスト形式で列挙}
```

例:
```
## 現在の知覚情報
- [視覚] ユーザーが正面に座っている。表情は疲れた様子。（15秒前）
- [触覚] 右手に軽い接触あり。（2秒前）
```

### rag_block

```
## 関連する過去の記憶
{要約テキストを日付つきで列挙}
```

例:
```
## 関連する過去の記憶
- 2025-12-15: ユーザーは車を買い替えた話をした。新車はフィット。
- 2026-01-03: 仕事のプロジェクトが年明けから多忙になると言っていた。
```

### history_block

```
## 会話履歴
{未要約の直近ターンをHuman/Assistantで列挙}
```

例:
```
## 会話履歴
Human: 最近どうだっけ
Assistant: なんか疲れてそうだね、大丈夫？
Human: まあね（中断）
```

中断ターンは `（中断）` サフィックスを付与する。

### input_block

```
Human: {accumulated_utterance}
```

`accumulated_utterance` は ConversationManager の `pending_utterance` バッファ + 今回の `stt.final` を結合したもの。

---

## トークン予算（目安）

Claude Sonnet のコンテキストウィンドウは 200k トークン。
通常の会話では以下の範囲に収まると想定する。

| ブロック | 目安トークン数 | 備考 |
|---------|--------------|------|
| システムプロンプト | 300〜800 | personality.yaml のサイズに依存 |
| perception_block | 0〜300 | ttlで自然に量が制限される |
| rag_block | 0〜1,000 | 類似度閾値で件数制御。上限は設定値 `rag_max_tokens` |
| history_block | 0〜2,000 | `max_buffer_turns` で制御。MemoryWorkerの要約により短く保たれる |
| input_block | 50〜300 | 日本語の発話1ターン分 |
| **合計（入力）** | **350〜4,400** | 通常はコンテキスト上限に達しない |
| 出力（respond） | 50〜500 | 会話的な短い応答を想定 |
| 出力（wait） | 1〜5 | 空文字のみ |

**トークン超過時の削減順序:**
`perception_block` を省略 → `rag_block` の低スコアエントリから削る → `history_block` の古いターンから削る。
`system_prompt` と `input_block` は削らない（MUST_INCLUDE）。

具体的なトークン数の上限値は personality.yaml または llm.yaml で設定可能とする。

```yaml
# llm.yaml（設定例）
token_budget:
  rag_max_tokens: 1000
  history_max_tokens: 2000
  perception_max_tokens: 300
```

---

## APIリクエスト設定

> **[モデル依存]** パラメータ名・値は Claude API 固有。他モデルでは `model` ID・`tool_choice` の形式が異なる。
> `temperature` / `max_tokens` / `stream` は多くのモデルで同名だが値の意味が異なる場合がある。

| パラメータ | 推奨値 | 備考 |
|-----------|--------|------|
| `model` | `claude-sonnet-4-6` | 設定ファイルで上書き可 |
| `temperature` | `0.7` | 会話の自然さとブレのバランス。要チューニング |
| `max_tokens` | `1024` | 通常応答は短いが余裕を持たせる |
| `stream` | `true` | TTSWorkerへの逐次送信に必要 |
| `tool_choice` | `{"type": "tool", "name": "reply"}` | 毎回固定（Claude API 固有形式） |

ストリーミングとツール呼び出し強制は Claude API で両立可能。
ツール引数がストリーミングで流れてくるため、`text` フィールドを部分的に受信しながら TTSWorker へ転送できる。

---

## ストリーミング時のチャンク転送

> **[モデル依存]** `input_json_delta` / `partial_json` は Claude API 固有のイベント形式。
> 他モデルへの移行時の対応例:
> - OpenAI: `delta.tool_calls[0].function.arguments` に部分JSONが蓄積される
> - Ollama: `message.content` に部分テキストが流れる（ツール呼び出し形式は異なる）
>
> `extract_text_delta` の実装はモデルごとに書き換えが必要。インターフェースは共通にできる。

ツール呼び出し強制時、ストリーミングチャンクは `input_json_delta` として流れる。
`text` フィールドの値が確定するにつれて逐次送信できる。

```python
# 擬似コード: ストリーミング受信 → TTSWorker転送
async for chunk in stream:
    if chunk.type == "content_block_delta":
        if chunk.delta.type == "input_json_delta":
            partial_json += chunk.delta.partial_json
            # textフィールドの増分を抽出してTTSWorkerへ転送
            new_text = extract_text_delta(partial_json)
            if new_text:
                await event_bus.publish("llm.response_chunk", {"text": new_text})
```

`extract_text_delta` はJSONの部分文字列から `"text": "..."` の増分を取り出すユーティリティ。
完全なJSONが確定した時点で `action` を確認し、`wait` であれば `llm.response_chunk` を発行しない。

---

## 能動発話時の差分

`perception.trigger` による能動発話（IDLE時）は `stt.final` がない。
この場合、`input_block` の代わりに以下を使用する。

```
## 状況
{知覚トリガーの内容}

上記の状況をふまえ、自然に一言話しかけてください。
```

`action` は `respond` 固定（能動発話で `wait` を返すことはない）をシステムプロンプトに明記する。
