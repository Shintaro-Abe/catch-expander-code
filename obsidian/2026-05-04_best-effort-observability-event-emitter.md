---
title: "best-effort イベント書き込み設計（observability が本番を壊さない）"
type: pattern
created: 2026-05-04
updated: 2026-05-04
expires_review: 2026-11-04
confidence: high
tags:
  - observability
  - pattern
  - lambda
  - dynamodb
  - reliability
  - best-effort
  - architecture
aliases:
  - best-effort EventEmitter パターン
  - observability 本番分離設計
  - NoOpEmitter フォールバック
related:
  - "[[2026-04-29_codex-iterative-review-finds-multilayer-misses]]"
---

# best-effort イベント書き込み設計（observability が本番を壊さない）

## TL;DR

- 監視・観測用のイベント書き込みコードがメインの業務処理を止めてはいけない。
- DynamoDB への `PutItem` が失敗しても **`logging.error` だけ出して続行**（例外を再 raise しない）。これは「握りつぶし」だが **意図的**。
- `EVENTS_TABLE` 環境変数が未設定のとき（ローカル開発・CI）は `_NoOpEmitter` に自動フォールバックして、メインコードに条件分岐を混入させない。
- `sequence_number` はインスタンス内でカウントアップして **同一実行内のイベント順序を保証**する。
- Lambda Layer 化して複数 Lambda から共有する場合、IAM の `dynamodb:PutItem` 権限は **各 Lambda の実行ロール** に付与する（Layer 側ではなく）。

---

## 何を解決するか / Problem Addressed

observability コード（メトリクス・トレース・イベント記録）には固有の危険がある:

1. **本番障害の連鎖**: DynamoDB のスロットリングや一時的ネットワーク障害で observability の `PutItem` が失敗し、その例外がメインロジックに伝播してビジネス処理まで止まる。
2. **環境差異による起動失敗**: `EVENTS_TABLE` 環境変数が設定されていないローカル環境・CI でインポート時にクラッシュしたり、全 Lambda に `if EVENTS_TABLE:` の条件分岐が散らかる。
3. **イベント順序の曖昧さ**: 同一 Lambda 実行内で複数のイベントを発火したとき、DynamoDB に入った順序だけでは因果順序が保証されない。

このパターンはこれらの問題を、**メインコードへの侵食を最小化**しながら解決する。

---

## 前提・適用範囲 / Applicability

### 適用が効く条件

- **業務処理の可用性 > observability データの完全性** のトレードオフが受容できる（イベントが数件欠損しても業務は止めない）。
- DynamoDB または類似の NoSQL ストアへのイベント書き込みを行うサーバレス構成。
- ローカル開発・CI でイベントテーブルが存在しない（またはアクセスしたくない）環境。

### 適用が弱い・不要な条件

- **イベントの損失が絶対に許容されない**監査ログ（金融取引ログ、規制要件のある操作記録）→ SQS Dead Letter Queue + Kinesis Firehose など耐障害性の高い経路が必要。
- 書き込み失敗率が高く、`logging.error` が埋もれてしまう環境 → アラート機構と組み合わせて閾値を監視する。

---

## 実装 / Implementation

### コアパターン

```python
# src/observability/event_emitter.py（概略）
import logging
import os
import time
import boto3

logger = logging.getLogger(__name__)

class EventEmitter:
    def __init__(self, table_name: str):
        self._table = boto3.resource("dynamodb").Table(table_name)
        self._sequence = 0

    def emit(self, event_type: str, payload: dict, status_at_emit: str = "success"):
        self._sequence += 1
        try:
            self._table.put_item(Item={
                "event_type": event_type,
                "timestamp": int(time.time() * 1000),
                "sequence_number": self._sequence,
                "payload": payload,
                "status_at_emit": status_at_emit,
                "ttl": int(time.time()) + 90 * 24 * 3600,  # 90日後に自動削除
            })
        except Exception as e:
            logging.error("event emit failed (type=%s): %s", event_type, e)
            # 意図的に握りつぶす: observability の失敗でビジネス処理を止めない


class _NoOpEmitter:
    """EVENTS_TABLE 未設定時のフォールバック。メソッドシグネチャは EventEmitter と同一。"""
    def emit(self, *args, **kwargs):
        pass  # サイレントにスキップ（ログも出さない）


# モジュールロード時に一度だけ判定してシングルトン生成
_EVENTS_TABLE = os.environ.get("EVENTS_TABLE")
emitter: EventEmitter | _NoOpEmitter = (
    EventEmitter(_EVENTS_TABLE) if _EVENTS_TABLE else _NoOpEmitter()
)
```

### 呼び出し側（メインロジック）

```python
# src/observability/__init__.py でエクスポートしておく
from observability.event_emitter import emitter

# Lambda ハンドラ内
def handler(event, context):
    emitter.emit("execution_started", {"execution_id": execution_id})
    try:
        result = run_main_logic()
        emitter.emit("execution_completed", {"execution_id": execution_id, "result": result})
        return result
    except Exception as e:
        emitter.emit("execution_failed", {"execution_id": execution_id, "error": str(e)}, status_at_emit="error")
        raise  # ビジネス例外は再 raise する（observability 例外とは分ける）
```

