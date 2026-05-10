# 設計: fix loop での content_blocks 構造的保護

## 設計方針

### 基本コンセプト

`_PRESERVED_DELIVERABLE_FIELDS` の単純拡張 (案 A) は **fixer の修正版を強制的に旧版で上書きする** regression を生む。一方で「fixer 応答を全面信頼」(現状) は **fixer が omit/empty を返すと content_blocks が消失する** バグを残す。

両者の中間として、**「fixer の値が valid なら採用、無効なら旧版に fallback」** を行う **条件付き fallback** をパイプライン層に追加する。プロンプト指示への依存ゼロ、決定論的に判定可能、`_PRESERVED_DELIVERABLE_FIELDS` 契約は不変。

### 設計レイヤの選定

| 層 | 案 | 採否 | 根拠 |
|---|---|---|---|
| プロンプト層 | fix_prompt 強化 (案 C) | ❌ | LLM 確率挙動依存。`8c5b220` で経験則として導入済みで、それでも 5/9 に崩れた |
| パイプライン層 | 条件付き fallback (本案) | ✅ | 決定論的、影響範囲局所、既存契約維持 |
| 型層 | TypedDict / Pydantic (案 D) | ❌ | 影響範囲過大。本タスクの最小コスト原則と乖離 |

### 局所化の原則

- 変更は `_run_review_loop` の fix loop 内 deliverables 置換ブロック (`orchestrator.py:1499-1513`) と、新規ヘルパー関数 1 つに限定
- `_PRESERVED_DELIVERABLE_FIELDS` 定数は `("code_files",)` のまま不変
- 関数シグネチャ・戻り値・呼出グラフは不変
- 新規モジュール / 新規依存追加なし

## 実装アプローチ

### Step 1: 判定ヘルパー関数の新設

`src/agent/orchestrator.py` のモジュールトップ (`_PRESERVED_DELIVERABLE_FIELDS` 直後) に判定関数を追加する。

```python
def _classify_content_blocks_fallback_reason(parsed: dict) -> str | None:
    """fixer 応答の content_blocks が fallback すべき無効値か判定する。

    Returns:
        - "missing_key": parsed に content_blocks キーが存在しない
        - "none_value": parsed["content_blocks"] が None
        - "non_list": parsed["content_blocks"] が list 型ではない
        - "empty_list": parsed["content_blocks"] が空 list
        - None: parsed["content_blocks"] が valid な non-empty list (fallback 不要)

    Notes:
        本判定は fix loop の deliverables 置換ロジックでのみ使用する。
        判定基準は requirements.md AC-1 と 1:1 対応。
    """
    if "content_blocks" not in parsed:
        return "missing_key"
    value = parsed["content_blocks"]
    if value is None:
        return "none_value"
    if not isinstance(value, list):
        return "non_list"
    if len(value) == 0:
        return "empty_list"
    return None
```

**判定の独立性**:
- `parsed` 全体を受け取り、キーの存在と型・値を一括チェック
- 呼び出し側で個別 if を書かないことで、テスト容易性 (1 関数で 5 ケース網羅) と保守性を担保
- 戻り値は string 定数 (loggable) または None (no fallback) の Optional

### Step 2: fix loop 内置換ブロックの改修

`orchestrator.py:1492-1513` を以下に書き換える。**変更箇所は最小限**。

```python
parsed = _parse_claude_response(fix_raw)
if parsed.get("parse_error"):
    logger.warning(
        "Fix attempt produced unparseable response, keeping previous deliverables",
        extra={"loop": loop, "issues_count": len(errors)},
    )
else:
    # content_blocks の旧版を deliverables 置換前にスナップショット
    prev_content_blocks = current_deliverables.get("content_blocks")
    preserved = {
        k: current_deliverables[k] for k in _PRESERVED_DELIVERABLE_FIELDS if k in current_deliverables
    }
    _accumulate_fixer_notes(accumulated_fixer_notes, parsed)
    current_deliverables = parsed
    current_deliverables.update(preserved)

    # ▼ 構造的保護: fixer 応答が content_blocks を omit/invalid で返した場合のみ旧版に fallback
    fallback_reason = _classify_content_blocks_fallback_reason(parsed)
    if fallback_reason is not None and isinstance(prev_content_blocks, list) and prev_content_blocks:
        logger.warning(
            "Fix loop fixer omitted/invalid content_blocks; falling back to previous version",
            extra={
                "loop": loop,
                "reason": fallback_reason,
                "previous_blocks_count": len(prev_content_blocks),
            },
        )
        current_deliverables["content_blocks"] = prev_content_blocks

    logger.info(
        "Deliverables updated by review fix",
        extra={
            "loop": loop,
            "issues_count": len(errors),
            "preserved_fields": list(preserved.keys()),
            "content_blocks_fallback": fallback_reason,  # observability: None なら fallback なし
        },
    )
```

