# Frontend Profile View タスクリスト

承認後、上から順に実施。各タスクの完了条件 (DoD) を明示する。

---

## Phase 0: 事前検証 (~5 分)

### T0-1. JWT sub の実形式確認 (design.md §4.1 必須前提)
- **手段**:
  1. CloudFront URL にブラウザでログイン
  2. DevTools > Application > Cookies > `session` 値をコピー
  3. https://jwt.io 等で decode (HS256 → payload のみ確認、署名検証不要)
  4. `sub` フィールドの実形式を確認 (`U0XXXXXXX-T0XXXXXXX` か `U0XXXXXXX` 等)
- **DoD**: sub 実形式を steering 内に記録 (本ファイル §結果欄に追記)
- **派生決定**: 
  - 形式が `<user>-<team>` → 案 A 採用 (`sub.split("-")[0]`)
  - 形式が `U...` のみ → 抽出不要 (sub をそのまま user_id 使用)
  - その他 → 設計見直し、ユーザー再確認

### 結果欄 (T0-1 完了後に追記)
```
sub 実形式: <ここに記録>
採用方式: <案 A / 抽出不要 / 設計見直し>
```

---

## Phase 1: Backend Lambda (~1 時間)

### T1-1. ハンドラディレクトリ + __init__.py 作成
- `mkdir -p src/dashboard_api/get_my_profile`
- `touch src/dashboard_api/get_my_profile/__init__.py`
- **DoD**: 空ディレクトリと __init__.py が存在

### T1-2. `app.py` 実装
- 参考: `src/dashboard_api/get_token_monitor_health/app.py`
- 実装内容: design.md §2.2 のサンプル準拠
- `_extract_slack_user_id(user_sub: str) -> str | None` を内部ヘルパとして実装 (T0-1 結果に応じた分岐)
- **DoD**:
  - `python -c "from src.dashboard_api.get_my_profile import app; assert callable(app.lambda_handler)"` がエラーなく通る
  - design.md §4.3 エラーレスポンス契約と§2.2 ボディ形式を満たす

### T1-3. template.yaml に Lambda + Event + IAM 追加
- 配置場所: 既存 `DashboardGetTokenMonitorHealthFunction` (1187 行付近) 直後
- 内容: design.md §2.3 の YAML をそのまま追加
- 環境変数: `USER_PROFILES_TABLE`
- IAM: `dynamodb:GetItem` only (Resource: UserProfilesTable.Arn)
- **DoD**:
  - `sam validate --lint` がエラー 0
  - 追加行が既存 dashboard Lambda 群の命名規則と整合

### T1-4. Backend unit test 作成
- ファイル: `tests/unit/dashboard_api/test_get_my_profile.py` (新規)
- 既存 `tests/unit/dashboard_api/test_*.py` のパターンを踏襲 (moto / mock_dynamodb の利用)
- 6 ケース (design.md §5.1):
  1. 正常: 全フィールド設定済み
  2. 正常: レコード未存在 (新規ユーザー)
  3. 正常: 一部フィールド REMOVE 済み
  4. 異常: user_sub 欠落 → 401
  5. 異常: sub 形式異常で抽出失敗 → 400
  6. 異常: DDB raise → 500
- **DoD**: `uv run pytest tests/unit/dashboard_api/test_get_my_profile.py -v` で 6 件全 pass

### T1-5. Backend lint
- `uv run ruff check src/dashboard_api/get_my_profile/`
- `uv run ruff format --check src/dashboard_api/get_my_profile/`
- **DoD**: warnings/errors 0

---

## Phase 2: Frontend (~1 時間)

### T2-1. types.ts に `MyProfile` interface 追加
- ファイル: `frontend/src/api/types.ts`
- 内容: design.md §3.2
- **DoD**: TypeScript コンパイル成功 (`cd frontend && npx tsc --noEmit`)

### T2-2. endpoints.ts に `getMyProfile()` 追加
- ファイル: `frontend/src/api/endpoints.ts`
- 既存 endpoint (`getTokenMonitorHealth` 等) のパターンを踏襲
- リターン型: `Promise<MyProfile>` (data フィールド unwrap)
- **DoD**: TypeScript コンパイル成功

### T2-3. `MyProfile.tsx` 新規ページコンポーネント作成
- ファイル: `frontend/src/routes/MyProfile.tsx`
- 構成: design.md §3.3
- 既存ページ (`DashboardHome.tsx`) の loading/error/data 3 状態パターンを踏襲
- 6 軸セクション + learned_preferences + updated_at + ヘルプ banner
- 未設定フィールドは `text-muted-foreground` で「未設定」表示
- **DoD**:
  - TypeScript コンパイル成功
  - eslint warnings 既存 4 件以上に増えない (memory `project_2026-05-15-session-end` 参照)

### T2-4. App.tsx にルート追加
- ファイル: `frontend/src/App.tsx`
- 追加箇所: `<Route path="feedback" ... />` の次行
- 内容: `<Route path="profile" element={<MyProfile />} />`
- import 追加: `import { MyProfile } from "./routes/MyProfile"`
- **DoD**: TypeScript コンパイル成功