メインコードに `if EVENTS_TABLE:` の条件分岐が一切不要なのが `_NoOpEmitter` の価値。

### DynamoDB テーブル設定

```yaml
# SAM / CloudFormation 例
EventsTable:
  Type: AWS::DynamoDB::Table
  Properties:
    TableName: !Ref EventsTableName
    BillingMode: PAY_PER_REQUEST
    AttributeDefinitions:
      - AttributeName: event_type
        AttributeType: S
      - AttributeName: timestamp
        AttributeType: N
    KeySchema:
      - AttributeName: event_type
        KeyType: HASH
      - AttributeName: timestamp
        KeyType: RANGE
    TimeToLiveSpecification:
      AttributeName: ttl       # TTL 属性は DDB 側でも有効化が必要
      Enabled: true
```

TTL は **`emit` 時に Item に入れる** だけでは足りない。DynamoDB テーブルの TTL 設定で `ttl` 属性を有効化しないと自動削除は動かない。

---

## 落とし穴 / Caveats

### 1. 握りつぶしは意図的だが `logging.error` は必ず残す

`except` で何もしない（pass）だとサイレント失敗になり、DynamoDB スロットリングが慢性化しても気づけない。`logging.error` を必ず残してアラート・ログ監視で検出できるようにする。

```python
# NG: 完全サイレント
except Exception:
    pass

# OK: エラーログを残す
except Exception as e:
    logging.error("event emit failed (type=%s): %s", event_type, e)
```

### 2. TTL は DDB テーブル設定と Item の両方が必要

- Item に `ttl` フィールドを入れるだけ → DDB は TTL 属性を認識しないので削除されない。
- DDB テーブルの TTL を有効化するだけ → Item に `ttl` フィールドがなければ削除対象にならない。

両方必要。忘れるとテーブルが無限に肥大化する。

### 3. `sequence_number` はインスタンススコープ（プロセス再起動でリセット）

`sequence_number` は `EventEmitter` インスタンス内でのカウントアップであり、**同一 Lambda 実行内の順序保証**が目的。Lambda コンテナが再利用されると同一インスタンスが使われるためカウントは継続するが、コンテナが入れ替わると 1 から再スタートする。複数 Lambda インスタンス間のグローバル順序は保証しない。

### 4. Lambda Layer の IAM は Layer ではなく各 Lambda の実行ロールに付与する

EventEmitter を Lambda Layer として複数 Lambda で共有する場合、`dynamodb:PutItem` の権限は Layer の「リソースポリシー」ではなく、**各 Lambda 関数の実行ロール（IAM Role）** に付与する。Layer はコードを提供するだけで IAM 権限は持たない。

```json
// 各 Lambda の実行ロールに追加するポリシー例
{
  "Effect": "Allow",
  "Action": ["dynamodb:PutItem"],
  "Resource": "arn:aws:dynamodb:<region>:<account>:table/<EventsTableName>"
}
```

### 5. テストでの emit アサーションには mock が必要

ローカル・CI では `_NoOpEmitter` が使われるため、`emitter.emit` を呼んでも何も起きない。テストで「emit が呼ばれたか」「emit に渡した引数が正しいか」をアサーションしたい場合は `unittest.mock.patch` で `emitter` を差し替える。

```python
# テスト例
from unittest.mock import patch, MagicMock

def test_emit_called_on_success():
    mock_emitter = MagicMock()
    with patch("my_lambda.handler_module.emitter", mock_emitter):
        handler(event, context)
    mock_emitter.emit.assert_called_with(
        "execution_completed", {"execution_id": "abc123"}
    )
```

---

## 実装ファイル

- `src/observability/event_emitter.py` — `EventEmitter` / `_NoOpEmitter` / シングルトン `emitter`
- `src/observability/__init__.py` — `emitter` のエクスポート

---

## 関連ナレッジ / Related

- [[2026-04-29_codex-iterative-review-finds-multilayer-misses]] — この EventEmitter 実装を Codex レビューで 4 回連続で回した記録。isinstance ガードの発見プロセスが参考になる。
- [[feedback_anti_pattern_discipline]] — 再発バグエリアでの 3 層代替案規律。observability の書き込み失敗対策でも「プロンプト / パイプライン / 型」に相当する 3 層で代替案を比較してから実装を決める。

---

## 参考文献 / References

- AWS 公式ドキュメント — DynamoDB Time to Live (TTL): <https://docs.aws.amazon.com/amazondynamodb/latest/developerguide/TTL.html>
- AWS 公式ドキュメント — Lambda Layer のリソースポリシーと実行ロールの関係: <https://docs.aws.amazon.com/lambda/latest/dg/access-control-resource-based.html>
- AWS 公式ドキュメント — Lambda 関数の実行ロール: <https://docs.aws.amazon.com/lambda/latest/dg/lambda-intro-execution-role.html>
- Google SRE Book — Chapter 6: Monitoring Distributed Systems（observability はビジネスクリティカルパスから分離する設計原則）: <https://sre.google/sre-book/monitoring-distributed-systems/>
