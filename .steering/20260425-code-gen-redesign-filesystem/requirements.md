# 要求定義: コード生成のファイルシステム書き込み方式への再設計

作成日: 2026-04-25
ステータス: ドラフト（承認待ち）

---

## 1. 背景

### 1.1 観測された問題

2026-04-25 21:00 JST、Slack から「AWSのCloud Front」を投入。CloudWatch Logs `/ecs/catch-expander-agent` の task `c9797a76` で以下を観測:

```
11:33:10 INFO Generating code files for type
11:39:59 WARNING Failed to parse Claude response as JSON, returning as text
11:39:59 WARNING Code generation failed for type | code_type=iac_code parse_error=True
                                                   files_kind=missing files_count=0
11:47:34 INFO Deliverables updated by review fix
...
12:00:03 INFO Workflow completed
```

`code_files` が空のままワークフローが完了し、GitHub への push がスキップされた。**Notion 投稿は成功**しているため、ワークフロー自体は完走しているが、**コード成果物の永続化だけが失敗**している。

### 1.2 過去の対応履歴（同じ問題に対する繰り返し）

`git log src/agent/orchestrator.py` を辿ると、コード生成の JSON パース問題に対し過去 5 回の修正が入っている:

| commit | 内容 | 効果 |
|---|---|---|
| `ed148f6` | Robust JSON parsing for agent responses | パーサー頑健化 |
| `d8589e7` | Separate code generation call to prevent large-response parse failures | レスポンス分割 |
| `d951ecc` | split code generation by deliverable type | iac/program 分離 |
| `5e77dd3` | normalize code generation payload | files キー無し救済 |
| `f0b121a` | surface code generation failure diagnostics | 診断ログ追加 |
| `73458e3` | preserve code_files across review-fix loop | review-fix で code_files 保持 |

**全て「壊れた JSON をどうにかパースしようとする」アプローチ。今回また再発。**

### 1.3 根本原因の分析

過去 5 回の修正が局所的な対症療法に終わった理由を、今回ゼロベース分析した結果:

1. **初期設計（2026-04-05 `.steering/20260405-initial-implementation/design.md`）の合理的判断**:
   - 全 step を `call_claude(prompt) → JSON` という統一インターフェースで統一
   - リサーチャー結果・ワークフロー設計・トピック解析・レビュー結果は **短い構造化データ** なので JSON で十分
   - Python 側で結果を統合・整形して後続処理に流すために、構造化データが必要
   - **ここまでは正しい設計**

2. **想定が外れた点**:
   - 初期 design.md には **「ファイル数は最大5ファイル」「実装の骨格として提供」** と記載 → 小さいコード成果物を想定
   - 実運用では 1 ファイルあたり数千文字（Terraform の WAF ルール等）、内容に JSON エスケープ衝突文字（`\"`、`${var}`、`\\n`、ヒアドキュメント等）が密集
   - 結果: JSON 文字列値の中に **数万文字の HCL/Python コード**を詰めることになり、LLM が JSON エスケープを完璧にやり続けることが確率的に困難に

3. **エージェント出力の性質的差異**:

| 出力 | サイズ | 特殊文字密度 | JSON 適性 |
|---|---|---|---|
| トピック解析 | ~500 字 | 低 | ◯ |
| ワークフロー設計 | ~1,000 字 | 低 | ◯ |
| リサーチャー要約 | ~2,000 字 | 中 | ◯ |
| レビュアー合否 | ~1,000 字 | 低 | ◯ |
| **コード成果物** | **10,000+ 字** | **高（HCL/Python 構文）** | **✗** |

コード成果物だけが質的に異なるのに、**統一性を優先して同じ JSON 経路に押し込んだ**ことが根本原因。

### 1.4 解決方針

**コード成果物だけを例外として扱う**。Claude Code CLI 本来の機能である **Write ツールでファイルシステムに直接書かせる**方式に変更する。

エージェントは Claude CLI の終了後に出力ディレクトリを scan して `code_files` dict を構築。**JSON エスケープ層が消えるため、過去 5 回繰り返した parse_error が構造的に発生し得ない**設計とする。

---

## 2. 解決方針の詳細

### 2.1 採用する仕組み（案 A）

