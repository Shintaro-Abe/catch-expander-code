# タスクリスト：実行診断役（第 1 段階）

> **進捗ステータス (2026-10-05 更新)**: 実装（Phase 1〜5）、reviewer の指摘への対応（Phase 6b）、Codex のクロスレビューへの対応（Phase 6c）、再レビューへの対応（Phase 6d）まで実装済み。第 1 段階のレビュー対応は、ここで区切り。未コミット。D5-3（用語集など永続的な文書）は、用語の名称が確定したため（2026-10-05、ユーザーの了承のもと司令塔が確定）、完了。手動確認（U7-3）は未実施で、実際の LLM・AWS での動作は未検証。
> ユーザーの承認（2026-10-04、`requirements.md` §6 の記録）により、設計書・タスク一覧・実装の各段階で個別の承認は求めない。
> **引き続きユーザーの承認が要るもの**: コミット、プッシュ、プルリクエストの作成、デプロイ、実際の LLM や AWS の実データを使う手動確認、`CLAUDE.md` の改訂。

## 進め方

- テスト先行。各フェーズは「tester がテストを書く（赤）→ developer が実装する（緑）」の順。
- **担当**: `[tester]` はテストだけを書く。`[developer]` は実装だけを書く。**developer はテストを変更しない**。テストが設計と食い違っていると思ったら、変更せずに司令塔へ報告する。
- 関数名・型・ツール名・決まった値は `design.md` §2〜§10 のとおり。テストと実装は、この名前で合わせる。
- `AC-n` は `requirements.md` §5 の受け入れ条件の番号。

基準のコマンド（変更前は 550 passed）:

```bash
TMPDIR=/tmp AWS_DEFAULT_REGION=ap-northeast-1 AWS_ACCESS_KEY_ID=dummy AWS_SECRET_ACCESS_KEY=dummy \
  .venv/bin/python -m pytest -q -p no:cacheprovider
```

リントは追加・変更したファイルに対して行う（リポジトリ全体には既存の指摘が 32 件ある）:

```bash
.venv/bin/ruff check src/agent/diagnosis tests/unit/agent/diagnosis src/agent/main.py tests/unit/agent/test_main.py
```

## Phase 0: 前提の確認（コードは書かない）

- [ ] T0-1 `[tester]` 導入済みの `claude_agent_sdk`（0.2.110）で、テストから `AssistantMessage`・`ToolUseBlock`・`ResultMessage` を組み立てるのに必要な引数を調べる。
  - 変更するファイル: なし
  - 完了の確かめ方: 3 つの型を組み立てる最小の書き方を、T3-1 のテストの補助関数に使える形で司令塔に報告している
- [x] D0-1 `[developer]` 同じ版で、`tool()` が返すものと `create_sdk_mcp_server()` が受け取るものを調べる（`design.md` §7.1 の `build_server` が書けることの確認）。
  - 変更するファイル: なし
  - 完了の確かめ方: `build_handlers` の非同期関数を `tool()` で包んでサーバーを作る書き方が、導入済みの版で通ることを報告している。通らない場合は、設計のどこを直す必要があるかを報告している

## Phase 1: 型・マスク・受け渡し（SDK にも AWS にも触れない部品）

- [ ] T1-1 `[tester]` テストの土台と、マスク・受け渡しのテストを書く。
  - 対応: AC-10、AC-25（ソケットの禁止）、AC-30
  - 変更するファイル（すべて新規）: `tests/unit/agent/diagnosis/__init__.py`、`conftest.py`、`test_redaction.py`、`test_handoff.py`
  - 中身: `conftest.py` に、`socket.socket.connect`・`socket.create_connection`・`socket.getaddrinfo` を止める自動適用フィクスチャ（`socket.socket` そのものは止めない。`design.md` §13）。マスクの各形、識別子のキーの除去、切り詰め（先頭・末尾・省略の印）、Slack 向けの無効化。`is_fixer_eligible` の真になる 1 通りと、偽になる 4 通り
  - 完了の確かめ方: テストが収集され、`diagnosis` が無いことを理由に失敗する（構文エラーや名前の誤りで落ちていない）
- [x] D1-1 `[developer]` `models.py`・`redaction.py`・`handoff.py` を実装する。
  - 変更するファイル（すべて新規）: `src/agent/diagnosis/__init__.py`（この時点では空でよい）、`models.py`、`redaction.py`、`handoff.py`
  - 完了の確かめ方: `pytest tests/unit/agent/diagnosis/test_redaction.py tests/unit/agent/diagnosis/test_handoff.py` が全パス。追加したファイルに `ruff check` の指摘なし

## Phase 2: 記録の読み取りとツール

