# 要求内容: deliverables への GitHub URL 永続化

## 背景

### システム全体での `deliverables` テーブルの位置づけ

Catch-Expander のワークフロー処理は次の経路で実行される (`docs/functional-design.md` 第 1 節 + 第 7 節)。

```
[Slack] @Catch-Expander トピック投稿
   ↓
[Lambda trigger] 署名検証 → DynamoDB workflow-executions に "received" を作成 → ECS RunTask
   ↓
[ECS Orchestrator] Notion へ本文 (テキスト成果物) を投稿
                 → 成果物に code_files が含まれていれば GitHub へ push
   ↓
[Orchestrator → DynamoDB deliverables] 1 execution あたり 1 件の deliverable レコードを保存
   ↓
[Orchestrator → Slack] 完了通知 (Notion URL + GitHub URL を同時掲載)
```

`deliverables` テーブルは以下の用途で参照される永続化レイヤである。

| 用途 | 参照元 | 取得するフィールド | 出力先 |
|---|---|---|---|
| F9 履歴コマンド (`履歴` / `history`) | `src/trigger/app.py:84-93` (`_get_deliverable_url`) | `external_url` | Slack スレッド |
| 品質メタデータの後追い分析 | 直接 query (運用) | `quality_metadata` | 人間 |
| 将来の検索 / ダッシュボード UI | 未実装 | 未定 | 未定 |

### 現在の保存仕様

`src/agent/orchestrator.py:640-655` の put_item ペイロード:

```python
deliverable = {
    "execution_id": execution_id,
    "deliverable_id": f"dlv-{execution_id}",
    "type": "all",
    "storage": "notion" if not github_url else "notion+github",
    "external_url": notion_url,                 # ← 常に Notion URL のみ
    "quality_metadata": {...},
    "created_at": now_iso,
}
```

`storage` フィールドは `"notion"` または `"notion+github"` の 2 値で「どこに保存したか」を表現する。一方 `external_url` は単数形・Notion URL ハードコードであり、コード成果物の所在 (GitHub URL) はテーブル上にどこにも保存されない。

### GitHub URL の現在のライフサイクル

| フェーズ | 状態 |
|---|---|
| 生成時 | `github_client.push_files()` の戻り値で URL を保持 (`https://github.com/{repo}/tree/main/{dir}`) |
| Slack 完了通知 | `slack.post_completion()` が Notion URL と GitHub URL を併記して投稿 |
| **永続化** | **どのテーブルにも保存されない** |
| 二次参照 | Slack のメッセージ本文を遡って人間が目視するしかない |

### 直前の関連変更 (2026-04-29)

本日、成果物 push 先を `Shintaro-Abe/catch-expander-code` から `Shintaro-Abe/catch-expander-deliverables` (専用リポジトリ) に切り替えた。`template.yaml` の `GitHubRepo` Default 値更新と CloudFormation `update-stack` で完了済み。これにより GitHub URL のリポジトリ部分が安定したため、永続化に踏み切る前提条件が整った。

### 関連スキーマ (`docs/functional-design.md` 第 5 節)

`deliverables` テーブルの現行定義は以下のフィールドを持つ。

| フィールド | 型 | 既存 / 追加 |
|---|---|---|
| `execution_id` | String (PK) | 既存 |
| `deliverable_id` | String (SK) | 既存 |
| `type` | String | 既存 |
| `storage` | String (`notion` / `notion+github`) | 既存 |
| `external_url` | String (Notion URL) | 既存 |
| `quality_metadata` | Map | 既存 |
| `created_at` | String (ISO 8601) | 既存 |
| **`github_url`** | **String (任意)** | **本変更で追加** |

## 課題

### 1. データ消失リスク (利用者視点)

GitHub URL の唯一の入口が Slack メッセージ本文である現状では、以下の事象でアクセス手段が **完全に失われる**。

- **Slack のメッセージリテンション**: 無料プランは 90 日でメッセージが閲覧不可、Pro プランでも検索が困難になる
- **チャンネルの削除 / archive**: 設定値 `SLACK_NOTIFICATION_CHANNEL_ID` (現状 `C0ARFKTELS0`) が将来移行された場合、過去の完了通知が辿れなくなる
- **ワークスペース移行**: 別ワークスペースへの引っ越しで履歴が引き継がれない
- **目視検索負荷**: 投稿数が積み重なると、特定トピックの GitHub 成果物を探すのに長時間かかる

コード成果物は Notion 本文と独立した価値 (commit 履歴、code search、`git clone` での取り込み、永続的な共有 URL) を持つため、Notion ページから手動で辿る運用では代替できない。

### 2. データモデルの内部矛盾

`storage="notion+github"` という複合値を持ちうるのに `external_url` は単数形で Notion URL しか入らない。これは次の意味を持つ。

- スキーマ (フィールド構造) が業務意図 (2 系統の保存先) を表現できていない
- 新機能を `deliverables` テーブルから組み立てるたびに、設計者は「`external_url` は Notion 固定」という暗黙ルールを思い出す必要がある
- 将来 storage targets が増えたとき (例: `s3`、`notion+github+s3`) に同じ拡張作業を繰り返す
- データ整合性の自動検証が書きづらい (`storage` と `external_url` の関係が形式的に表現できない)

