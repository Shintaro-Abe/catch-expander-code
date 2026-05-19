# Frontend Profile View 要求定義

## 1. 目的・背景

F6 (User Profile) は Slack Modal 経由で登録/編集/削除が完了している (commit `20392bf`)。しかし、ユーザーは「自分が今どのような 6 軸プロファイルを設定済みか」を確認する手段がない。Slack Modal は再オープンしないと中身が見えず、誤った設定に気付きにくい。

本作業では、frontend ダッシュボードに **マイプロファイル閲覧サブページ (`/profile`)** を追加し、設定済みプロファイル全文を read-only で確認できる UX を提供する。

## 2. スコープ

### 2.1 In Scope

- frontend に新規サブページ `/profile` を追加
  - Sidebar に「マイプロファイル」リンクを追加 (`ダッシュボード / 実行履歴 / レビュー品質 / エラー / フィードバック / マイプロファイル`)
  - 6 軸 (role / interests / expertise / learning_goals / background / output_preferences) を read-only 表示
  - `learned_preferences` (検出済み学習履歴) を一覧表示
  - 未設定時はプレースホルダー + Slack 誘導文 (「`@CatchExpander profile` で設定してください」)
- backend に閲覧専用 API を追加
  - 新規 Lambda `dashboard-get-my-profile`
  - 新規エンドポイント `GET /api/v1/profile/me` (既存 `/api/v1` 階下、HttpApi 認可)
  - JWT から `user_id` を取得し UserProfilesTable から `GetItem` のみ実行
- 既存 dashboard_api と同じ認証フロー (Cognito Authorizer 経由 JWT) を流用

### 2.2 Out of Scope

- **編集 UI は frontend に作らない** (Slack Modal 1 系統で完結)
  - 画面冒頭に「編集は Slack で `@CatchExpander profile`」と案内のみ
- 他ユーザーのプロファイル閲覧 (admin 機能含む)
- learned_preferences の編集 / リセット機能
- プロファイル変更履歴 (audit log) の表示

## 3. 受け入れ条件 (Acceptance Criteria)

### AC-1: API
- `GET /api/v1/profile/me` が JWT 認証付きで動作
- JWT から取り出した `user_id` で UserProfilesTable を `GetItem`
- レスポンスボディ:
  ```json
  {
    "user_id": "U0XXXXXXX",
    "role": "...",
    "interests": "...",
    "expertise": "...",
    "learning_goals": "...",
    "background": "...",
    "output_preferences": "...",
    "learned_preferences": [...],
    "updated_at": "2026-05-18T..."
  }
  ```
- レコード未存在時は HTTP 200 で `{"user_id": "...", "role": null, ..., "learned_preferences": [], "updated_at": null}` を返す (404 ではない)
- 未設定フィールド (REMOVE 済み) は値 `null` で返す

### AC-2: Frontend ページ
- `/profile` ルートが Layout 配下に存在
- Sidebar に「マイプロファイル」リンクが追加され、ハイライト動作も他リンクと同等
- ページ最上部に「編集は Slack で `@CatchExpander profile` を実行してください」のヘルプ文を表示
- 6 軸の各セクションは以下の表示ルール:
  - 値あり: 全文 (500 字まで) を改行保持で表示
  - 値 null: 「未設定」グレーアウト表示
- learned_preferences は配列の要素を一覧表示 (空配列なら「学習履歴はまだありません」)
- API 失敗時はエラーメッセージ表示 (既存ページと同等の UX)

### AC-3: 認可
- 未ログイン状態で `/profile` を開くと既存ガードによりログインページへリダイレクト
- JWT に含まれる `user_id` 以外の profile は取得不可能 (query 等で他 user_id を指定する経路を作らない)

### AC-4: 既存挙動への非干渉
- Slack Modal 登録/編集/削除フロー (commit `20392bf`, `97b033e`, `73c9a56`, `c8f5e97` で確定) は一切変更しない
- 既存 dashboard ページ (`/dashboard`, `/executions`, ...) のレンダリング/データに影響しない
- TriggerFunction の IAM (UserProfilesTable GetItem/UpdateItem) は触らない

### AC-5: 検証 (実機)
- dev デプロイ後、ユーザーが CloudFront URL でログイン → `/profile` に遷移 → 既設定プロファイル全文表示まで確認
- 未設定状態 (新規 user_id) を模擬し、プレースホルダー表示を確認 (テスト user で UserProfilesTable レコードなし状態を作る or 一時的に Modal で全空欄保存)
- CloudWatch Logs で `dashboard-get-my-profile` の正常起動を確認
- `git push` 後 Codex レビュー 1 pass を回し、Critical/High 指摘ゼロを確認

## 4. 制約事項

### 4.1 セキュリティ・プライバシ
- profile 内容は **個人情報を含む** (役割・職業・背景 = 30 代後半 / 子育て中 等のサンプル経歴で実証済み)
- API は JWT 必須、他ユーザー閲覧経路を作らない
- frontend は同一ブラウザセッション内でのみ閲覧

### 4.2 コスト
- 新規 Lambda: 1 関数 (cold start <1s 想定、月数百リクエストで $0)
- API GW: 既存 HttpApi に 1 ルート追加、月 100 リクエスト程度で $0
- DynamoDB: GetItem のみ、追加月額ほぼゼロ
- 合計追加月額: **~$0** (memory `feedback_logging_waf_cost_rejected` 同様の個人利用前提)

### 4.3 既存パターンとの整合
- backend handler 配置: `src/dashboard_api/get_my_profile/` (既存 `get_token_monitor_health/` 等と並列)
- frontend ルート: `frontend/src/routes/MyProfile.tsx` (PascalCase ファイル名で既存と統一)
- API クライアント: `frontend/src/api/` に既存パターン (TokenMonitorHealth 型と同様) で型定義 + fetch 関数を追加

### 4.4 期間
- 1 セッション (steering 3 文書 + 実装 + 実機検証 + Codex レビュー 1 pass) で完結予定
- 中規模、Slack Modal 実装時 (`20260515-user-profile-modal/`) より工数小さい想定

## 5. ユーザーストーリー

> プロジェクトオーナー (個人利用) として、
> 自分が設定した 5W1H 6 軸プロファイルが現在どのような内容で AI に伝わっているかを CloudFront ダッシュボード上で確認したい。
> なぜなら、Slack Modal を再オープンしないと中身が見えず、誤設定や古い情報が混ざっていることに気付きにくいから。
> 編集は引き続き Slack Modal で行うので、frontend は read-only でよい。

## 6. 関連 memory / commit

- 親 steering: `.steering/20260515-user-profile-modal/` (Modal 登録/編集/削除)
- 関連 commit:
  - `20392bf` feat(trigger): implement user profile registration via Slack Modal (F6)
  - `97b033e` ephemeral 化 + value:None no-op + 最小権限
  - `73c9a56` isinstance 全層 + SlackApiError 捕捉
  - `c8f5e97` value:null は REMOVE 扱い
- memory:
  - [[project_2026-05-16-session-end]] §「確定した将来 steering 提案」
  - [[project_2026-05-15-session-end]] §「次セッション開始時の自然な action」
  - [[feedback_frontend_deploy_separate_from_sam]] (実装時の frontend デプロイ手順)
  - [[feedback_no_unsolicited_build_deploy_codex_required]] (実装後の Codex レビューゲート)