1. **コード生成プロンプトの変更**: 「JSON で返せ」ではなく「`/tmp/agent-output/<code_type>/` 配下に Write ツールでファイルを書け」と指示
2. **Claude CLI の `--allowedTools Write Edit` を有効化**: コード生成時のみ
3. **作業ディレクトリの sandbox 化**: `subprocess.run(cwd=output_dir)` で cwd 制限。さらにプロンプトで絶対パス指定を禁止
4. **終了後の scan**: `output_dir` を walk して `{相対パス: 内容}` の dict を構築
5. **失敗判定**: ファイル数 0、空ファイルのみ、想定外拡張子のみ等を明示的に検知して構造化ログ
6. **テキスト成果物パスは無変更**: コードを含まない投入（時事問題、トレンド調査等）への影響をゼロにする

### 2.2 採用しない選択肢（参考）

- **B: 1 ファイル 1 リクエスト（テキスト返し）**: 実装は容易だが API 呼び出し N 倍、コスト増
- **C: XML タグ区切り**: 実装小だが、エスケープ問題が完全には消えない。ファイルシステム書き込みより本質的でない
- **D: 現状維持で対症療法を続ける**: 過去 5 回が証明している通り再発する。却下

---

## 3. ユーザーストーリー

### US-1: 運用者として、IT 関連トピック（コード成果物あり）を投入したとき、JSON エスケープ問題で GitHub プッシュが失敗してほしくない

- 受け入れ条件:
  - 「AWS の CloudFront」「Lambda 入門」等の IT トピックを Slack 投入したとき、`code_files` が空にならず GitHub にディレクトリが push される
  - 過去の parse_error の発生条件（数千文字の HCL コード、エスケープ衝突文字）が再現しても新方式で動作する

### US-2: 運用者として、コードを含まないトピック（時事問題、トレンド調査等）を投入したとき、本改修の影響を受けたくない

- 受け入れ条件:
  - コード成果物を含まないトピック投入時、本改修によるリグレッションが発生しない
  - テキスト成果物（content_blocks, summary）の生成・Notion 投稿は従来通り動作する

### US-3: 運用者として、コード生成が失敗したときに何が起きたかログから判別できるようにしたい

- 受け入れ条件:
  - Claude が Write ツールを呼ばずに終了した場合、構造化ログ（`reason: "no_files_written"` 等）で検知できる
  - 出力ディレクトリ scan の結果（ファイル数、各ファイルのサイズ）が構造化ログとして残る
  - Slack 通知の有無・内容が現行の挙動を維持する

### US-4: 運用者として、新方式が安全に動作することを実機検証で確認できるようにしたい

- 受け入れ条件:
  - ローカル単体テストで「Claude CLI が Write を呼ぶケース」「呼ばないケース」「絶対パスに書こうとするケース」をモックでカバー
  - 実機（ECS Fargate）でコード成果物を含む投入を 1 回行い、`code_files` が GitHub に push されることを確認

---

## 4. 機能要件

### F1. コード生成プロンプトの書き換え

- F1.1 `_build_code_generation_prompt`（`src/agent/orchestrator.py:272`）を書き換え
- F1.2 「`./` 配下に Write ツールでファイルを書け（絶対パス禁止、相対パスのみ）」を明示
- F1.3 「ファイル一覧と簡潔な README をテキストで返してよい」（任意、終了確認用）
- F1.4 ファイル数の制約（最大 5）は維持

### F2. Claude CLI 呼び出しの拡張

- F2.1 コード生成専用の Claude CLI 呼び出しヘルパーを新設（または `call_claude` を拡張）
- F2.2 `--allowedTools` に `Write,Edit` を追加（コード生成時のみ）
- F2.3 `subprocess.run(cwd=<sandbox_dir>)` で cwd を sandbox 化
- F2.4 sandbox ディレクトリは ECS タスクの `/tmp/` 配下に動的作成（`tempfile.mkdtemp`）
- F2.5 タスク終了時に sandbox を確実に削除

### F3. 出力ファイルの収集

- F3.1 `os.walk(sandbox_dir)` で全ファイルを列挙
- F3.2 想定拡張子（`.tf`, `.py`, `.ts` 等、既存 `_FILE_EXTENSIONS` 流用）または既知ファイル名（`Dockerfile`, `Makefile` 等）のみ採用
- F3.3 想定外パスへの書き込みは構造化ログに記録した上で破棄
- F3.4 ファイル内容を読み取り `{相対パス: 内容}` dict を構築
- F3.5 既存の `_normalize_code_files_payload` の役割は不要になるため、コード生成パスからは除去（テキスト成果物パスでは不要なので何もしない）