### T2-5. Layout.tsx Sidebar に「マイプロファイル」追加
- ファイル: `frontend/src/components/Layout.tsx`
- NAV 配列末尾に追加 (design.md §3.4)
- lucide-react import に `UserCircle` 追加 (既存 `User` は logout 用、競合しない)
- **DoD**:
  - TypeScript コンパイル成功
  - PC 表示 / モバイル表示両方で「マイプロファイル」リンクが見える

### T2-6. ローカル動作確認
- `cd frontend && npm run dev` で dev server 起動
- ブラウザで Sidebar 表示確認 + `/profile` 直接 URL アクセスで Login redirect 動作確認
- (API は backend 未デプロイなので 502/CORS エラーで OK、UI 構造のみ確認)
- **DoD**: dev server ログにコンパイルエラーなし、Sidebar に新リンク表示

---

## Phase 3: ローカル commit (~10 分)

### T3-1. 変更ファイル確認
- `git status --short` で以下を確認:
  ```
  ?? .steering/20260518-frontend-profile-view/
  ?? src/dashboard_api/get_my_profile/
  ?? tests/unit/dashboard_api/test_get_my_profile.py
  ?? frontend/src/routes/MyProfile.tsx
   M template.yaml
   M frontend/src/api/types.ts
   M frontend/src/api/endpoints.ts
   M frontend/src/App.tsx
   M frontend/src/components/Layout.tsx
  ```
- **DoD**: 想定外の M (Modified) がない

### T3-2. pre-commit-secret-scan Skill 起動
- ユーザーが「コミット」と発話 → Claude が自動起動
- 96 件の pre-existing 検出はベースラインとして許容、新規検出 0 を確認
- **DoD**: 新規シークレット検出 0、コミット承認

### T3-3. commit 作成
- メッセージ例:
  ```
  feat(dashboard): add /profile read-only view (F6)

  - Backend: DashboardGetMyProfileFunction Lambda + GET /api/v1/profile/me
    - GetItem only against UserProfilesTable
    - 6 axes + learned_preferences + updated_at
    - Empty record returns 200 with null fields (not 404)
  - Frontend: /profile route + Sidebar entry + read-only page
    - Edit guidance: "Slack で @CatchExpander profile"
    - Null fields display as "未設定"
  - Tests: 6 unit cases for get_my_profile handler

  Refs: .steering/20260518-frontend-profile-view/
  ```
- Co-Authored-By: 行を付ける (CLAUDE.md デフォルト)
- **DoD**: commit hash 取得、working tree clean (steering 含む)

---

## Phase 4: デプロイ (~20 分)

### T4-1. sam build
- `sam build` を repo root で実行
- **DoD**: ビルド成功、新 Lambda のコードがパッケージング対象に入る

### T4-2. sam deploy (dev 環境)
- **ユーザー明示承認** が前提 (CLAUDE.md §4 / memory `feedback_no_unsolicited_build_deploy_codex_required`)
- `sam deploy` 実行 (samconfig.toml の dev profile)
- **DoD**:
  - CloudFormation スタック更新成功
  - 新リソース: DashboardGetMyProfileFunction (Lambda) + 新 HttpApi route
  - 既存リソース変更なし (`No changes to deploy` か、新リソースのみの change set)

### T4-3. frontend ビルド + S3 sync
- `cd frontend && npm run build`
- `aws s3 sync dist/ s3://<frontend-bucket>/ --delete`
- CloudFront invalidation: `aws cloudfront create-invalidation --distribution-id <ID> --paths '/*'`
- 参考: memory `feedback_frontend_deploy_separate_from_sam`
- **DoD**: invalidation Completed まで確認

### T4-4. CloudFormation Outputs 検証
- `aws cloudformation describe-stacks` で新 Lambda の ARN / API GW URL を取得
- 期待値と整合確認
- **DoD**: 新 Lambda ARN が CloudFormation Outputs に存在

### T4-5. Lambda LastModified 確認
- `aws lambda get-function --function-name catch-expander-dashboard-get-my-profile --query 'Configuration.LastModified'`
- **DoD**: deploy 直後のタイムスタンプ

---

## Phase 5: 実機検証 (~20 分) [CLAUDE.md §1「検証の基準」必須]

### T5-1. 正常系: 自分のプロファイル (6 軸設定済み)
- CloudFront URL でログイン (まだなら)
- Sidebar 「マイプロファイル」クリック
- **期待**: 6 軸全部に Slack Modal で登録した内容が表示される、learned_preferences も表示
- **証拠保存**: 表示画面のスクショ or 主要フィールドの値テキスト

### T5-2. 部分未設定パターン
- Slack で `@CatchExpander profile` Modal を開き、1 軸 (例: background) だけ空欄保存
- frontend `/profile` をリロード
- **期待**: 該当軸が「未設定」グレーアウト表示、他軸は変化なし
- 確認後、元の値に戻す (Modal で再保存)

