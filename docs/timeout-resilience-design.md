# タイムアウト・リトライ・障害復旧設計

## 概要

各Worker・外部APIのタイムアウト仕様、リトライ方針、障害検出・通知・復旧パスを定義する。
リアルタイム会話の体験を損なわないことを最優先とし、初期は最小限の仕組みを入れて実運用でチューニングする方針をとる。

### 設計方針

1. **タイムアウトはWorker/API種別ごとに個別設定** — 処理特性（ストリーミング/一括、ローカル/クラウド）が異なるため
2. **リトライは最小限** — リアルタイム会話ではリトライによる遅延が体験を直接損なう。初期は0-1回
3. **障害時はイベントで通知** — `worker.status` イベントでモニタリング可能にする
4. **回復は初期状態リセット** — 障害解消後、次のリクエストで自動的にREADYに戻る
5. **チューニングは実運用後** — 使用するAPI・モデルによって挙動が変わるため、具体値は運用で調整

---

## Worker稼働状態

全Workerが共通の稼働状態を持つ。Engine側で集約し、モニタリングGUI等から参照可能にする。

```
READY ──(タイムアウト/エラー発生)──► DEGRADED ──(連続失敗が閾値到達)──► DOWN
  ▲                                    │
  │                                    │
  └──(次のリクエスト成功)───────────────┘

DOWN ──(手動リセット or 自動復帰タイマー)──► READY
```

| 状態 | 意味 | 挙動 |
|------|------|------|
| `READY` | 正常稼働中 | 通常処理 |
| `DEGRADED` | エラー発生、リトライ中 | リトライポリシーに従って処理を試行 |
| `DOWN` | 連続失敗で機能停止 | リクエストを受け付けない。手動リセットまたは自動復帰タイマーで回復 |

### 状態遷移の閾値

```yaml
# health.yaml（デフォルト値、アバタープロジェクト側でオーバーライド可能）
health:
  degraded_to_down_threshold: 5    # 連続失敗回数。この回数に達したらDOWNへ遷移
  down_auto_recovery_seconds: 30   # DOWN状態からの自動復帰タイマー（0で無効=手動のみ）
```

具体的な閾値は実運用後にチューニングする。

---

## タイムアウト設定

Worker/API種別ごとに個別のタイムアウトを設定する。

### 設定構造

```yaml
# timeouts.yaml（デフォルト値、アバタープロジェクト側でオーバーライド可能）
timeouts:
  stt:
    request_timeout: 5000         # 1リクエストのタイムアウト(ms)

  llm:
    first_token_timeout: 10000    # 最初のチャンクが到着するまで(ms)
    stream_stall_timeout: 5000    # ストリーミング中にチャンクが途切れた場合(ms)

  tts:
    request_timeout: 3000         # 1文の合成タイムアウト(ms)

  memory:
    search_timeout: 2000          # RAG検索(ms)

  reaction:
    inference_timeout: 1000       # ローカル軽量モデル推論(ms)

  vision:
    inference_timeout: 5000       # 軽量なVLMモデル前提
```

### LLMWorkerのタイムアウト詳細

LLMWorkerはストリーミング応答のため、2種類のタイムアウトが必要。

```
LLM APIリクエスト送信
    │
    │← first_token_timeout（最初のチャンクが来るまで）
    ▼
最初のチャンク到着
    │
    │← stream_stall_timeout（チャンク間の最大間隔）
    ▼
次のチャンク到着
    │
    │← stream_stall_timeout
    ▼
  ...繰り返し...
    │
    ▼
llm.response_done
```

- `first_token_timeout`: APIの初回応答時間（TTFT）に対するタイムアウト。クラウドAPIは数秒かかることがあるため長めに設定
- `stream_stall_timeout`: ストリーミング中にチャンクが途切れた場合のタイムアウト。ネットワーク断やAPI側の異常を検出

### タイムアウト値の考え方

| Worker | タイムアウト | 根拠 |
|--------|------------|------|
| STT | 5s | 音声認識は通常1-3秒。5秒超えは異常 |
| LLM (TTFT) | 10s | クラウドAPIのTTFTは200ms-数秒。負荷時に増大するため余裕を持つ |
| LLM (stall) | 5s | ストリーミング中の5秒停滞は異常 |
| TTS | 3s | VOICEVOX一括合成は100-500ms。3秒超えは異常 |
| Memory | 2s | ローカルDB検索は30-60ms。2秒超えは異常 |
| Reaction | 1s | ローカル推論は15-40ms。1秒超えは異常 |
| Vision | 5s | SmolVLM等の軽量VLMを前提（VRAM 1GB程度、推論300ms周期）。5秒超えは異常 |

全て初期値であり、使用するAPI・モデルに応じてアバタープロジェクト側でオーバーライドする。

---

## リトライ方針

リアルタイム会話ではリトライによる遅延が体験を直接損なうため、初期は最小限とする。