- [ ] T2-1 `[tester]` 偽の DynamoDB・S3 と、5 つの筋書きの記録を用意する。
  - 対応: AC-19 の準備
  - 変更するファイル（すべて新規）: `tests/unit/agent/diagnosis/fakes.py`、`scenarios.py`
  - 中身: 偽物は、受けた操作の名前と引数を記録する。筋書きは、調査が全滅 / 出力の検証に失敗 / レート制限 / レビューで不合格が続いた / 記録が欠けている。イベントの形は、実際に emit している箇所に合わせる（`src/agent/orchestrator.py:1594-2018`、`2170-2210`、`src/observability/event_emitter.py:123-195`）
  - 完了の確かめ方: 2 つのファイルが import でき、筋書きごとに実行レコード・段階・イベント・プロンプト記録が取り出せる
- [ ] T2-2 `[tester]` `RecordReader` と `ToolSession` のテストを書く。
  - 対応: AC-1〜AC-9、AC-11〜AC-15、AC-29
  - 変更するファイル（すべて新規）: `tests/unit/agent/diagnosis/test_records.py`、`test_tools.py`
  - 中身: 4 つの読み取りツールの返す項目と根拠 ID。入力の検証（スキーマにない項目、`../` を含む番号、決まった値以外の役）。`not_found`・`unavailable`・`internal_error`（例外の文面が結果に出ない、ログに出る）。`call_limit`・`size_limit`。偽物が受けた操作が読み取りだけで、実行 ID が固定であること。提出の検証、根拠の照合、根拠ゼロでの格下げ、「分からない」の受理、フレーム ID と例外の型の引き当て、発行していない ID の除去
  - 完了の確かめ方: テストが収集され、`diagnosis.records`・`diagnosis.tools` が無いことを理由に失敗する
- [x] D2-1 `[developer]` `records.py` を実装する。
  - 変更するファイル（新規）: `src/agent/diagnosis/records.py`
  - 完了の確かめ方: `pytest tests/unit/agent/diagnosis/test_records.py` が全パス
- [x] D2-2 `[developer]` `tools.py` を実装する。
  - 変更するファイル（新規）: `src/agent/diagnosis/tools.py`
  - 完了の確かめ方: `pytest tests/unit/agent/diagnosis/test_tools.py` が全パス。追加したファイルに `ruff check` の指摘なし

## Phase 3: SDK への登録と実行

- [ ] T3-1 `[tester]` ハンドラーと実行関数のテストを書く。
  - 対応: AC-16、AC-17、AC-18、AC-19、AC-24、AC-25
  - 変更するファイル（すべて新規）: `tests/unit/agent/diagnosis/test_sdk_server.py`、`test_runner.py`
  - 中身: ハンドラーを直接呼んだときの `content` と `is_error`、`session.call` が例外を出すよう差し替えてもハンドラーから漏れないこと。`build_options` の中身（`tools` が空、`allowed_tools` が 5 個、`mcp_servers`、`max_turns`、`model`）。`diagnosis.runner._invoke_sdk` を差し替えた偽の SDK で、終わり方の 4 通り（上限 / 時間切れ / 例外 / 不正な実行 ID）と SDK の呼び出し回数、足取りの順序、5 つの筋書き、`diagnosis_completed` の項目（自由記述・識別子が入らない）とログ 1 行
  - 完了の確かめ方: テストが収集され、`diagnosis.sdk_server`・`diagnosis.runner` が無いことを理由に失敗する
- [x] D3-1 `[developer]` `prompt.py`・`sdk_server.py` を実装する。
  - 変更するファイル（すべて新規）: `src/agent/diagnosis/prompt.py`、`sdk_server.py`
  - 完了の確かめ方: `pytest tests/unit/agent/diagnosis/test_sdk_server.py` が全パス
- [x] D3-2 `[developer]` `runner.py` を実装し、`__init__.py` で `diagnose_execution`・`DiagnosisRun`・`DiagnosisLimits` を再公開する。
  - 変更するファイル: `src/agent/diagnosis/runner.py`（新規）、`src/agent/diagnosis/__init__.py`
  - 完了の確かめ方: `pytest tests/unit/agent/diagnosis/` が全パス。追加したファイルに `ruff check` の指摘なし

## Phase 4: 起動（Slack の投稿文と `main.py`）

