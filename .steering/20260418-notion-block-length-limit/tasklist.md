# タスクリスト: Notion ブロック長制限対応

## 方針

- 単一 commit で実装＋テスト＋ドキュメント追記を集約
- push 後 1 回の Slack 投入で AC-2 を確認
- 同 execution で関連 steering（Followup-A AC-3 / Followup-B AC-1）も同時観測

## フェーズ 1: 実装

### T1: 定数とヘルパー関数追加

- [x] **T1-1** `src/agent/storage/notion_client.py` に `_NOTION_RICH_TEXT_MAX_CHARS = 2000` 定数を追加
- [x] **T1-2** `_split_long_rich_text(blocks: list[dict]) -> list[dict]` をモジュールレベルに追加
- [x] **T1-3** 入力 list / dict を mutate しない実装（新 list / 新 dict を返す）
- [x] **T1-4** `block[block["type"]]["rich_text"]` を動的アクセス。type に対応するキーが無い / 不正構造はスキップして堅牢化
- [x] **T1-5** rich_text の各 element について `text.content` が 2000 char 超なら 2000 char ごとに分割。`type` / `annotations` / `link` 等の他フィールドを各チャンクにコピー

### T2: 適用箇所への組み込み

- [x] **T2-1** `create_page` 入口で `content_blocks = _split_long_rich_text(content_blocks)` を実行
- [x] **T2-2** `append_blocks` 入口で `blocks = _split_long_rich_text(blocks)` を実行
- [x] **T2-3** `_request_with_retry` のシグネチャ無変更を確認

## フェーズ 2: テスト

### T3: ユニットテスト追加

既存 `tests/unit/agent/test_notion_client.py` に `TestSplitLongRichText` クラスとして追加（既存テストの集約場所に追従。設計時想定パス `tests/unit/agent/storage/` は未使用のため流用）。

- [x] **T3-1** `test_split_short_rich_text_unchanged` — 2000 char 以下はそのまま
- [x] **T3-2** `test_split_exactly_2000_chars_unchanged` — 境界値（ちょうど 2000）
- [x] **T3-3** `test_split_over_2000_code_block` — code 3921 char が 2000+1921 に分割される
- [x] **T3-4** `test_split_preserves_language_and_annotations` — `code.language` 等のメタ保持
- [x] **T3-5** `test_split_preserves_other_rich_text_fields` — `type` / `annotations` / `link` 等が分割後も保持
- [x] **T3-6** `test_split_handles_multiple_blocks` — 複数ブロック中の長尺 1 件のみ分割
- [x] **T3-7** `test_split_handles_block_without_rich_text` — divider など rich_text が無い block は無変更
- [x] **T3-8** `test_split_handles_unknown_type_safely` — 未知 type / 不正構造でも例外を出さずスキップ
- [x] **T3-9** `test_split_does_not_mutate_input` — 入力 list / dict が変更されない

### T4: テスト全件パス確認

- [x] **T4-1** `pytest tests/ -q` で全件パス（200 passed: 既存 191 + 新規 9）

## フェーズ 3: コミット & デプロイ

- [x] **C1** 単一 commit を作成（`2e62d25`: 実装 + テスト）
- [x] **C2** `git push origin main` で GitHub Actions build-agent.yml 経由デプロイ済み
- [x] **C3** デプロイ完了確認

## フェーズ 4: 実機検証

### V1: AC-2 確認

- [x] **V1-1** Slack で「API GatewayとLambdaの組み合わせについて」を投入（exec-20260418144822-33a2a5ab）
- [x] **V1-2** CloudWatch ログから `Notion API client error.*status=400` が出ていないことを確認
- [x] **V1-3** `Notion page created` が出ていることを確認
- [x] **V1-4** DynamoDB `catch-expander-workflow-executions` の status=`completed`
- [x] **V1-5** DynamoDB `catch-expander-deliverables` の storage=`notion+github`
- [x] **V1-6** Notion ページを目視確認済み（長尺 code ブロックが自然表示）

### V2: 関連 steering 同時観測（任意）

- [ ] **V2-1** Followup-A AC-3（storage=notion+github 2 連続）達成のため、別トピックで 1 回追加投入（**進捗 1/2**: 14:48 で 1 件達成）
- [ ] **V2-2** Followup-B AC-1（fix loop 発火）が出れば、Followup-B の検証フローへ移行（受動観測継続中）

## 完了条件

- T1〜T4 全達成
- C1〜C3 全達成
- V1-1〜V1-6 全達成（AC-2 確認）
- requirements.md AC-1（Must）/ AC-2（Must）が全て充足
- AC-4（テスト）達成
- AC-3（既存 100-chunk ロジック干渉なし）は設計時点で確認済み（design.md 参照）

## リスクと対処

| リスク | 対処 |
|--------|------|
| Notion 側の文字数カウントが Python `len()` と異なる | V1-2 で再度 400 が出たら `_NOTION_RICH_TEXT_MAX_CHARS` を 1900 に下げる（1 行変更）|
| V1 で長尺コードが再現しない | 「Terraform で AWS API Gateway + Lambda + DynamoDB + IAM の包括的な構成例を示して」のようにより大規模な構成を要求するトピックで再試行 |
| サロゲートペア境界での分割で文字化け | 観測されたら codepoint 境界保護を追加（別 commit）|
| 既存テストパターン（pytest fixtures）が無い | `tests/unit/agent/storage/` 配下を新規作成 |