| Worker | リトライ回数 | 方式 | 失敗時の挙動 |
|--------|------------|------|-------------|
| LLMWorker | 0-1回 | 即時リトライ | 諦めて `worker.status` で通知。会話ターンは不成立 |
| TTSWorker | 1回 | 即時リトライ（文単位） | その文をスキップして次の文へ進む |
| STTWorker | 0回 | — | ストリーミング中のリトライは困難。接続断の場合は再接続 |
| MemoryWorker | 0回 | — | RAG結果なしでLLMは応答可能。スキップ |
| ReactionWorker | 0回 | — | なくても会話は成立。スキップ |
| VisionWorker | 1回 | 即時リトライ | 低頻度なのでリトライしても遅延影響が小さい |

### リトライ判定

リトライ対象とするエラー種別:
- **タイムアウト** — リトライ対象
- **接続エラー（ネットワーク断）** — リトライ対象
- **HTTPステータス 429 (Rate Limit)** — リトライ対象（ただし即時ではなくバックオフが必要な場合あり）
- **HTTPステータス 5xx** — リトライ対象
- **HTTPステータス 4xx（429以外）** — リトライ対象外（設定ミスの可能性が高い）
- **レスポンスパースエラー** — リトライ対象外

リトライ回数・バックオフ間隔の具体値は実運用後にチューニングする。

---

## 障害通知

### worker.status イベント

全Workerが状態変化時に `worker.status` イベントを発行する。

```python
@dataclass
class WorkerStatus:
    worker: str          # "llm", "tts", "stt", "memory", "reaction", "vision"
    status: str          # "ready", "degraded", "down"
    error: str | None    # エラー内容（"timeout after 10000ms", "connection refused", ...）
    timestamp: float     # イベント発生時刻
```

### イベント発行タイミング

| 遷移 | トリガー |
|------|---------|
| READY → DEGRADED | タイムアウト、接続エラー、APIエラー等の発生 |
| DEGRADED → DOWN | 連続失敗が `degraded_to_down_threshold` に到達 |
| DEGRADED → READY | リトライ成功、または次のリクエストが正常完了 |
| DOWN → READY | 手動リセット、または自動復帰タイマー経過後の最初のリクエスト成功 |

### モニタリング

`worker.status` イベントはEventBus経由で配信される。以下から購読可能:

- **テスト用GUI** — 各Workerの状態をリアルタイム表示
- **WebSocketServer** — Unity Bridge側に通知を転送
- **Engine** — 全Workerの稼働状態を集約して保持

---

## 回復パス

### 自動回復（基本）

パイプラインはイベント駆動のため、上流Workerが復帰すればイベントが再び流れ始め、下流Workerも自然に動作を再開する。特別な復帰シーケンスは不要。

```
例: LLMWorkerが一時的にタイムアウト → DEGRADED

  次のユーザー発話
    → stt.final 発行
    → LLMWorkerが新しいリクエストを送信
    → 成功 → READY に復帰
    → パイプラインは正常動作
```

### 手動リセット

GUIまたはAPI経由で以下のリセット操作を実行可能にする:

1. **Worker単体リセット** — 特定Workerの内部状態をクリアし、READY状態に戻す
2. **全体リセット** — 全Workerをリセットし、ConversationManagerをIDLE状態に戻す

全体リセットの処理:
```
① ConversationManager → IDLE状態に遷移
② 全Workerの内部キュー・バッファをクリア
③ 全Workerの稼働状態を READY にリセット
④ 進行中のLLM/TTSリクエストをキャンセル
```

---

## パイプライン障害時の影響範囲

各Workerの障害が他のWorkerに与える影響の整理。パイプラインは直列に繋がっているため、上流の障害は下流に波及する。これは構造上避けられないが、障害箇所を素早く特定し通知することで対応可能にする。

| 障害箇所 | 直接的な影響 | 会話への影響 |
|----------|------------|------------|
| STTWorker | LLMWorkerにstt.finalが届かない | 会話全体が停止 |
| LLMWorker | TTSWorkerに応答テキストが届かない | アバターが応答できない |
| TTSWorker | 音声が再生されない | テキスト応答は生成されているが音声が出ない |
| MemoryWorker | RAG結果なしでLLMが応答 | 会話は継続するが過去の記憶を参照できない |
| ReactionWorker | 表情・アニメーションが無反応 | 会話は継続するが表情が変わらない |
| VisionWorker | 視覚情報なしでLLMが応答 | 会話は継続するが環境認識ができない |

MemoryWorker / ReactionWorker / VisionWorker の障害は会話の継続に影響しない（グレースフルデグラデーション）。
STT / LLM / TTS の障害は会話パイプラインの根幹であるため、障害時は `worker.status` で即座に通知する。

---

## 未決事項（実運用後に決定）

実際にAPI・モデルを接続して動作させた後に、以下を調整する:

- タイムアウト値の具体的なチューニング
- リトライ回数・バックオフ間隔の調整
- DEGRADED → DOWN の閾値（連続失敗回数）
- DOWN状態の自動復帰タイマーの適切な値
- 障害時にアバターが何をするか（personality.yaml で設定可能にするかどうか）
- Rate Limit (429) 受信時のバックオフ戦略