- [ ] T4-1 `[tester]` 投稿文と、`main.py` の失敗時の経路のテストを書く。
  - 対応: AC-20〜AC-23
  - 変更するファイル: `tests/unit/agent/diagnosis/test_notify.py`（新規）、`tests/unit/agent/test_main.py`（**追加だけ**。既存のテストは書き換えない）
  - 中身: 投稿文に決まった値の日本語の表示が入り、メンション記法が無効化されていること。フラグが未設定・`false` のとき診断役が呼ばれず、既存の失敗通知が同じ引数で 1 回、例外がそのまま上がること。フラグがオンのとき、失敗通知 → 診断 → 2 通目の順、例外と書き戻しはそのまま。起動しない 4 通り（レート制限 / フィードバック処理 / 実行 ID なし / 認証情報の準備前）。診断役が `failed`・`incomplete`、または投稿が例外を出しても、元の例外・失敗通知・書き戻しが変わらないこと
  - 完了の確かめ方: 追加したテストが、`diagnosis.notify` または `main._maybe_post_diagnosis` が無いことを理由に失敗する。既存の `test_main.py` のテストは通ったまま
- [x] D4-1 `[developer]` `notify.py` を実装する。
  - 変更するファイル（新規）: `src/agent/diagnosis/notify.py`
  - 完了の確かめ方: `pytest tests/unit/agent/diagnosis/test_notify.py` が全パス
- [x] D4-2 `[developer]` `main.py` に `_should_run_diagnosis`・`_maybe_post_diagnosis` と、呼び出し 1 行を足す（`design.md` §9.2）。
  - 変更するファイル: `src/agent/main.py`（追加だけ。既存の行は書き換えない）
  - 完了の確かめ方: `pytest tests/unit/agent/test_main.py` が全パス。`git diff main -- src/agent/main.py` に削除された行がない

## Phase 5: 手動の入口と文書

- [x] D5-1 `[developer]` `__main__.py` を実装する（`design.md` §12）。
  - 対応: AC-28
  - 変更するファイル（新規）: `src/agent/diagnosis/__main__.py`
  - 完了の確かめ方: `ruff check` の指摘なし。実行はしない（記録の読み取りと認証が要るため）
- [x] D5-2 `[developer]` 手動手順の文書を書く。
  - 対応: AC-28
  - 変更するファイル（新規）: `docs/execution-diagnostician.md`
  - 完了の確かめ方: `design.md` §12 の項目がすべて書かれている。テストから `__main__` が実行されていない
- [x] D5-3 `[developer]` 永続的な文書を更新する（`requirements.md` §8）。
  - 変更するファイル: `docs/glossary.md`（用語の登録。名称は司令塔がユーザーに確認してから）、`docs/product-requirements.md`（機能一覧）、`docs/functional-design.md`（失敗時の通知の表）、`docs/repository-structure.md`（`diagnosis/` の置き場所）
  - 完了の確かめ方: 4 つの文書に追記がある。用語の名称が未確定なら、このタスクは保留にして司令塔へ報告する
  - 結果（2026-10-05）: いったん保留にした後、名称の確定を受けて実施した。`docs/glossary.md`（エージェントの節、新しい小節「実行の診断と修正」、§3 の `diagnosis_completed`、§7 の使い分け）、`docs/product-requirements.md`（機能一覧の F10 と状態、FR-9）、`docs/functional-design.md`（エージェント一覧・コンポーネント一覧・8 章のエラーの表と「失敗時の自動診断」）、`docs/repository-structure.md`（置き場所は追記済み。役の名前を添えた）。機能フラグは既定オフ、実際の LLM・AWS では未検証、と各文書に書いた

## Phase 6: 全体の確認

- [ ] T6-1 `[tester]` 全テストとリント、差分を確かめる。
  - 対応: AC-25、AC-26、AC-27
  - 変更するファイル: なし
  - 完了の確かめ方（実際の出力を司令塔に報告する）:
    - 基準のコマンドで、既存の 550 件がすべて通り、追加したテストもすべて通る
    - `git diff main -- src/agent/orchestrator.py template.yaml src/agent/prompts/ src/agent/requirements.txt src/agent/Dockerfile src/observability/` が空
    - 上のリントのコマンドで指摘なし
    - `tests/unit/agent/diagnosis/` のテストが、ソケットを止めた状態・認証情報なしで通っている
- [ ] R6-1 `[reviewer]` 設計との一致、受け入れ条件の網羅、安全の要件（R1・R3・R14・R18）を確かめる。
  - 変更するファイル: なし
  - 完了の確かめ方: 指摘の一覧（なければ「なし」）を司令塔に報告している

## Phase 6b: レビュー指摘への対応（`design.md` §18）

