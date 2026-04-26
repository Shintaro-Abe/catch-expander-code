# 要求定義: Review fix 反映の追検証

## 問題

`_run_review_loop` が reviewer の指摘を受けて生成成果物を修正したとき、その修正が **本当に Notion ページや GitHub コードに反映されているか実機で確認できていない**。

- 実装（M2: `_run_review_loop` を `tuple[dict, dict]` に変更し、修正後 `deliverables` を呼び出し元に rebind）は完了
- ユニットテスト（`test_run_review_loop_returns_fixed_deliverables_on_*` 系）も green
- しかし Phase 3 の検証 2 回（exec-20260418044855-00a18b82 / exec-20260418073748-2c27bb2f）はいずれも **review が初回 pass で fix loop 自体が走らなかった**
- → 「修正したつもりが反映されず古い deliverables が出力される」というサイレント不具合の可能性が残っている

## 影響

reviewer 指摘 → fix → 反映 のループが機能しないと、品質改善ループが事実上死ぬ。
失敗が静かに進行するため、ユーザー側からも気づきにくい。

## 目的

実機で fix loop が発火する execution が観測されたタイミングで、修正後 `deliverables` が確実に Notion / GitHub に反映されていることを 3 系統（DynamoDB / Notion / GitHub）で確認する。

## ユーザーストーリー

- 開発者として、reviewer が指摘を返すような execution が発生した際に、その修正が成果物（Notion ページ本文や GitHub コード）にきちんと反映されていることを確認したい。fix が無視され元の deliverables が出力されると、品質改善ループが事実上機能しないため

## 受け入れ条件

### AC-1（Must）fix loop 発火 execution の捕捉

reviewer が `passed=False` を返した execution を 1 件以上特定する:

- DynamoDB / CloudWatch から `Deliverables updated by review fix` または `Review failed at max loop` ログが出ている execution を見つける
- もしくは reviewer が指摘を返しやすいトピックを意図的に投入（例: 出典が乏しいニッチトピック / 最新の不安定情報）

### AC-2（Must）修正反映の確認

該当 execution について以下を確認:

- fix_prompt 実行前後の `deliverables` 差分を CloudWatch ログまたは DynamoDB `catch-expander-deliverables` から取得し、修正が適用されていることを確認
- Notion ページ本文に修正後の `content_blocks` が反映されている
- code_files が含まれる場合、GitHub にも修正後ファイルが push されている
- レビュアーが指摘した issue が修正版で解消されている（少なくとも 1 件）

### AC-3（Should）パースエラー時の安全性

fix_prompt の応答が parse_error の場合、`current_deliverables` は rebind されず元の deliverables のままとなることを実機で 1 件確認:

- CloudWatch ログに `Review fix returned parse error, keeping previous deliverables` が出ていること
- それでも Notion / GitHub には旧 deliverables（fix 前）が投入されていること

### AC-4（Should）N1 効果測定の判断材料

fix loop 発火 execution が複数蓄積されたら、`fix_prompt` の差分修正化（quality-fix の N1 タスク）の要否判断のため以下を集計:

- 平均 fix loop 実行時間
- fix_prompt のトークン消費量（CloudWatch から取得可能なら）

集計結果次第で N1 の要否を確定する。

## 制約事項

- 受動タスク（fix loop 発火を待つ）。意図的にトピックを選ぶ場合も自然な範囲で
- 観測のための仕掛け追加は最小限（既存ログで十分判断可能なはず）
- 確認後も M2 / N1 のロジックは変更しない（変更が必要なら別 steering を起票）

## 対象外

- M2 ロジックそのものの再設計
- fix_prompt の差分修正化（N1 のスコープ）
- 新しい reviewer 評価軸の追加

## トリガー条件

以下のいずれかが発生した時点で本 steering を再開する:

- CloudWatch に `Deliverables updated by review fix` ログが新規 execution で出現
- CloudWatch に `Review failed at max loop` ログが新規 execution で出現
- ユーザーが意図的に fix loop を発火させやすいトピックで投入

## リスクと対処

| リスク | 対処 |
|--------|------|
| fix loop が長期間発火しない | 受動扱いとする。3 ヶ月以上発火しなければ意図的トピック投入で 1 件取得 |
| 観測時点で M2 関連コードが変更されている | 観測実行時点の git SHA を steering に記録し、その時点の挙動として評価 |
| fix loop 発火しても Notion / GitHub 投入前に別エラー | 別エラー優先で対処、AC-2 は次の発火を待つ |
