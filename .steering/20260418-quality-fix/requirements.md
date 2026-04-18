# 要求内容: 成果物品質問題の修正

## この作業をひとことで

**エージェントが壊れて出力している部分（出典番号の重複・レビュー修正の破棄・コード生成失敗）を直し、同じトピックで再実行してちゃんと動くことを確認する。**
判断の質（そもそも詰め込みすぎでは？等）は今回は別課題として後回しにする。

## 修正する 4つの「おかしさ」

| # | 症状（ユーザーから見えたもの） | 原因 | 修正内容 |
|---|-----------------------------|------|---------|
| **M1** | 出典番号 `[src-001]` があちこちに重複して、どの資料のことか追えない | 並列で走る5人のリサーチャーが、それぞれ独立に "1番, 2番, 3番..." と付番している | 各リサーチャーの番号にステップ名を前置きして、全体で一意の番号にする（例: `research-1:src-001`）|
| **M2** | レビュアーが「直してください」と指摘しても、直された内容が Notion に反映されない | レビュー修正後の成果物がコード上でローカル変数に入ったまま呼び出し元に戻されず捨てられている | レビューの戻り値に修正後の成果物も含めるよう変更 |
| **M3** | GitHub にコードがプッシュされない。`iac_code` と `program_code` を作る計画なのに生成が空で終わる | 「調査レポート + IaCコード + Pythonコード + 設計書 + 手順書」を 1回の応答に詰め込もうとして容量オーバー | コード生成を種類ごとに別々の呼び出しに分ける（1回で全部やらない）|
| **S1/S2** | 「検証済み3件」と出るが実際は49件中の3件しか見ていない / 「鮮度: 最新 None / 最古 None」と不親切 | レビュアーに検証範囲のルールがない / 公開日不明時の代替値指示がない | 「最低30%か10件検証」等のルールをプロンプトに追記、公開日不明は `"unknown"` と書くよう統一 |

## 作業の流れ（ざっくり）

```
1. コード修正 → ユニットテスト
2. M1, M2, M3 を個別コミット
3. 本番にデプロイ
4. 同じトピック「API GatewayとLambdaの組み合わせ」を Slack に再投入
5. 症状が消えたか確認（DB / CloudWatch / Slack / Notion / GitHub）
6. まだ問題があれば、次のステアリング（判断の質向上）を起票
```

---

## 背景

2026-04-17 23:23 JST（14:23 UTC）、実トピック「API GatewayとLambdaの組み合わせについて」を @Catch Expander に投入した結果、以下の品質問題が検出された（対象実行ID: `exec-20260417142327-14040fe5`）。

### 観測された症状（Slack 完了通知上の品質情報より）

```
検証ステータス: ✅ 出典検証済み: 3件
情報の鮮度: 最新 None / 最古 None
セルフレビュー結果: チェック項目 2/4 合格
注意事項:
  - 出典リスト内でsource_idが複数セクション間で重複（src-001〜src-010が複数回出現）
  - 出典のpublished_atがほぼ全てnull
  - HTTP APIの「低レイテンシ」「71%低コスト」は出典不十分のためwarning
  - レビュー修正上限（2回）に到達。未修正の指摘: 1件
```

**GitHub へのコードプッシュもなし**（`iac_code` / `program_code` が workflow に含まれていたにも関わらず）。

### DynamoDB / CloudWatch Logs による定量的裏付け

| 項目 | 実測値 |
|------|--------|
| 5 リサーチャーが emit した source 数合計 | 50 件 |
| DB 格納件数（URL dedup 後） | 49 件 |
| `source_id = "src-001"` の出現ステップ数 | **全 5 ステップ** |
| `published_at` が NULL の source | 49 件中 47 件（96%） |
| レビュアーが検証した source | 49 件中 **3 件**（6%）|
| `workflow_plan.storage_targets` | `["notion", "github"]` ✓ |
| `deliverable_types` | `[research_report, iac_code, program_code, architecture_design, procedure_guide]` ✓ |
| 最終 `deliverable.storage` | `"notion"` のみ（github なし） |
| 総実行時間 | 54 分 |
| Review loop 経過時間 | 34 分（CLI timeout 1回含む） |

### CloudWatch Logs タイムライン（抜粋）

```
14:40:01 INFO  Generating code files separately
14:43:12 WARN  Code file generation returned empty result      ← fallback失敗
15:04:04 WARN  Claude CLI error, retrying (terminated, 340s)   ← review中のtimeout
15:17:12 INFO  Review loop limit reached
15:17:13 INFO  Notion page created
```

## 課題の構造的分類

観測症状を構造バグに対応付けると以下の 4 系統にまとめられる。

### 課題 A: source_id がシステム全体で一意化されていない

- 並列実行される各リサーチャーが独立に `src-001, src-002, ...` と付番
- ジェネレーター入力・レビュアー入力・Notion 本文の参照まで重複 ID が流れる
- DynamoDB 書き込み時のみ UUID 上書き（`dynamodb_client.py:132`）で表面的にDB整合性を保っているが、本文との相互参照が壊れている

**影響**: 出典参照 `[src-003]` が複数の別文献を指し、読者が出典を特定不能

