# 要求定義: Notion ブロック長制限対応

## 問題

Notion API は 1 個の `rich_text[N].text.content` を **2000 文字以下** に制限している。
generator が生成する `content_blocks` 内の長尺要素（特に `code` ブロック）がこの制限を超えると、Notion `pages` API が 400 を返してページ作成自体が失敗する。

実機観測:

- execution: 2026-04-18 09:01:37 UTC
- Notion API レスポンス: `body.children[78].code.rich_text[0].text.content.length should be ≤ 2000, instead was 3921`
- 影響: ページ作成失敗 → 成果物が Notion にも GitHub にも残らない（task=failed）

## 影響

- ユーザーが Slack で投入したリクエストが「成果物を格納中...」直後に汎用エラーで返る
- 後続の Followup-A AC-3（`storage="notion+github"` 2 連続成功）と Followup-B AC-2（fix 反映 3 系統確認）の検証もブロック
- Slack の汎用エラー文言が「OAuth 切れの可能性」を案内するため、ユーザーから見ると root cause が誤認される

## 目的

長尺テキストを含む `content_blocks` を Notion の制限内に収まる形へ正規化してから送信し、ページ作成失敗を解消する。

## ユーザーストーリー

- 開発者として、generator が長尺コードや本文を含む `content_blocks` を返した場合でも、Notion API 制限に引っかかってページ作成が失敗しないようにしたい。失敗すると成果物がどこにも残らず、修正ループや GitHub 投入も全て無駄になるため

## 受け入れ条件

### AC-1（Must）長尺ブロックの分割正規化

`content_blocks` 内の各ブロックについて、`rich_text[N].text.content` が 2000 文字を超える場合は **同一ブロック内の rich_text 配列を 2000 文字ごとに分割** して投稿できる形に変換する。

- 対象ブロックタイプ: `code` / `paragraph` / `heading_*` / `bulleted_list_item` / `numbered_list_item` / `quote` / `callout`（rich_text を含む全タイプ）
- 分割は文字数ベース（行境界に揃える等の凝った処理は不要）
- 分割対象が無い場合は元の構造を維持

### AC-2（Must）Notion API 投入成功

修正適用後、AC-1 で分割が必要な execution を 1 件以上実行し、Notion API 400 が出ずにページ作成が成功することを確認する:

- CloudWatch ログに `Notion API client error | ... status=400` が出ない
- DynamoDB `catch-expander-workflow-executions` の status=`completed`
- Notion ページ本体に分割された code ブロックが正しく表示される（人手目視 1 件）

### AC-3（Should）ブロック数上限考慮

Notion API はリクエスト 1 回あたりの children 数も上限がある（100 ブロック）。既存実装は 100 ずつチャンク投入しているが、AC-1 の分割によりブロック総数が増える可能性は無い（同一ブロック内の rich_text 配列のみ増える）ため、既存ロジックとの干渉は無いはず。これを設計時に確認する。

### AC-4（Should）ユニットテスト

`_split_long_rich_text(content_blocks)` 等のヘルパー関数を追加し、以下のケースをテスト:

- 制限内（≤2000 char）の content は分割されない
- 制限超過の content は 2000 char チャンクに分割される
- 分割後も block type / language（code 用）等のメタは保持される
- rich_text 配列に複数要素が既にある場合も正しく動作

### AC-5（Should）Slack エラー文言の見直し（対象外候補）

Slack の汎用エラー文言が「OAuth 切れ」を案内する仕様は本件と分離可能なため、本 steering の対象外とする。別 steering で扱う。

## 制約事項

- `notion_client.py` の API 呼び出しシグネチャは変更しない（呼び出し元への影響を避ける）
- 分割ロジックは `notion_client.py` 境界（投入直前）で実施し、generator や orchestrator 側のロジックは変更しない
- 文字数判定は Python の `len(str)`（UTF-8 codepoint 数）で行い、Notion 側の数え方と多少差異があっても 2000 を確実に下回るよう余裕を持たせる（例: 1900 char チャンクとする選択も検討）

## 対象外

- generator プロンプトの「コードを短くする」指示追加（根本解決にならない / プロンプト依存性増加）
- Notion 側の他制限（タイトル長 2000 / URL 長 2000 / リクエストサイズ等）の網羅的対応（観測されてから個別対応）
- Slack 汎用エラー文言の改善（別 steering）
- code_files の GitHub 側格納（既に成功している）

## トリガー条件

本 steering は即時実装対象（観測待ちタイプではない）。実装後の検証フェーズで Slack 投入を 1〜2 回実施する。

## リスクと対処

| リスク | 対処 |
|--------|------|
| 分割により Notion ページの可読性が下がる | code ブロックは分割されても連続表示されるため実害は小さい。テキスト本文の分割は段落境界が崩れる可能性があるが、現状 2000 char 超のテキストブロックは観測されていないので code 限定でも可（設計時判断）|
| 2000 char 制限が将来変わる | 定数化（`_NOTION_RICH_TEXT_MAX_CHARS = 2000`）し変更容易にする |
| 既存の 100 件 chunk 投入ロジックと干渉 | 分割は同一ブロック内 rich_text 配列を増やすのみで block 総数は変わらないため干渉なし（design.md で再確認）|