### 設計上の判断ポイント

#### ポイント 1: `prev_content_blocks` のスナップショット取得タイミング

`current_deliverables = parsed` で `current_deliverables` の参照先が `parsed` dict に置き換わる。**置換前に旧 dict から `content_blocks` を取り出す**ことで、置換後でも旧版にアクセスできる。

`current_deliverables.update(preserved)` は code_files のみを戻すため、content_blocks の保護にはこのスナップショットが必須。

#### ポイント 2: 旧版自身が無効値だった場合の挙動

`prev_content_blocks` 自体が None / non-list / empty list の場合、fallback しても無意味。条件 `isinstance(prev_content_blocks, list) and prev_content_blocks` で **旧版が valid non-empty list の場合のみ fallback** を実行する。

旧版も無効だった場合 (例: 初回 fix loop 直前の generator 応答時点で既に content_blocks が空) は、parsed の値 (None / non-list / empty) をそのまま受け入れる。これは fix loop の責任範囲外であり、generator 段階の問題として扱う (本 steering スコープ外)。

#### ポイント 3: warning ログの構造化フィールド

`reason` を string 定数で出すことで、CloudWatch Logs Insights / Athena 等での後段集計が容易になる。`previous_blocks_count` を併記することで、fallback の影響範囲 (何ブロック復元したか) も観測可能。

`logger.info` の `Deliverables updated by review fix` には `content_blocks_fallback` フィールドを追加し、**fallback が発動しなかった通常ケースでも `None` を記録**する。これにより「fallback が走らなかった = 健全」の観測も可能 (silent success の可視化)。

#### ポイント 4: events テーブル emit を追加しない

requirements.md AC-2 通り、events テーブルへの emit は本 steering スコープ外。理由:

- TokenMonitor still_valid emit 抜けバグ (`memory/project_token_monitor_still_valid_no_emit.md`) と同じ層 (`EventEmitter`) を触ることになり、副次的な regression リスクがある
- `content_blocks_fallback` イベント名・schema は dashboard 設計と合わせて別 steering で議論する余地を残す
- 本タスクは structlog の `logger.warning` レベルで観測可能であり、運用上の検知には十分

#### ポイント 5: parse_error 経路の挙動は変更しない

`parsed.get("parse_error")` true 経路では従来通り `current_deliverables` を据え置く (`current_deliverables` 自体への代入が走らないため、content_blocks も自動的に保持される)。本タスクは parsed が成功した経路のみ対象。

## 変更するコンポーネント

| ファイル | 変更内容 | 行数目安 |
|---|---|---|
| `src/agent/orchestrator.py` | ヘルパー関数 `_classify_content_blocks_fallback_reason` 追加 (モジュールトップ)、fix loop 内置換ブロック改修 (`:1499-1513`) | +30 行 |
| `tests/unit/agent/test_orchestrator.py` | テスト 6 ケース追加 (詳細は後述) | +120 行 |
| `docs/functional-design.md` | レビュー機能の節に「fix loop content_blocks 構造的保護」段落追加 | +10 行 |

## データ構造の変更

**変更なし**。

- `current_deliverables` の dict 構造は不変
- `_PRESERVED_DELIVERABLE_FIELDS` 定数は `("code_files",)` のまま
- DynamoDB / Notion / S3 への永続化スキーマは不変
- イベントスキーマも不変

新規追加されるのは `logger.warning` / `logger.info` の `extra` フィールドの追加のみ (構造化ログのキー追加であり、後方互換)。

## 影響範囲の分析

### 直接影響

- **`_run_review_loop` の fix loop**: 条件付き fallback が追加される。fixer が valid を返す場合 (大多数のケース) は挙動不変
- **構造化ログ**: `logger.warning` 1 種追加、`logger.info` の `extra` フィールド 1 つ追加

### 間接影響 (リスク評価)