### 課題 B: コード成果物（GitHub push）が生成されない

- 初回ジェネレーター応答で `code_files` が null
- Fallback 呼び出し（`orchestrator.py:322-341`）も「empty result」で失敗
- Fallback が `iac_code + program_code` を 1応答で両方要求しており、応答サイズ超過で生成失敗する
- 結果として `iac_code` / `program_code` が選ばれても成果物化されない

**影響**: ユーザーが期待したコード成果物が届かない、GitHub リポジトリに何も残らない

### 課題 C: レビュー修正が永久破棄される

- `_run_review_loop` は `current_deliverables` をローカル変数で修正するが、戻り値は `review_result` のみ
- 呼び出し元 `run()` の `deliverables` は初回 gen の結果のまま、Notion にその古い内容が格納される
- 一方 `quality_metadata` はレビュー最終時点の「修正後を評価した」結果 → 表示と実体が乖離

**影響**:
- 「修正上限到達・未修正1件」と表示されるが実際は修正自体が適用されていない
- 品質情報が実本文を反映しない不正確な状態

### 課題 D: レビュアーの検証カバレッジが極度に低い

- `reviewer.md` に検証対象の選定戦略なし
- 49 件の出典中 3 件（6%）のみ WebFetch 検証
- `sources_verified = 3, sources_unverified = 0` という矛盾メタデータ（残り 46 件は未検証扱いされていない）

**影響**: ユーザーに「3件検証済み」と誤って品質を伝える（実際には 6% の限定的検証）

### 課題 E（付随）: published_at 取得フォールバック指針がない

- AWS 公式 Doc は `published_at` を持たないページが多い → リサーチャーが null を埋める
- `researcher.md` に不明時の代替値指示がない
- `information 鮮度: 最新 None / 最古 None` の原因

**影響**: 品質情報の鮮度評価が常に不能となる

### 課題 F（付随）: 並列リサーチャーの step_id 混線

- research-2 の結果 `result.step_id` が "research-1" として返る LLM エラーを実データで観測
- 原因: `researcher.md:68` のプロンプト例示が固定値 `research-1` + `orchestrator.py:457-464` のプロンプト組立で実 step_id が差し込まれていない

**影響**: ステップトレーサビリティが崩れる（実害は軽度）

## 受け入れ条件

本作業は、以下の条件をすべて満たすことで完了とする。

### 必須条件（Must）

- [ ] **AC-1**: 同一トピック（または類似の技術トピック）で再実行した場合、Slack 完了通知の「出典検証済み」表示が 10 件以上となること
- [ ] **AC-2**: `deliverable_types` に `iac_code` または `program_code` が含まれる実行では、`deliverable.storage` が `"notion+github"` となり、GitHub にコードがプッシュされること
- [ ] **AC-3**: Notion 本文中の `[src-XXX]` 参照とレビュアー / リサーチャーに渡す source 配列の source_id がすべて一意であること
- [ ] **AC-4**: レビュー修正ループで fix を行った場合、Notion に格納される本文がその修正結果を反映していること
- [ ] **AC-5**: 既存の全テスト（現状 125 テスト）がパスすること

### 推奨条件（Should）

- [ ] **AC-6**: `sources_verified` と合わせて `sources_total` が品質情報に表示され、検証率が明示されること
- [ ] **AC-7**: `published_at` が取得できない場合、`"unknown"` 等のフォールバック値が入り、品質情報の鮮度表示が「N/A」「不明あり」等で適切に表現されること

### 任意条件（Nice）

- [ ] **AC-8**: 並列リサーチャーの `result.step_id` が正しく自ステップの ID を返すこと
- [ ] **AC-9**: 修正ループでの Claude CLI 呼び出しサイズ（fix_prompt）が初回 gen_prompt より小さいこと

## 制約事項

- 既存の DynamoDB テーブルスキーマは変更しない（マイグレーション回避）
- `dynamodb_client.py:132` の UUID 上書きは、source_id を system-wide で一意化した後に削除する
- Claude CLI の timeout / 応答サイズ制限自体は変更できない（前提として設計する）
- `docs/product-requirements.md` の参照化は本スコープ外（別作業として分離）
- `MAX_REVIEW_LOOPS = 2` の変更はしない（課題 C は上限値ではなく破棄バグ）

## スコープ外

- プロダクト要求書（`docs/product-requirements.md`）をエージェント側に読み込ませる機能
- レビュー上限値の変更
- 完全な新機能（例: 出典 deep-link、出典キャッシュ層）
- F9 成果物履歴機能との統合（別途必要なら別作業）

## 参考実行記録

- 対象実行 ID: `exec-20260417142327-14040fe5`
- Slack channel: `C0ARFKTELS0`
- Notion URL: `https://www.notion.so/API-Gateway-Lambda-34547b55202e81bdb6d0cb2c6061cb7b`
- 調査根拠: DynamoDB `catch-expander-workflow-executions` / `workflow-steps` / `sources` / `deliverables` + CloudWatch Logs `/ecs/catch-expander-agent` （期間: 2026-04-17 14:23-15:17 UTC）
