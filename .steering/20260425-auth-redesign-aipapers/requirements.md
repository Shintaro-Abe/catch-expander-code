# 要求定義: 認証方式の再設計（ai-papers-digest 方式の採用）

作成日: 2026-04-25
ステータス: ドラフト（承認待ち）

---

## 1. 背景

### 1.1 現状の問題

Catch-Expander の Claude OAuth トークン管理は次の運用上の問題を抱えている。

1. **CloudShell + iPhone 経由の手動再認証手順が複雑かつ動作実績が伴わない**
   - `.steering/20260419-cloudshell-iphone-auth/procedure.md` の手順は「sync 成功」と「OAuth refresh が起きた」を区別できず、スタール状態で「OK」を返す欠陥がある
   - 実機検証（2026-04-23 22:48 JST）で、`sync-claude.sh` が「OK: synced」を返したにもかかわらず、Secrets Manager の `expiresAt` は 69.95 時間過去のままで、Slack 投入が 4 件連続で 401 失敗した
2. **Claude Code 2.1.118 で `~/.claude/.credentials.json` が CloudShell では生成されない**
   - 2026-04-23 24:00 ごろ判明。CloudShell で `/login` 成功直後でも `~/.claude/.credentials.json` は作成されず、認証情報の保存先が変更された可能性が高い
   - 既存 `sync-claude.sh` は旧パスを前提にしており、根本的に動作不能
3. **再認証が「人間の手動操作」に依存しており、運用負荷が常時かかる**
   - トークンは約 8 時間で失効する可能性があり、現状の運用は「失効通知 → ユーザーが iPhone で認証 → CloudShell で sync」のループが必要
4. **token_monitor は失効を検知するだけで延命しない**
   - `src/token_monitor/handler.py` は `expiresAt` を読んで Slack 通知するのみ。実際のトークン更新ロジックを持たない

### 1.2 参考実装

ユーザー本人が運用中の `Shintaro-Abe/ai-papers-digest` リポジトリでは、以下の方式で **完全自動の Claude OAuth 自動リフレッシュ** を実現している。

- **`token_refresher` Lambda（`src/lambdas/token_refresher/handler.py`）**: `https://platform.claude.com/v1/oauth/token` を `refresh_token` で直接叩き、新 access_token を Secrets Manager に書き戻す
- **Fargate `entrypoint.sh`**: 起動時に Secrets Manager → `~/.claude/.credentials.json` を生成し、`claude auth status` で CLI 経由 refresh を念のため誘発、変化があれば Secrets Manager に書き戻す
- **初回認証は 1 回だけ手動**: ローカル PC で `claude` 認証 → `~/.claude/.credentials.json` を Secrets Manager に投入

この方式は **CloudShell を使わない**ため、現状の Catch-Expander 運用問題の大半を構造的に解消できる。

---

## 2. 解決方針

ai-papers-digest 方式を Catch-Expander に移植する。

### 2.1 採用する仕組み

1. **Token Refresher Lambda（既存 `token_monitor` を進化させる）**
   - EventBridge で定期実行（既存 12 時間間隔は維持）
   - Secrets Manager から credentials を読み、`expiresAt` を確認
   - **失効間近 or 失効済みなら refresh_token で OAuth エンドポイントを叩いて延命**
   - 延命に成功したら Secrets Manager 上書き
   - 延命に失敗（refresh_token も無効など）した場合のみ Slack へアラート（=最終手段）
2. **ECS Task entrypoint の書き戻し追加（`src/agent/main.py`）**
   - 既存 `_setup_claude_credentials` は維持（Secrets → `~/.claude/.credentials.json`）
   - **タスク実行終了時、`~/.claude/.credentials.json` の内容と起動時値を比較し、変化があれば Secrets Manager に書き戻す**（Fargate 内で CLI が refresh した場合の救済）