| リスク | 評価 | 緩和策 |
|---|---|---|
| 既存 fix loop テストの破壊 | 低 | 既存テストは valid な content_blocks を返すモックが大半 → fallback 経路に入らないため挙動不変 |
| valid content_blocks の意図せぬ fallback | 極低 | 判定基準は厳密 (None / non-list / empty list のみ) で、valid non-empty list は素通し |
| code_files への影響 | なし | `_PRESERVED_DELIVERABLE_FIELDS` 既存ロジックは無変更 |
| 新規パフォーマンス劣化 | なし | 追加処理は dict.get + isinstance + len のみ (O(1)) |
| `_run_review_loop` の他経路 (review pass / 上限到達) | なし | 改修は fix attempt 後の置換ブロックのみ。pass / limit 経路は未変更 |

### 関連メモリ整合チェック

- `memory/project_review_loop_recurring_patch_site.md`: 6 件目の patch site 該当。**ただし層が異なる**ため対症療法アンチパターン非該当 (requirements.md で論証済み)
- `memory/feedback_anti_pattern_discipline.md`: 3 層代替案規律遵守 → requirements.md 「非対応」セクションで明示済み
- `memory/project_review_loop_content_blocks_loss.md`: 本 steering で解決対象とする構造バグ

## ユニットテスト設計

`tests/unit/agent/test_orchestrator.py` の `TestReviewLoop` クラス (またはそれに準ずるクラス) に以下 6 ケースを追加する。

### 共通 fixture

既存の `_run_review_loop` テストで使われている `call_claude` モックパターンを踏襲する。fixer 応答のみ差し替えて挙動を確認する。

```python
# 概念図 (実装は既存テストの mock パターンに合わせる)
def _make_fixer_response(content_blocks_value):
    """fixer 応答 JSON を生成。content_blocks_value=_OMIT で key 自体を省略"""
    response = {
        "content_blocks": content_blocks_value,
        "summary": "fixed summary",
        "quality_metadata": {"sources_verified": 1, ...},
    }
    if content_blocks_value is _OMIT:
        del response["content_blocks"]
    return json.dumps(response)
```

### テストケース一覧

| # | テスト名 | fixer の content_blocks | 期待挙動 |
|---|---|---|---|
| 1 | `test_fix_loop_preserves_content_blocks_when_fixer_omits_key` | キー自体を省略 | 旧版 content_blocks が deliverables に残る |
| 2 | `test_fix_loop_preserves_content_blocks_when_fixer_returns_none` | `None` | 旧版 content_blocks が deliverables に残る |
| 3 | `test_fix_loop_preserves_content_blocks_when_fixer_returns_empty_list` | `[]` | 旧版 content_blocks が deliverables に残る |
| 4 | `test_fix_loop_preserves_content_blocks_when_fixer_returns_non_list` | `"some string"` | 旧版 content_blocks が deliverables に残る |
| 5 | `test_fix_loop_uses_fixer_content_blocks_when_valid` | `[{"type": "heading_2", ...}]` | fixer 版が deliverables に採用される (既存挙動維持) |
| 6 | `test_fix_loop_logs_warning_on_fallback` | `None` (case 2 と同条件) | `logger.warning` が呼ばれ、`extra` に `loop`/`reason`/`previous_blocks_count` が含まれる |

### テスト構造

各テストは以下の 3 段構造 (AAA):

1. **Arrange**: orchestrator + Slack/Notion/Storage モック setup、generator は valid な content_blocks を返し、reviewer は 1 回 issue を返してから pass、fixer は当該パターンで応答
2. **Act**: `_run_review_loop` を実行
3. **Assert**:
   - 戻り値の `current_deliverables["content_blocks"]` が期待値か
   - case 6 のみ `caplog` (or `mock.patch` 経由の logger) で warning ログ検証

### Regression 防止

- 既存 `TestReviewLoop` 系テスト全件 pass を最終確認 (`pytest tests/unit/agent/test_orchestrator.py -k 'TestReviewLoop' -v`)
- pre-existing `call_codex` モック不整合テストはスキップ扱い (requirements.md AC-3 注記通り、本 steering スコープ外)

## Codex 連続レビュー計画

### 実施タイミング

タスクリスト T-3 (実装完了) 後、T-4 (テスト pass 確認) 後に Codex レビュー (1 回目)。指摘修正後 2 回目を回す。

### 承認ルール

- `memory/feedback_codex_review_requires_approval.md` 準拠: 1 回目完了後にユーザー承認を得てから 2 回目を実施
- WSL2 sandbox 回避策 (`memory/feedback_codex_wsl2_sandbox.md`): VS Code ターミナルで `codex -c sandbox_mode="danger-full-access"` を直接実行

### 収束判定

- 1 回目で P1 指摘ゼロ かつ 2 回目で新規 P1/P2 ゼロ → 収束
- それ以外は 3 回目まで実施 (`memory/2026-04-29_codex-iterative-review-finds-multilayer-misses.md` の経験則に準拠)