### F4. 失敗判定とエラーハンドリング

- F4.1 ファイル数 0 の場合、構造化ログ `reason: "no_files_written"` を残し、`code_files` を空のままにする（既存の merge ロジックは流用）
- F4.2 全ファイルが空（0 バイト）の場合、`reason: "all_files_empty"` で失敗扱い
- F4.3 想定外拡張子のファイルしかない場合、`reason: "no_recognized_files"` で失敗扱い
- F4.4 想定外パス（絶対パス、`../` を含む等）が検出された場合、当該ファイルは破棄しつつ警告ログ
- F4.5 Claude CLI が non-zero exit code で終わった場合、現行の retry ロジックを維持

### F5. 構造化ログ

- F5.1 コード生成開始時: `code_type`, `sandbox_dir`
- F5.2 コード生成終了時: `files_written_count`, `files_kind: "valid"|"all_empty"|"no_recognized"|"none"`, `files_total_bytes`
- F5.3 ファイル収集時: 各ファイルの `path`, `size_bytes`（先頭 100 文字程度のプレビューは含めない=ログ量抑制）
- F5.4 想定外パス検出時: `rejected_path`, `rejection_reason`

### F6. テキスト成果物パスの不変性

- F6.1 ジェネレーター呼び出し（`call_claude(generator_prompt + ...)` の最初の呼び出し）は変更しない
- F6.2 テキスト成果物（content_blocks, summary）の `_parse_claude_response` 経由のパースも変更しない
- F6.3 レビューループも変更しない（4/23 の `73458e3` で修正済みの code_files 保護も維持）

### F7. テスト

- F7.1 コード生成専用ヘルパーの単体テスト（tmp_path で sandbox を再現、Write が呼ばれた/呼ばれないケース）
- F7.2 ファイル収集ロジックの単体テスト（想定拡張子、空ファイル、絶対パス、`../` 含むパス等）
- F7.3 失敗判定の単体テスト（F4.1〜F4.4 の各ケース）
- F7.4 既存テストの非回帰確認（テキスト成果物パスを含む全 229 件）

---

## 5. 非機能要件

### NF1. 影響範囲の最小化

- NF1.1 コード生成パス（`if code_types and "github" in storage_targets:` ブロックの内側）のみを変更する
- NF1.2 テキスト成果物パス・トピック解析・ワークフロー設計・リサーチャー・レビュアーは無変更

### NF2. セキュリティ

- NF2.1 sandbox ディレクトリは `/tmp/agent-output-<uuid>/` 形式で実行ごとに分離。タスク終了時に削除
- NF2.2 Claude が絶対パス指定で書いた場合は破棄（書かれた事実は記録）
- NF2.3 `--allowedTools Write,Edit` は **コード生成時のみ** 付与。テキスト成果物・リサーチャー・レビュアー・WF 設計の Claude CLI 呼び出しでは付与しない
- NF2.4 ファイルサイズ上限（例: 1 ファイル 100 KB）を超えるファイルは破棄＋警告（DoS 抑止）

### NF3. 観測性

- NF3.1 失敗時に「何が起きたか」を CloudWatch Logs で完全に判別可能にする（F5）
- NF3.2 既存のメトリクス・ダッシュボードは無変更（既存の DynamoDB 状態管理を維持）

### NF4. ai-papers-digest との設計差異の明示

- NF4.1 ai-papers-digest は要約タスクのみで JSON 一本で済むため統一設計を維持できる
- NF4.2 Catch-Expander はコード成果物の性質が違うため、コード生成だけ例外設計を採用することを `docs/architecture.md` に明記
- NF4.3 「将来統一性のために再び JSON 方式に戻したい」誘惑が起きたとき、過去の失敗が思い出せるよう、本 steering へのリンクを `docs/architecture.md` に残す

---

## 6. 制約事項

### C1. Claude Code CLI の Write ツール挙動への依存

Claude Code CLI 2.1.x の `--allowedTools Write` が期待通り動作することを前提とする。バージョン依存の挙動変化があれば本設計が壊れる可能性は許容リスクとする（CLI 自体が動かなければ全機能停止するため、本変更固有のリスクではない）。

### C2. 出力先は ECS タスクのファイルシステム（エフェメラル）

ECS Fargate のローカルファイルシステムを使用する。EFS 等の永続ストレージは使用しない（タスク終了時に消える前提なので問題なし）。