### 3. F9 履歴コマンドの片面性

`docs/functional-design.md` 第 7 節 F9 で定義された履歴コマンドは `deliverables.external_url` から URL を解決する設計のため、構造上 GitHub URL を返せない。今後この成果物テーブルを起点にする新機能 (例: 検索 UI、Slack スラッシュコマンド、外部 API、Notion フィルタとの突き合わせ) はすべて同じ穴を引き継ぐ。

### 4. 修復コストの非対称性 (今やる vs 後でやる)

放置すると、過去成果物の GitHub URL を後から補完するのは事実上不可能になる。

| アプローチ | 実現可能性 | コスト |
|---|---|---|
| 過去 Slack メッセージのスクレイピング | △ (リトライ重複・書式変更・削除済みで欠落) | 高 |
| 過去成果物を再生成して上書き | × (LLM の非決定性で同一結果にならない) | 過大 |
| GitHub リポジトリ側のディレクトリ名から逆算して `execution_id` に紐付け | △ (タイムスタンプ部分でしか一致できず精度低) | 中 |
| **新規レコードに対する保存 (本対応)** | ○ | **低 (put_item に 1 行、F9 表示に数行)** |

つまり「今やれば 1 行追加で済むが、後でやるとほぼ不可能」という非対称性がある。

### 5. ドキュメントとの不整合

`docs/functional-design.md` 第 5 節の `deliverables` テーブル定義は現状を反映しているが、第 7 節 F9 の説明では Slack 出力に「Notion / GitHub の URL を併記」と記述しており、現実の F9 出力 (Notion のみ) と齟齬がある可能性が高い。本変更でこの齟齬も同時に解消する必要がある。

## 目的

コード成果物の GitHub ディレクトリ URL を `deliverables` テーブルに永続化し、Slack メッセージの寿命に依存せず利用者が再到達できるようにする。あわせて F9 履歴コマンドおよび関連ドキュメントを更新し、データモデル・実装・仕様の三者を整合させる。

## ユーザーストーリー

- **U1**: 私 (利用者) は過去に生成したコード成果物の GitHub リンクを、Slack の履歴を漁らずに `履歴` コマンドだけで取り戻したい。
- **U2**: 私 (利用者) はテキスト読み物 (Notion) とコード (GitHub) を別々のリンクで素早く開きたい。
- **U3**: 私 (運用者) は将来 `deliverables` テーブルを起点に検索 UI や分析ジョブを作るとき、保存先ごとの URL が構造化された状態で参照できることを期待する。

## 受け入れ条件

- [ ] AC1: `storage="notion+github"` の新規 deliverable レコードに、push 先の GitHub ディレクトリ URL が `github_url` フィールドとして保存されている。
- [ ] AC2: F9 履歴コマンド (`履歴` / `history`) の応答に、コード成果物がある場合の GitHub リンクが Notion リンクと並んで表示される。
- [ ] AC3: コード成果物がないケース (`storage="notion"` のみ) では従来通り Notion URL のみが表示され、レイアウト・絵文字・改行構造が崩れない。
- [ ] AC4: 既存の (本変更前に書かれた) deliverable レコードは `github_url` フィールド未存在として扱われ、F9 履歴コマンドは Notion リンクのみを表示する (`KeyError` 等で落ちない)。
- [ ] AC5: ユニットテスト (`tests/unit/agent/test_orchestrator.py`, `tests/unit/trigger/test_app.py`) が新フィールドの保存と表示・後方互換挙動を検証する。
- [ ] AC6: `docs/functional-design.md` 第 5 節 (`deliverables` スキーマ表) と第 7 節 F9 (Slack 出力例) が本変更後の挙動と一致している。

## 制約事項

- DynamoDB はスキーマレスのため、新フィールド追加はマイグレーション不要。既存レコードへの遡及書き込みは行わない (実現困難かつ実装コスト過大)。
- `deliverables` テーブルの主キー (`execution_id` / `deliverable_id`) と GSI 構成は変更しない。
- 命名は既存の `external_url` と整合させ、追加フィールドは `github_url` とする (オプショナル / 単一値)。
- F9 履歴コマンドの Slack 出力レイアウト変更は最小限に留め、既存 5 件表示の構造を維持する。
- 本対応はソースコード変更 + ドキュメント更新で完結し、CloudFormation Stack の再デプロイは不要 (テーブルスキーマ変更を伴わない)。
- 既存テストは壊さない。新規テストは既存のフィクスチャ・モック方式 (`tests/unit/trigger/conftest.py` 等) に従う。

## 非対応 (スコープ外)

- 既存レコードへの GitHub URL 事後補完 (上記「修復コストの非対称性」の通り、実装コスト過大)。
- Notion ページ本文への GitHub URL 自動埋め込み (本要求の主目的ではない)。
- GitHub のリポジトリ配下を全文検索する UI / コマンドの新設。
- `storage` 値のスキーマ拡張 (`s3` 等の新ターゲット追加) — 別件で扱う。
- F9 のページネーション・ソート機能の拡張 — 別件で扱う。