- [x] D6b-1 `[developer]` S1: フレームを実際のコードと突き合わせる（`frames.py` の追加、`ToolSession`・`diagnose_execution` の `code_root`）。
- [x] D6b-2 `[developer]` M1: SDK に渡す入力スキーマを緩め、検証を `ToolSession` に一本化する（`sdk_server.SDK_INPUT_SCHEMAS`）。
- [x] D6b-3 `[developer]` M2: 格下げのときの対処と自由記述の差し替え、投稿文の「未検証」の印と URL の無効化。
- [x] D6b-4 `[developer]` M3: `strict_mcp_config`、`disallowed_tools` の拡張、起動時と途中のツールの確認（`tool_isolation_failed`）。
- [x] D6b-5 `[developer]` M4: コードは変えず、制約を `design.md` §15 と `docs/execution-diagnostician.md` に記録する。本番で有効にする前に、ユーザーが判断する。
- [x] D6b-6 `[developer]` M5: DynamoDB の集合型・バイト列を平らにする。
- [x] D6b-7 `[developer]` M6: `fixer_eligible` の条件を足す（`Diagnosis` に 2 項目）。
- [x] D6b-8 `[developer]` L1〜L7、`--handoff-out` の先に既にファイルがあるときの扱い、`get_prompt_record` の中の確認。
- [x] D6b-9 `[developer]` 文書（`design.md` §18 と食い違う記述の修正、`docs/execution-diagnostician.md`）。
  - 完了の確かめ方: 基準のコマンドで全テストが通る。追加・変更したファイルに `ruff check` の指摘なし。`git diff --numstat -- src/agent/main.py` が追加 42 行・削除 0 行のまま

## Phase 6c: Codex のクロスレビューへの対応（`design.md` §18 の 2 巡目）

- [x] D6c-1 `[developer]` C1: 例外の型を、コード側の許可一覧と照合する（`exception_types.py` の追加。一覧に無い型は `None`、ツールの結果では `<unlisted>`）。
- [x] D6c-2 `[developer]` C2: フレームを、スタックトレースの最初のブロックからだけ取る。
- [x] D6c-3 `[developer]` C3: 段階の読み込みに上限（200 件・2MB）と `ProjectionExpression` を付ける。`has_result` は、2 回目の問い合わせで調べる。
- [x] D6c-4 `[developer]` C4: イベントの外側の項目の型を限り（`<omitted>`）、最小の形（`evidence_id` と `event_type`）を足す。
- [x] D6c-5 `[developer]` C5: AWS の試行回数を `total_max_attempts: 2` にする。
- [x] D6c-6 `[developer]` 文書（`design.md` §11・§15・§18、`docs/execution-diagnostician.md`）。
  - 完了の確かめ方: 基準のコマンドで全テストが通る。追加・変更したファイルに `ruff check` の指摘なし。`git diff --numstat -- src/agent/main.py` が追加 42 行・削除 0 行のまま

## Phase 6d: Codex の再レビューへの対応（`design.md` §18 の 3 巡目）

- [x] D6d-1 `[developer]` A1・A5: マスクの式を線形の時間で終わるように書き直し、対象を足す。
- [x] D6d-2 `[developer]` A2: 受け渡しの場所を、例外の型を取ったイベントのものだけにする（順序と重複はコードが決める）。
- [x] D6d-3 `[developer]` A3: `frames.exception_line_type` を足し、例外の型を、トレースの例外の行と一致するときだけ使う。指示文に案内を足す。
- [x] D6d-4 `[developer]` A4: `ToolSession` に `deadline` を足し、期限を過ぎてから始まる呼び出しを断る（`deadline_exceeded`）。
- [x] D6d-5 `[developer]` A6: Slack の自由記述の書式・不可視の文字・リンクの書き方を無効化する。
- [x] D6d-6 `[developer]` A7: 変えない。手動確認の項目に足す（実物の形を確かめてから決める）。
- [x] D6d-7 `[developer]` 文書（`design.md` §11・§15・§18、`docs/execution-diagnostician.md`）。
  - 完了の確かめ方: 基準のコマンド（`--ignore=tests/unit/fixer` つき）で全テストが通る。追加・変更したファイルに `ruff check` の指摘なし。`git diff --numstat -- src/agent/main.py` が追加 42 行・削除 0 行のまま

## Phase 7: ユーザーの承認が要る作業（承認があるまで着手しない）

- [ ] U7-1 コミット（ユーザーが求めたとき）。
- [ ] U7-2 プッシュと、CLAUDE.md 作業規律 §4 の Codex レビューゲート。
- [ ] U7-3 手動確認（任意条件 O1）: 実際の失敗した実行を 1 件以上診断し、診断結果・足取り・ターン数の実出力を記録する。あわせて、`tools=[]` の実際の挙動、`error_max_turns` のときの `is_error`、実際の `step_id` の形を確かめる（`design.md` §15）。未実施のあいだは「未検証」と明記する。
- [ ] U7-4 本番での有効化（別の作業単位）: `template.yaml` に環境変数 `EXECUTION_DIAGNOSIS` と `s3:GetObject` を足す。

## 第 2 段階へ

第 1 段階の Phase 6 が終わったら、`.steering/20261004-execution-fixer/` の `design.md`・`tasklist.md` を作る。受け渡しの形は `design.md` §11 のとおり。