### レビュー範囲

- `src/agent/orchestrator.py` の変更箇所
- `tests/unit/agent/test_orchestrator.py` の追加テスト
- 構造的保護ロジックの正当性 (条件分岐の網羅性、エッジケース見落としの有無)

## 実機検証計画

### Phase 1: dev デプロイ前のローカル確認

- `pytest tests/unit/agent/test_orchestrator.py` 全件 pass
- `python -m pytest tests/unit/agent/test_orchestrator.py::TestReviewLoop -v` で本 steering 関連テスト全件 pass

### Phase 2: dev デプロイ

- GitHub Actions の CI 経由で dev 環境へ自動デプロイ (`memory/feedback_deploy_after_ci_completion.md` 準拠、ローカル `deploy-agent.sh` は使わない)
- ECS Task Definition revision 更新を CloudWatch Logs で確認

### Phase 3: Slack 投入による fix loop 発火検証

- **fix loop が走るトピックの選定**: reviewer が指摘を返しやすい複雑な技術トピック (例: 「AWS Lambda の SnapStart と Provisioned Concurrency の使い分け」「Kubernetes HPA と KEDA の連携」等)
- Slack で 1 件投入し、DynamoDB events テーブルで `Deliverables updated by review fix` が記録されることを確認
- Notion ページに **品質情報ブロック + 記事本体 (content_blocks) 両方表示**を目視確認
- CloudWatch Logs で `Fix loop fixer omitted/invalid content_blocks; falling back to previous version` warning が出ているか / 出ていないかを観測

### Phase 4: fix loop が発火しなかった場合の代替検証

reviewer が一発 pass した場合 (fix loop 未走行) は本 steering の保護経路を通らない。その場合の代替検証:

- 別トピックを再投入して fix loop 発火を再試行 (最大 3 回)
- 3 回連続で fix loop 未発火なら、**ローカル実行**で `_run_review_loop` を直接呼ぶ統合テスト (mock fixer で content_blocks=None を返す) で構造保護を確認 → tasklist に追加タスクとして記載

## ドキュメント整合計画

### docs/functional-design.md への追記

レビュー機能の節 (`### レビュー修正ループ` 等のセクション) に以下の段落を追加:

```markdown
#### fix loop での content_blocks 構造的保護

fix loop の deliverables 置換時、fixer LLM 応答が content_blocks を omit / 空 list / 非 list で返した場合、
直前の valid な content_blocks を自動で引き継ぐ条件付き fallback を実装している。fixer が valid な
non-empty list を返した場合は通常通り fixer の修正版が採用される。本保護は plain Python の決定論的処理であり、
プロンプト指示への依存はない。発動時は `logger.warning` でログ記録される。
```

### obsidian への学び記録 (任意)

`obsidian/` ディレクトリに 5/9 インシデント + 5/10 構造修正の経緯を記録するエントリを追加 (任意、ユーザー判断)。

## 次フェーズ予告: tasklist.md の構成案

design.md 承認後、tasklist.md を起草する。タスク粒度の予告:

- **T-1**: ヘルパー関数 `_classify_content_blocks_fallback_reason` 追加
- **T-2**: fix loop 内置換ブロックの改修 (`orchestrator.py:1499-1513`)
- **T-3**: ユニットテスト 6 ケース追加
- **T-4**: 既存テスト regression 確認
- **T-5**: Codex 連続レビュー (1 回目)
- **T-6**: Codex 指摘対応 (必要時)
- **T-7**: Codex 連続レビュー (2 回目)
- **T-8**: docs/functional-design.md 更新
- **T-9**: dev デプロイ (CI 経由)
- **T-10**: 実機検証 (Slack 投入)
- **T-11**: メモリ更新 (本 steering 完了状態の記録)

## 設計上の判断事項 (確定)

design.md 承認時 (2026-05-10) にユーザー判断で確定:

1. **ヘルパー関数の配置**: ✅ モジュールトップ (`_PRESERVED_DELIVERABLE_FIELDS` 直後) に配置。テスト容易性と他のモジュールトップ関数 (`_extract_source_domains` 等) とのスタイル一貫性を優先
2. **`logger.info` への `content_blocks_fallback` フィールド追加**: ✅ 追加する (None 含む)。silent success を CloudWatch Logs Insights で集計可能にし、fallback 発動頻度を継続観測する
3. **テスト追加先**: ✅ 既存 `TestReviewLoop` クラスに追加。test discovery と命名規約の一貫性、fix loop テストの集約を優先