3. **初回 bootstrap 手順の簡素化**
   - ローカル PC（Mac/Windows のクライアント環境）で 1 回 `claude` 認証
   - `~/.claude/.credentials.json` を `aws secretsmanager put-secret-value --secret-string file://~/.claude/.credentials.json` で投入
   - 以降は手動操作ゼロ（リフレッシュ失敗時のみ手動）
4. **退役対象**
   - `.devcontainer/sync_claude_token.sh`
   - `.devcontainer/watch_claude_token.sh`
   - `.steering/20260419-cloudshell-iphone-auth/`（履歴として残すが、procedure.md には「廃止された手順」を明記）

### 2.2 採用しない選択肢（参考）

- **`ANTHROPIC_API_KEY` への切り替え**: Claude Max プランの定額枠が使えなくなり、API 課金が発生する。コスト面で却下
- **OAuth client_secret 方式**: Claude Code CLI の OAuth はパブリッククライアント設計で client_secret 方式は提供されていない

---

## 3. ユーザーストーリー

### US-1: 運用者として、トークン失効による業務中断を経験したくない

- 受け入れ条件:
  - Token Refresher Lambda が 12 時間ごとに Secrets Manager 上のトークンを確認し、失効間近（残り 1 時間以下）または失効済みでも refresh_token が有効ならば自動延命する
  - 延命結果が Secrets Manager に書き戻され、次の ECS 起動時に新 access_token が使われる

### US-2: 運用者として、初回セットアップは 1 回で済ませたい

- 受け入れ条件:
  - 新しい手順書 `.steering/20260425-auth-redesign-aipapers/initial-setup.md` に「ローカル PC で 1 回 `claude` 認証 → `aws secretsmanager put-secret-value --secret-string file://~/.claude/.credentials.json`」の流れが明記されている
  - 手順書通りに実行すれば、以降の運用では再認証が不要（refresh が連続失敗した場合のみ再実行）

### US-3: 運用者として、リフレッシュが本当に効いているかを検証可能にしたい

- 受け入れ条件:
  - Token Refresher Lambda の各実行で、`refreshed=true/false`、新 `expiresAt`、所要時間が CloudWatch Logs に出力される
  - 失敗時は失敗理由（HTTP コード、`refresh_token` 不在等）が構造化ログとして出る
  - 単体テストで「stale → refresh → 新 expiresAt が `now + 1h` 以上未来になる」フローが検証されている

### US-4: 運用者として、refresh が完全に失敗したときは確実に通知を受けたい

- 受け入れ条件:
  - refresh_token が revoke されている等で OAuth エンドポイントが 4xx を返した場合、Slack に「自動延命に失敗。再認証が必要」通知が飛ぶ
  - 通知文には execution_id 相当の Lambda RequestId、最終 expiresAt、HTTP エラーレスポンスの要約が含まれる

### US-5: ECS タスク内で refresh が走った場合も Secrets Manager と整合させたい

- 受け入れ条件:
  - ECS タスク終了時、`~/.claude/.credentials.json` を起動時値と比較し、変化があれば Secrets Manager へ書き戻す
  - 書き戻しに失敗してもタスク本体の実行結果（成功/失敗）には影響しない（ベストエフォート）

---

## 4. 機能要件

### F1. Token Refresher Lambda（既存 `token_monitor` を進化）

- F1.1 EventBridge から 12 時間間隔で起動（既存スケジュールを継承）
- F1.2 Secrets Manager から `claudeAiOauth` 構造を読み出し
- F1.3 `expiresAt - now < 1 時間` または既に失効済みなら refresh を試みる
- F1.4 refresh は `https://platform.claude.com/v1/oauth/token` への POST（grant_type=refresh_token）
- F1.5 成功時は新 access_token / refresh_token / expiresAt を Secrets Manager へ put_secret_value
- F1.6 refresh が HTTP 4xx/5xx で失敗した場合は Slack に通知、Lambda は failure を返す（CloudWatch Alarm でも検知可能にする）
- F1.7 トークンがまだ十分有効（残り 1 時間超）の場合は何もせず INFO ログのみ出力
- F1.8 構造化ログ: `refreshed`, `expires_in_minutes`, `error_class` を出力

