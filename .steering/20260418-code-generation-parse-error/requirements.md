# 要求定義: コード成果物生成の失敗修正

> **⚠️ 注記 (2026-04-26 追加): 採用した方針は後に撤回**
>
> このステアリングで採用した B-2（スキーマ正規化 `_normalize_code_files_payload`）は、その後 4 回の再発を経て対症療法であったと判断されました。
> 2026-04-25 に `.steering/20260425-code-gen-redesign-filesystem/` にて、コード成果物生成は **JSON パースを完全に放棄** し、Claude に Write ツール経由でサンドボックスへファイルを直接書き出させる方式（`call_claude_with_workspace`）に再設計されました。
> 本 steering で追加した `_normalize_code_files_payload` / `_build_code_failure_diagnostics` 等は撤去済みです（commit `1eba566`）。
> 関連ナレッジ: `feedback_unified_interface_anti_pattern.md`（性質的差異の大きい成果物への統一 JSON 契約は構造的に壊れる）/ `feedback_repeated_fix_threshold.md`（同じ問題の 2 回再発でゼロベース見直し）。
> 本ファイルは履歴として保持されます。

## 背景

`.steering/20260418-quality-fix/` の Phase 3 検証（exec-20260418044855-00a18b82）で AC-2「storage = notion+github」が未達となった。

実機ログから判明した事実:

- M3 のタイプ別コード生成ループは設計通り `iac_code` / `program_code` の両方を順に呼んだ
  （CloudWatch: `INFO Generating code files for type` 1 回, `WARNING Code generation failed for type` 2 回）
- 両タイプとも `_build_code_generation_prompt` → `call_claude` → `_parse_claude_response` を経た結果、
  `code_result["files"]` が空 or 非 dict と判定され `code_files_merged` に何も追加されなかった
- 結果 `deliverables["code_files"]` がセットされず、後段の Storage 判定で `"github"` が外れて
  最終 `storage = "notion"` のみになった
- `Claude CLI error` / `Failed to parse Claude response as JSON` の警告は出ていない
  → CLI 自体は成功し、JSON パースも成立している可能性が高い
  （= `files` キー欠損または空の構造化応答が返っている）

しかし、現状のロガー設定（`format="%(asctime)s %(name)s %(levelname)s %(message)s"`）では
`logger.warning(..., extra={...})` の `extra` が CloudWatch に出力されないため、以下が判別できない:

- 実際に `parse_error` が立っていたのか（戦略 1〜4 が全失敗したのか）
- `parse_error=False` で `files` キー欠損だったのか
- Claude 応答の本文がどんな形をしていたのか
- 応答サイズ（M3 の前提が成立していたか）

## 目的

1. コード生成失敗の root cause を観測可能にする（diagnostics 不足の解消）
2. 観測結果を踏まえて生成ロジックまたはプロンプトを修正し、`storage = "notion+github"` を恒常的に達成する

## ユーザーストーリー

- 運用者として、コード生成が失敗したとき CloudWatch 1 回の検索で「なぜ失敗したか（parse error / files 欠損 / 空 / 応答形式違い）」と「Claude 応答の冒頭」を読めるようにしたい。失敗の root cause 特定で deploy → 投入 → 観測のサイクルを毎回繰り返さずに済むようにしたい
- ユーザーとして、Slack で IaC / プログラムコードの生成を依頼したとき、storage = "notion+github" が安定して達成され、GitHub リポジトリにファイルが push されることを期待する

## 受け入れ条件

### AC-1（Must）診断情報の充実

`Code generation failed for type` の warning に最低以下を含むこと:

- `code_type`（既存）
- `parse_error` フラグ（既存・extra 化されているが出力されない問題を解消）
- `files_kind`: 取得した `files` の型名（`dict` / `list` / `NoneType` / `missing` 等）
- `files_count`: dict なら要素数、それ以外は `0` または `null`
- `top_level_keys`: パース結果が dict のときのトップレベルキー一覧（最大 10 件）
- `response_preview`: Claude 応答テキストの先頭 500 文字
- `response_chars`: 応答テキストの文字数

実現手段は問わないが、CloudWatch でメッセージ本文だけで判別できる形で出力されること
（ロガーの formatter 改修 / メッセージ文字列に直接埋め込み / JSON 形式の構造化ログ化 など）。

### AC-2（Must）root cause に応じた本対応

AC-1 で取得した情報をもとに以下のいずれかの本対応を実施:

- root cause が**プロンプト由来**（schema 認識ズレ等）→ `_build_code_generation_prompt` / generator.md の出力形式定義を修正
- root cause が**応答サイズ超過**（戦略 4 で部分パースされ files 欠損）→ プロンプト分割粒度の見直しまたは max_tokens 調整
- root cause が**files 構造の表記揺れ**（`code_files.files` ネスト等）→ `_parse_claude_response` 直後の正規化レイヤを追加

### AC-3（Must）実機検証

修正後に同一トピックで再投入し、以下を確認:

- DynamoDB `workflow-executions.storage = "notion+github"`
- GitHub リポジトリに新 execution 向けディレクトリが作成される
- 少なくとも 1 ファイル以上の IaC / プログラムコードが push される
- CloudWatch に `Code generation failed for type` warning が出ない
  （または部分失敗のみで、もう一方のタイプは成功している）

### AC-4（Should）回帰テスト

ユニットテストに以下を追加:

- 観測項目（AC-1）が正しく出力されることのテスト
- root cause 別のテストケース（`files` 欠損 / 空 dict / list 化 / parse_error）

### AC-5（Should）ドキュメント更新

`docs/architecture.md` または `docs/development-guidelines.md` の該当箇所に
コード生成パスのログ仕様を 1 段落追記する（運用者が参照する場所を明示）。

## 制約事項

- M1〜M3 / S1〜S2 の修正は破壊しない（173 tests pass を維持）
- 公開リポ方針に従い `.claude/` `.devcontainer/` を変更しない
- `gen_prompt`（テキスト成果物）側は触らない（M3 で分離済みのため対象外）
- `requirements.md` の `requirements.md` AC-2 が未達のため、本 steering 完了をもって
  quality-fix の AC-2 を再評価し充足とする

## 対象外

- 新しい code_type の追加（要求の対象は iac_code / program_code のみ）
- review loop 周りの diagnostics（Followup-B のスコープ）
- N1（fix_prompt 差分化）

## リスクの先取り

| リスク | 想定対処 |
|--------|----------|
| 観測強化のみで本対応にたどり着かない | 観測 → 1 回の実機投入 → root cause 確定 → 修正の手順を tasklist で明示し、観測結果次第で design.md を再ドラフト |
| 応答サイズが本当に大きい場合、タイプを更に細分化する必要 | tasklist の本対応フェーズで「分割粒度の段階的引き下げ」をオプションとして用意 |
| 修正後も別の execution で再現する間欠的な失敗 | AC-3 の検証は 2 回以上の再現確認を推奨（tasklist で明文化）|
