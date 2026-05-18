# スラッシュコマンド使用方法

本プロジェクトで利用可能なカスタム Skill（スラッシュコマンド）の使用方法を記載します。
全 Skill は `.claude/skills/<name>/SKILL.md` に SKILL.md として配置されており、
`.claude/` は `.gitignore` 対象のため **DevContainer ローカルのみ** に存在します。

---

## `/handoff` — セッション終端メモ生成

### いつ使うか
- セッションを終わる直前
- トークン消費が高くなって会話を区切りたいとき
- 「次回再開時に何から始めるか」を残したいとき

### 起動方法
- `/handoff` と入力
- 「ハンドオフして」「終わる」「セッション終了」「次のセッションへ」のいずれかを発言

### 何をしてくれるか
1. 並列で git ログ / status / ECS revision / Lambda 最終更新 / pytest collection を取得
2. `/home/vscode/.claude/projects/-workspaces-Catch-Expander/memory/project_YYYY-MM-DD-session-end.md` を生成・更新
3. CLAUDE.md §3 必須 4 項目（完了作業 / 現在状態 / 次候補タスク / ブロッカー）で構造化
4. `MEMORY.md` インデックスに 1 行追記
5. 内容をユーザーに見せて承認を待つ

### 出力例
```
✅ ハンドオフメモを更新しました:
  - /home/vscode/.claude/projects/-workspaces-Catch-Expander/memory/project_2026-05-17-session-end.md
  - /home/vscode/.claude/projects/-workspaces-Catch-Expander/memory/MEMORY.md (1 行追加)

次回再開ポイント:
  - フェーズ 5: ... を実装
  - HEAD は 724062a (CLAUDE.md 作業規律追加)
```

---

## `/codex-review` — Codex レビューゲート

### いつ使うか
- `git push` を完了した直後
- コード変更を伴うコミットがあって、外部 LLM（Codex）の独立レビューを挟みたいとき

### 起動方法
- `/codex-review` と入力
- 「Codex レビュー回して」「コーデックスでレビュー」と発言

### 何をしてくれるか
1. トピックスラッグと対象コミット範囲を確認（明示されていなければ質問）
2. `git diff` を読んで 3 観点（構造妥当性 / エッジケース / 再発バグ領域）でレビュー要件を抽出
3. `.audit/YYYY-MM-DD_<topic-slug>.prompt.md` に Codex 用 prompt を生成
4. `.audit/YYYY-MM-DD_<topic-slug>.md` を空ヘッダで作成
5. **そこで停止し、ユーザーに以下を実行してもらうよう依頼**:
   ```bash
   codex -c sandbox_mode="danger-full-access" exec --skip-git-repo-check \
     "$(cat .audit/2026-05-17_<topic>.prompt.md)"
   ```
6. 結果を受け取ったら指摘の High/Medium/Low ごとに修正案提示
7. 「2 pass 目を回すか」を必ず確認

### 重要な制約
- **Claude は `codex` コマンドを勝手に実行しない**（WSL2 sandbox 制約）
- レビュー指摘ゼロが確認できるまで `sam build` / `sam deploy` に進まない

---

## `/deploy-verify` — デプロイ実機検証

### いつ使うか
- `sam deploy` 完了直後
- 「デプロイしたけど本当に動いてる？」を機械的に確認したいとき
- `image SHA が反映されているか` を CFN/ECS で確認したいとき

### 起動方法
- `/deploy-verify` と入力
- 「デプロイ検証」「verify deploy」と発言

### 何をしてくれるか
1. 検証対象（ECS task / Lambda 名 / フロント SPA）と期待される変更内容を確認
2. CloudFormation スタック status を確認（`UPDATE_COMPLETE` か）
3. 対象別に artifact 反映を検証:
   - **ECS**: `aws ecs describe-services` で task definition revision、起動中タスクの image SHA
   - **Lambda**: `aws lambda get-function` で LastModified / CodeSha256
   - **フロント**: S3 bundle 最終更新時刻 + CloudFront invalidation status
4. CloudWatch Logs を直近 5 分 tail（CloudWatch MCP 優先、なければ `aws logs tail`）
5. CLAUDE.md §1「報告フォーマット」3 セクションで結果出力:
   - **受け入れ基準**: 何が verified されるべきか
   - **各基準の根拠**: コマンド / ログ / URL の実観測
   - **未検証で推測している項目**: verified に含めない部分

### 重要な制約
- **検証で artifact 不一致を発見しても、自動で `sam deploy` を再実行しない**
  - 例: ECS task の image SHA が期待値と違う、Lambda LastModified が古いまま 等
  - 自動で `sam deploy --parameter-overrides AgentImageUri=<sha>` を叩く誘惑が生まれるが、必ずユーザーに報告して明示承認を待つ
- **`sam build` / `sam deploy` は Claude が自動実行できない**（CLAUDE.md §4）
  - PreToolUse hook `.claude/hooks/sam-deploy-block.sh` で機械的にブロックされる
  - Claude はコマンドを案内するのみ、実行はユーザー自身が VS Code ターミナルで行う
  - 他の sam サブコマンド (`sam local`, `sam logs`, `sam validate` 等) は hook 対象外
- 「verified」と書くのは観測した事実がある場合のみ

---

## 3 Skill の連携フロー

典型的な開発サイクルでの呼び出し順序:

```
コード変更
  ↓
git commit  ← pre-commit-secret-scan が hook 経由で自動実行
  ↓
git push
  ↓
/codex-review     ← Codex に独立レビューを依頼、指摘修正
  ↓
（ユーザー承認）
  ↓
sam deploy
  ↓
/deploy-verify    ← CFN/ECS/Lambda + CloudWatch で実機検証
  ↓
（必要なら）
  ↓
/handoff          ← セッション終端、次回再開ポイント記録
```

---

## 共通事項

- 全 3 Skill は `.claude/skills/<name>/SKILL.md` に SKILL.md として配置済み
- `.claude/` は `.gitignore` 対象なので **DevContainer ローカルのみ** に存在（他マシンに持っていく場合は手動コピー or gitignore 例外化が必要）
- 起動失敗時は `/help` でスラッシュコマンドが正しく認識されているか確認

## 関連ドキュメント

- `CLAUDE.md` §1〜§4「作業規律」セクション — 各 Skill の根拠ルール
- `.claude/skills/<name>/SKILL.md` — 各 Skill の手順詳細
- `.claude/hooks/` — git commit 前の secret scan / Edit 後の ruff check