### F2. ECS Task の credentials 書き戻し

- F2.1 `src/agent/main.py` に「起動時の credentials 内容」を保持する処理を追加
- F2.2 タスク終了直前（success/failure 問わず）に `~/.claude/.credentials.json` を読み、起動時値と比較
- F2.3 差分があれば Secrets Manager に put_secret_value
- F2.4 書き戻しエラーは WARN ログのみ（タスク終了コードに影響させない）

### F3. 初回 bootstrap 手順書

- F3.1 `.steering/20260425-auth-redesign-aipapers/initial-setup.md` を新規作成
- F3.2 ローカル PC で 1 回 `claude` 認証する手順
- F3.3 `aws secretsmanager put-secret-value --secret-string file://~/.claude/.credentials.json` での投入手順
- F3.4 セキュリティ注意（投入後はローカルファイル削除推奨、シェル履歴サニタイズ）
- F3.5 動作確認手順（Token Refresher を手動 invoke → CloudWatch Logs 確認 → ECS 起動テスト）

### F4. 退役・廃止対応

- F4.1 `.devcontainer/sync_claude_token.sh` の削除
- F4.2 `.devcontainer/watch_claude_token.sh` の削除
- F4.3 `.steering/20260419-cloudshell-iphone-auth/procedure.md` 冒頭に「**この手順は 2026-04-25 に廃止されました。新しい手順は `.steering/20260425-auth-redesign-aipapers/initial-setup.md` を参照**」を追記
- F4.4 `docs/architecture.md` / `docs/functional-design.md` の認証関連記述を新方式に更新

---

## 5. 非機能要件

### NF1. セキュリティ

- NF1.1 IAM 最小権限: `secretsmanager:GetSecretValue` および `PutSecretValue` を Token Refresher Lambda role と ECS task role の **2 つだけ** に許可
- NF1.2 Lambda の VPC 配置と egress 制限は **段階的に検討**（初期実装ではパブリック egress を許容、運用安定後に VPC エンドポイント or NAT GW + SG 制限を追加）
- NF1.3 トークン値が CloudWatch Logs に出力されないこと（accessToken / refreshToken の文字列を一切ログ出力しない）
- NF1.4 ECS タスクの `enable_execute_command` は本番で false（既に false なら現状維持）

### NF2. 可観測性

- NF2.1 Token Refresher Lambda の各実行は構造化ログ（refreshed フラグ、所要時間、エラー時の HTTP code）を出す
- NF2.2 ECS タスクの credentials 書き戻し成否も構造化ログ
- NF2.3 必要に応じて CloudWatch Alarm を将来追加（本タスクのスコープ外、別 steering で扱う）

### NF3. テスト

- NF3.1 Token Refresher Lambda の単体テストは pytest で実装、既存 `tests/unit/token_monitor/` 配下に追加
- NF3.2 OAuth エンドポイントは httpserver / urllib mock でフェイクして、ネットワーク呼び出しを行わない
- NF3.3 ECS 書き戻しロジックの単体テストは `tests/unit/agent/test_main.py` に追加

### NF4. デプロイ

- NF4.1 SAM template の Token Monitor を Token Refresher にリネーム or 機能拡張（既存リソース名を変更しないことで既存 EventBridge スケジュールを温存）
- NF4.2 IAM role に `PutSecretValue` を追加（既存は GetSecretValue のみのはず）

---

## 6. 制約事項

### C1. 公開 OAuth client_id を使用する

ai-papers-digest と同じく `CLIENT_ID = "9d1c250a-e61b-44d9-88ed-5944d1962f5e"`（Claude Code CLI 公式の公開クライアント ID）を使う。これは Anthropic 側仕様で、将来仕様変更があれば壊れる可能性がある（許容リスク）。