### C3. テキスト成果物の JSON 方式は維持

レビュー対象が混乱するため、本タスクではテキスト成果物の JSON 方式は変更しない。将来的にテキストもファイルシステム方式にする選択肢は別 steering で扱う（本タスクのスコープ外）。

### C4. 既存の `_FILE_EXTENSIONS` / `_FILENAME_EXACT` 定数を流用

ホワイトリストの定義は既存値を踏襲し、本タスクでは拡張しない。新拡張子追加が必要なら別 steering。

---

## 7. スコープ外

- **テキスト成果物のファイルシステム化**: 本タスクではコード成果物のみが対象
- **レビューループの再設計**: 4/23 の code_files 保護で十分動作中。本タスクでは触らない
- **ai-papers-digest 側の設計変更**: あちらは要約タスクのみで JSON 適性あり。変更不要
- **ECS タスクの cwd / sandbox 全体の見直し**: コード生成パスでのみ sandbox を導入する。他の Claude 呼び出しの sandbox 化は別タスク
- **Claude が想定通り Write を使わなかった場合の自動再試行**: 本タスクでは「失敗をログに残し、空のまま完走」する。再試行ロジックは現行のレビューループに任せる

---

## 8. 受け入れ条件（マスタ）

以下のすべてが満たされたとき本タスクを完了とする。

- [ ] `_build_code_generation_prompt` が Write ツール経由のプロンプトに書き換えられている
- [ ] コード生成専用 Claude CLI 呼び出しが `--allowedTools Write,Edit` を付与する
- [ ] sandbox ディレクトリ作成・cwd 設定・終了時削除が実装されている
- [ ] ファイル収集ロジック（os.walk + ホワイトリスト + サイズ上限）が実装されている
- [ ] 失敗判定（F4.1〜F4.5）が構造化ログを出力する
- [ ] 単体テスト（F7.1〜F7.3）が pytest でパス
- [ ] 既存テスト 229 件が全てパス（非回帰）
- [ ] `docs/architecture.md` / `docs/functional-design.md` に「コード成果物のみファイル書き込み方式」の例外設計を明記
- [ ] 実機検証: 「AWS の CloudFront」等の IT トピック投入で `code_files` が GitHub に push される
- [ ] 実機検証: コードを含まない投入（時事問題等）で従来通りテキスト成果物のみが Notion に投稿される
- [ ] main へマージ済み

---

## 9. 想定される質問・懸念

### Q1. Claude が Write ツールを使わずにテキストで返してしまった場合は？

A: F4.1（`reason: "no_files_written"`）として失敗扱いにする。Claude のレスポンステキストは構造化ログに preview を残し、原因究明に使う。レビューループが既に走っているため、レビュアーの修正指示で再生成される可能性もある（4/23 修正で code_files 保護されているので、空のまま review_fix に渡る）。

### Q2. sandbox ディレクトリのクリーンアップ漏れで /tmp が肥大化しないか？

A: ECS Fargate のタスクは終了時にコンテナごと破棄されるため、/tmp も自動削除される。ただしタスク内で複数回コード生成する場合があるので、各呼び出しの finally で個別 sandbox を削除する（NF2.1）。

### Q3. ai-papers-digest との設計乖離が将来運用負債になるのでは？

A: ai-papers-digest はコード生成しないため、根本的に同じ設計を取る必要がない。設計差異の根拠を `docs/architecture.md` に明記しておけば、後続実装者の混乱は防げる（NF4）。

### Q4. なぜコード生成だけ例外なのか、を将来の自分や他のエンジニアが納得できる形で残す必要があるのでは？

A: その通り。本 steering と `docs/architecture.md` に「成果物の性質的差異（サイズ・特殊文字密度）が JSON 適性を分ける」という分析結果を明記する。Q4 自体への答えとして §1.3 の比較表をそのまま `docs/` にも転記する。

### Q5. レビューループ内の review-fix で Claude が Write ツールを使えない場合は？

A: review-fix のプロンプトはテキスト修正のみなので、Write ツール許可は不要。code_files 自体は 4/23 修正の `_PRESERVED_DELIVERABLE_FIELDS` で保護されている（生成時に書き込まれたものが review_fix で消えない）ため、レビューループは現行のまま動作する。

### Q6. 失敗時のリカバリー手段は？

A: 現行と同じく、Slack 経由でユーザーが同じトピックを再投入する。実装上の自動リトライは入れない（無限ループ防止）。