### T5-3. 未認証 redirect
- DevTools > Application > Cookies > `session` を削除
- `/profile` 直接アクセス
- **期待**: ログインページに redirect

### T5-4. CloudWatch Logs 確認
- ロググループ `/aws/lambda/catch-expander-dashboard-get-my-profile`
- 期待: T5-1 / T5-2 のリクエストで 200 ログ、ERROR/WARN なし
- **DoD**: 3 ケース全部期待通り

### T5-5. 検証結果まとめ
- 本ファイル末尾の「検証結果」セクション (下記) に証拠を追記

---

## Phase 6: push + Codex レビュー (~40 分) [CLAUDE.md §4「Codex レビューゲート」必須]

### T6-1. git push
- **ユーザー明示承認** 後に実行
- `git push origin main`
- **DoD**: GitHub に反映 (未 push commit が解消)

### T6-2. Codex レビュー prompt 作成
- ファイル: `.audit/2026-MM-DD_frontend-profile-view.prompt.md`
- 観点: design.md §4.1 (sub 抽出ロジック) / §4.2 (認可) / §4.4 (個人情報保護) を重点
- レビュー対象 commit hash を明記
- **DoD**: prompt ファイル作成、結果保存先 `.audit/2026-MM-DD_frontend-profile-view.md` も準備

### T6-3. ユーザーに Codex レビュー実行承認を依頼
- memory `feedback_codex_review_requires_approval` 準拠
- VS Code ターミナルで手動 `codex -c sandbox_mode="danger-full-access"` をユーザーに依頼
  (memory `feedback_codex_wsl2_sandbox` 参照: companion 経由は hang する)
- **DoD**: ユーザーから「進めて」承認取得

### T6-4. レビュー結果保存 + 指摘対応判断
- 出力を `.audit/2026-MM-DD_frontend-profile-view.md` に保存
- Critical / High があれば対応 → 修正 commit → T4 から再実行
- Medium / Low はトリアージ、対応判断はユーザーに確認
- **DoD**: Critical / High 指摘 0 (または対応済み)

### T6-5. (任意) 2nd pass レビュー
- 1st pass で修正があった場合、ユーザーに「2 pass 目を回すか」確認
- memory `2026-04-29_codex-iterative-review-finds-multilayer-misses` 準拠
- **DoD**: ユーザー判断による

---

## Phase 7: 完了処理

### T7-1. tasklist.md に検証結果追記
- 本ファイル末尾「検証結果」セクションに記入
- **DoD**: T5 の証拠 (URL, ログタイムスタンプ, 主要画面のスクショパス等)

### T7-2. memory 更新 (ハンドオフ skill 経由)
- `project_2026-MM-DD-session-end.md` 作成
- F6 閲覧 UX 完了を記録、関連 commit hash + ECS rev (本作業では変わらず) + Lambda 新規 ARN

### T7-3. ユーザー報告
- 完了サマリ提示
- 未対応の next steps (例: learned_preferences の構造化、admin 閲覧機能等) があれば列挙

---

## 検証結果 (実装完了後に追記)

### T0-1 検証結果 (2026-05-19 完了)
```
sub 実形式: 純粋 Slack user_id (例: U0XXXXXXXX、ハイフン区切りなし)
確認方法:   実機 session cookie を DevTools で取得 → jwt.io で decode
            payload に sub / name / exp の 3 フィールド、sub は U + 英数字のみ
採用方式:   抽出不要 (sub をそのまま user_id として使用)
            ただし将来の仕様変更に備えて防御的に split("-")[0] も併用 (実害ゼロ)
```

### T5 実機検証ログ
```
T5-1 正常系: <未実施>
T5-2 部分未設定: <未実施>
T5-3 未認証 redirect: <未実施>
T5-4 CloudWatch: <未実施>
```

### Codex レビュー結果
```
1st pass: <未実施>
2nd pass: <未実施 / 不要>
```

---

## 関連 memory / 参考

- 親 requirements / design: 本 steering ディレクトリ内
- 既存実装の参考:
  - `src/dashboard_api/get_token_monitor_health/` (Lambda 雛形)
  - `src/dashboard_api/authorizer/`, `auth_callback/`, `auth_me/` (JWT 認証)
  - `frontend/src/routes/DashboardHome.tsx` (ページ雛形)
- memory:
  - [[feedback_doc_approval_process]] — 各 Phase 跨ぎでユーザー承認を取得
  - [[feedback_pre_commit_secret_scan_skill]] — T3-2 必須
  - [[feedback_frontend_deploy_separate_from_sam]] — T4-3 必須
  - [[feedback_no_unsolicited_build_deploy_codex_required]] — T4-2 / T6-1 ユーザー承認待ち
  - [[feedback_codex_review_via_audit_dir]] — T6-2/4 ファイル配置
  - [[feedback_codex_review_requires_approval]] — T6-3 承認必須
  - [[feedback_codex_wsl2_sandbox]] — T6-3 companion 経由禁止
  - [[2026-04-29_codex-iterative-review-finds-multilayer-misses]] — T6-5 反復レビューの根拠