### C2. 初回 bootstrap はローカル PC（macOS/Windows）が前提

CloudShell では Claude Code 2.1.118 が `~/.claude/.credentials.json` を生成しないことが判明済み。ローカル PC で 1 回認証して JSON を取り出す運用を前提とする。ローカル PC が用意できないユーザー向けの代替手順は **本タスクのスコープ外**。

### C3. refresh_token のローテーション挙動

Anthropic 側が refresh のたびに新 refresh_token を返すかどうかは仕様非公開。実装では「レスポンスに `refresh_token` があればそれを使う、無ければ既存を流用」とする（ai-papers-digest と同じ挙動）。

### C4. 既存の Secrets Manager 内の格納形式を維持

現状 `claudeAiOauth.expiresAt` 等の構造で格納されている前提。新方式でもこの構造を維持し、互換性を壊さない。

---

## 7. スコープ外

- **CloudWatch Alarm の自動構築**: 本タスクは Lambda + ECS のロジック更新に集中。Alarm は別 steering
- **VPC 配置 + egress 制限**: NF1.2 の通り、運用安定後に別 steering で扱う
- **Anthropic API Key 直接利用への切替**: コスト面で却下（要求の段階で除外）
- **Bootstrap 自動化**: 初回認証の自動化（例: GitHub Actions で自動）は仕様上不可能（OAuth 同意が人間操作必須）。手動手順書のみ整備
- **既存 CloudShell 手順の deep dive 修正**: 廃止対応のみ（procedure.md に廃止注記）

---

## 8. 受け入れ条件（マスタ）

以下のすべてが満たされたとき本タスクを完了とする。

- [ ] Token Refresher Lambda が refresh_token で延命を実行し、Secrets Manager を更新できる
- [ ] Lambda 単体テストが pytest で全件パスする
- [ ] ECS タスク終了時の credentials 書き戻しが実装され、`tests/unit/agent/test_main.py` で検証されている
- [ ] 初回 bootstrap 手順書が `.steering/20260425-auth-redesign-aipapers/initial-setup.md` に存在する
- [ ] 廃止対応（sync_claude_token.sh 削除、procedure.md 廃止注記）が実施されている
- [ ] `docs/architecture.md` / `docs/functional-design.md` の認証関連記述が更新されている
- [ ] 実機で「Secrets Manager 内のトークンを意図的に stale 状態にする → Lambda 手動 invoke → 延命成功 → Secrets Manager の expiresAt が更新される」を確認
- [ ] 実機で「ECS タスクを起動 → orchestrator が正常完了する」を確認
- [ ] main へマージ済み

---

## 9. 想定される質問・懸念（事前検討）

### Q1. 既存の `token_monitor` の機能（Slack 通知）は残すのか？

A: 残す。**自動延命に失敗した場合の最終手段**として Slack 通知を維持する。具体的には、refresh_token が revoke 済み or OAuth サーバーが恒常エラーを返すケース。

### Q2. ローカル PC の Claude Code バージョンが 2.1.118 でも `~/.claude/.credentials.json` は生成されるのか？

A: ai-papers-digest が運用実績で動いていることから、**ローカル macOS/Windows では生成される**と推定する。Bootstrap 手順実行時に検証ステップを設け、生成されない場合は手順書に代替パス（`~/.claude.json` 内のフィールド抽出など）を記載する余地を残す。これは F3 のタスクで現物確認する。

### Q3. Lambda が VPC 外で OAuth エンドポイントを叩くのはセキュリティ的に許容か？

A: 初期実装では許容。NF1.2 の通り、運用安定後に VPC + egress 制限を追加する。理由: 構成複雑化を段階的に進めるため、また現状の CloudShell 運用も同等のネットワークモデル（パブリック egress）であり、セキュリティ後退ではない。
