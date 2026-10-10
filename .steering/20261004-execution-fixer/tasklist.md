# タスクリスト：修正役（第 2 段階）

> **進捗ステータス (2026-10-05)**: Phase 0〜5b まで完了（Phase 5 は、reviewer と Codex のクロスレビューの指摘 F1〜F12 への対応。Phase 5b は、修正後の Codex の再レビューの指摘 H1〜H9 への対応で、この作業単位の最後の修正）。`tests/unit/fixer/`（1,811 件）は全件通り、それ以外の既存のテスト（2,230 件）は成否が変わらない。`ruff check src/fixer tests/unit/fixer` は指摘なし。D0-1 の結果と実装時に決めた点は `design.md` §19、レビュー指摘への対応は §20、最終の再レビューへの対応は §21、既知の制限は §16（位置づけと、隔離された環境の条件を足した）。**Phase 5b の修正は、再レビューを受けていない**（人が後で見るべき箇所は、developer の報告と `design.md` §21）。**修正役の位置づけは、「隔離された環境で、試験的に動かせる段階」**（`design.md` §16.0）。D6-1（永続的な文書）は、用語の名称が確定したため（2026-10-05、ユーザーの了承のもと司令塔が確定）、完了。実際に動かす確認（U7-4）は、隔離された環境で、ユーザーの承認の後に行う。
> ユーザーの承認（2026-10-04、`requirements.md` §6 の記録）により、設計書・タスク一覧・実装の各段階で個別の承認は求めない。
> **引き続きユーザーの承認が要るもの**: コミット、プッシュ、プルリクエストの作成、デプロイ、実際の LLM を使う手動確認（修正役を実際に動かすことを含む）、`CLAUDE.md` の改訂、ADR-0003 の作成。
> **前提**: 第 1 段階（実行診断役）の、Codex のクロスレビューへの修正が済んでいること（例外の型の許可一覧、スタックトレースの最初のブロックからだけ場所を取る）。

## 進め方

本部の流れに合わせる: **tester がテストをまとめて先に書く → developer が実装する → tester が実行し、不足を足す → reviewer がレビューする → 指摘に対応する**。

- **担当**: `[tester]` だけがテストを書き、変えられる。`[developer]` は実装だけを書き、**テストを変更しない**。テストが設計と食い違っていると思ったら、変更せずに司令塔へ報告する。
- 関数名・型・ツール名・決まった値・終了理由は、`design.md` §2〜§12 のとおり。テストと実装は、この名前で合わせる。
- `AC-n` は `requirements.md` §5 の受け入れ条件、`G-n` は合否の検査の番号。
- 第 1 段階のコード（`src/agent/diagnosis/`）と、`orchestrator.py`・`main.py`・`pyproject.toml` は変更しない。

基準のコマンド（変更前の件数は、Phase 0 で確かめる。第 1 段階の完了時点では 1610 passed）:

```bash
TMPDIR=/tmp AWS_DEFAULT_REGION=ap-northeast-1 AWS_ACCESS_KEY_ID=dummy AWS_SECRET_ACCESS_KEY=dummy \
  .venv/bin/python -m pytest -q -p no:cacheprovider
```

リントは、追加・変更したファイルに対して行う:

```bash
.venv/bin/ruff check src/fixer tests/unit/fixer
```

## Phase 0: 前提の確認（コードは書かない）

- [x] D0-1 `[developer]` 第 1 段階の実物を読み、再利用できるものを確かめる。
  - 確かめること: (1) `diagnosis.frames.FrameVerifier` を、`code_root` に任意のディレクトリを渡して使えること。(2) 例外の型の許可一覧を作る関数が、第 1 段階から公開されているか（公開されていれば、その名前と、コードの根を引数で渡せるか）。(3) 受け渡しの形（`DiagnosisHandoff.to_dict()` の項目）が、`design.md` §4.1 の 11 項目と一致するか。(4) `git archive`・`hash-object`・`read-tree`・`update-index`・`write-tree`・`commit-tree`・`update-ref` が、手元の git で `design.md` §5 のとおり使えること（一時的なリポジトリで確かめる。このリポジトリには触れない）
  - 変更するファイル: なし
  - 完了の確かめ方: 4 点の結果を司令塔に報告している。(2)(3) が設計と違う場合は、設計のどこを直す必要があるかを報告している（直すのは architect）
- [x] T0-1 `[tester]` テストの土台に使えるものを確かめる。
  - 確かめること: (1) 第 1 段階の `tests/unit/agent/diagnosis/sdk_fake.py` の部品（`mcp_call_tool`・`result_message`・`init_message` など）を、サーバー名 `fixer` で使えるか。(2) 一時ディレクトリに `git init` した小さなリポジトリで、実物の pytest と ruff を走らせる時間（通しの確認の件数を決めるため）。(3) 変更前の全テストの件数
  - 変更するファイル: なし
  - 完了の確かめ方: 3 点の結果を司令塔に報告している

## Phase 1: テストをまとめて書く（tester）

すべて新規。置き場所は `tests/unit/fixer/`。完了の確かめ方は共通: **テストが収集され、`src.fixer` が無いことを理由に失敗する**（構文エラーや名前の誤りで落ちていない）。既存のテストは通ったまま。

- [x] T1-1 `[tester]` 土台: `__init__.py`、`conftest.py`、`repo_factory.py`、`sdk_fake.py`。
  - 対応: AC-22、AC-26 の土台
  - 中身: ネットワークの禁止（第 1 段階の conftest と同じ止め方）。`subprocess` の呼び出しを記録し、テストの終わりに、git のサブコマンドが決まった一覧に収まっていることを確かめる自動適用フィクスチャ。git の隔離（`GIT_CONFIG_GLOBAL`・`GIT_CONFIG_NOSYSTEM`）。小さなリポジトリを作る関数（`src/agent/sample.py`、`tests/unit/test_sample.py`、`pyproject.toml`、対象外のファイルの見本）。偽の SDK（ツールの呼び出しは、実物の MCP サーバーを通す）。偽の `TestRunner`・`LintRunner`
- [x] T1-2 `[tester]` 入力: `test_handoff_input.py`、`test_exception_types.py`。
  - 対応: AC-1、AC-2、AC-3、AC-32、AC-33
  - 中身: 形の検証（項目の過不足、決まった値、識別子の形、長さ、件数、版の番号）。条件の再計算（偽になる各通りと、ファイルの判定が偽の場合）。場所の再検証（実在する関数と行 / 実在しない関数 / 範囲の外の行 / 200 字を超える名前 / `../` / 対象外のファイル）。例外の型（組み込み / 複製のコードのクラス / 決まった一覧 / 形は識別子だが一覧にない値 / `None`）
- [x] T1-3 `[tester]` 複製と git: `test_gitio.py`、`test_workspace.py`。
  - 対応: AC-6（パス）、AC-20、AC-21、AC-22、AC-28 の部品
  - 中身: `Git` が一覧にないサブコマンドを拒否する。書き出した複製に `.git` がない。`create_branch_commit` の後、親・ブランチ名・コミットの中身が期待どおりで、元の `HEAD`・現在のブランチ・作業ツリー・インデックスが変わらず、仕込んだフックが走らず、既にあるブランチは上書きされない。読み取り・書き込みのパスの検査（絶対パス、`..`、複製の外を指すシンボリックリンク、`.git`、`src/`・`tests/` の外、対象外の一覧、既存のテスト、`conftest.py`）。変更の取り出し（追加・変更・削除・シンボリックリンク・モードの変化・キャッシュの無視）
- [x] T1-4 `[tester]` ツールと実行: `test_tools.py`、`test_runners.py`。
  - 対応: AC-6、AC-7、AC-8、AC-29
  - 中身: 7 つのツールの返すもの。入力の検証（未定義の項目、型の違い。結果に値が出ない。足取りと回数に残る）。上限（`call_limit`）。`run_tests` のパスの検証（複製の外、`-` で始まる値、`::`）と時間切れ。**実物の pytest を使う確認**: 親の環境変数に目印の値を置き、テストの中から見えないこと、見える環境変数が決まった一覧だけであること、ホームが空の一時ディレクトリであること
- [x] T1-5 `[tester]` 合否の検査: `test_gates.py`。
  - 対応: AC-11〜AC-19（G-1〜G-10）、AC-31
  - 中身: `check_static` と `check_dynamic` に、手で作った `ChangeSet` と `TestReport` を渡して、検査ごとに「通る」「落ちる」を確かめる（実物の実行なし）。基準の時点で落ちているテストが、G-7 を妨げないこと
- [x] T1-6 `[tester]` 修正役の全体: `test_sdk_server.py`、`test_runner.py`、`test_prompt.py`、`test_draft.py`、`test_cli.py`。
  - 対応: AC-4、AC-5、AC-9、AC-10、AC-20、AC-23、AC-24、AC-27（入口）、AC-28、AC-30、AC-34
  - 中身: ハンドラーを、実物の MCP サーバーを通して呼んだときの形。オプション（`tools` が空、許可は 7 つ、`cwd` が複製、`max_turns`、`model`）。7 つ以外のツールが見えたときの中断。終了理由の各通り（`design.md` §9.3 の 13 行）と、SDK の呼び出し回数。起動の前の確かめ。偽の修正役の台本で、`declined`・`incomplete`・`rejected`・`proposed` になること（テストとリントは偽物）。複製が消えていること、元のリポジトリが変わらないこと。指示文に入る項目（再検証を通った場所だけ、「失敗が観測された場所」「手がかり」「変更せずに終わってよい」の文面、実行 ID が入らない）。下書きの節と固定の文面、説明のマスクと長さ。足取りの順序と件数。入口の引数と終了コード
- [x] T1-7 `[tester]` 通しの確認: `test_end_to_end.py`（実物の git・pytest・ruff を使う。数件に絞る）。
  - 対応: AC-14、AC-15、AC-16、AC-21、AC-31
  - 中身: すべて通って `proposed` になる / 修正前でも通るテストで G-5 に落ちる / 既存のテストを壊して G-7 に落ちる / 作業した複製に残したファイルが、検査に影響しない

## Phase 2: 実装（developer）

依存の少ない順。各タスクの完了の確かめ方は、対応するテストファイルが全パスし、追加したファイルに `ruff check` の指摘がないこと。

- [x] D2-1 `[developer]` `src/fixer/__init__.py`（この時点では空でよい）、`models.py`。
- [x] D2-2 `[developer]` `exception_types.py`、`handoff_input.py`。
  - 確かめ方: `test_exception_types.py`、`test_handoff_input.py`
- [x] D2-3 `[developer]` `gitio.py`。
  - 確かめ方: `test_gitio.py`
- [x] D2-4 `[developer]` `gates.py`（対象外の一覧を含む）、`workspace.py`。
  - 確かめ方: `test_workspace.py`、`test_gates.py`
- [x] D2-5 `[developer]` `runners.py`。
  - 確かめ方: `test_runners.py`
- [x] D2-6 `[developer]` `tools.py`。
  - 確かめ方: `test_tools.py`
- [x] D2-7 `[developer]` `prompt.py`、`sdk_server.py`、`draft.py`。
  - 確かめ方: `test_prompt.py`、`test_sdk_server.py`、`test_draft.py`
- [x] D2-8 `[developer]` `runner.py`、`__init__.py` の再公開、`__main__.py`。
  - 確かめ方: `test_runner.py`、`test_cli.py`、`test_end_to_end.py`。`pytest tests/unit/fixer/` が全パス
- [x] D2-9 `[developer]` 文書: `docs/execution-fixer.md`（新規。`design.md` §13 の項目）。
  - 対応: AC-27
  - 確かめ方: 項目がすべて書かれている。テストから `__main__` の実物の実行（実際の LLM）が走っていない

## Phase 3: 実行と不足の追加（tester）

- [x] T3-1 `[tester]` 全テスト・リント・差分を確かめ、実際の出力を司令塔に報告する。
  - 対応: AC-22、AC-25、AC-26
  - 確かめること:
    - 基準のコマンドで、変更前のテストがすべて通ったままで、追加したテストもすべて通る
    - `git diff main -- template.yaml pyproject.toml src/agent/Dockerfile src/agent/requirements.txt src/observability/` が空。`src/agent/` の差分は、第 1 段階のものだけ
    - `src/fixer/` が、Dockerfile と `template.yaml` から参照されていない
    - 上のリントのコマンドで指摘なし
    - 修正役のテストを通じて、リモートに触れる git の操作が 1 度も記録されていない
- [x] T3-2 `[tester]` 受け入れ条件 1〜34 と、テストの対応表を作り、足りないテストを足す。
  - 変更するファイル: `tests/unit/fixer/` の下
  - 完了の確かめ方: 対応表を司令塔に報告している。足したテストが通っている（落ちる場合は、実装の不足として司令塔に報告する）

## Phase 4: レビュー（reviewer）

- [x] R4-1 `[reviewer]` 設計との一致、受け入れ条件の網羅、安全の要件を確かめる。
  - 特に見る点: 複製の外への読み書きが、呼び出しの時点で拒否されるか（R6a）。対象外の一覧が、場所の再検証・書き込みのツール・G-2 の 3 か所で同じものを使っているか。検査のテストが、きれいな複製で走っているか。コミットに入るのが、検査を通ったファイルだけか。git の操作に、リモートに触れるものと、フックを走らせるものが無いか。指示文と下書きに、モデルや記録の文字列が、検証なしで入っていないか
  - 変更するファイル: なし
  - 完了の確かめ方: 指摘の一覧（なければ「なし」）を司令塔に報告している

## Phase 5: 指摘への対応

- [x] T5-1 `[tester]` 指摘ごとに、再現するテストを先に書く。
- [x] D5-1 `[developer]` 指摘に対応する。`design.md` に「実装時に決めた点」「レビュー指摘への対応」を追記する（第 1 段階の §17・§18 と同じ形）。
  - レビュー指摘（2026-10-05、reviewer と Codex）への対応の中身: F1（検査用の複製を 1 つずつ作り、前後で照合する）、F2（git のフックと全体設定を無効にし、元のリポジトリの設定とフックの変化を確かめる）、F3（全体の期限と、検査の合計の上限）、F4（直してよい範囲: `src/trigger/app.py`、`__init__.py`、import の乗っ取りになる名前、認証情報を扱うファイルの注意書き）、F5（G-5〜G-7 の判定）、F6（出力ファイルの扱い）、F7（行数の計算）、F8（リンク越しの場所）、F9（下書きとコミットに入る文字）、F10（根の一覧、ハードリンク、書き出しの属性、参照、ブランチ名、割り込み）、F11（隔離されていない環境での起動を断る）、F12（gitleaks の誤検出）。詳細は `design.md` §20
- [ ] T5-2 `[tester]` 全テストとリントをもう一度確かめる（T3-1 と同じ項目）。

## Phase 5b: 最終の再レビューへの対応

（修正後の Codex の再レビューで、F1〜F12 が塞がっていることが確かめられ、新しい指摘 H1〜H9 が挙がった。この作業単位の、最後の修正。この後の再レビューは行わない。）

- [x] T5b-1 `[tester]` 指摘ごとに、再現するテストを先に書く（`tests/unit/fixer/test_final_fixes.py` の 100 件と、既存のテストの改訂）。
- [x] D5b-1 `[developer]` 指摘に対応する。`design.md` に §21（最終の再レビューへの対応）を足し、§16（既知の制限）に、位置づけと、隔離された環境の条件を足す。`docs/execution-fixer.md` に反映する。
  - 対応の中身: H1（検査に使う Python の環境に書き込めるときは、起動を断る）、H2（git に置換参照を読ませない。コミットの前に、作ったツリーを、実行の最初に取った基準の記録と比べる）、H3（部分クローンの遅延取得を止める）、H4（モデルの作業の後に、設定とフックを照合する。モデルが書いたコードが走った後は、`git archive` を呼ばない）、H5（依存の名前を、基準のコミットの `requirements*.txt` からも作る）、H6（パスの要素にピリオドを使えない。同じ ID の結果が複数ある実行は、「検査できなかった」）、H7（1,000,000 バイトを超える `.gitattributes`）、H8（後始末がリンクをたどらない。複製の根が替えられたら、触れない）、H9（読む前に大きさを確かめる。リントにも残り時間を渡す）。詳細は `design.md` §21
  - 確かめ方: `tests/unit/fixer` が全件通る（1,811 件）。それ以外のテストは 2,230 件のまま。H2 のツリーの確かめは、環境変数を外した状態で、スクラッチパッドの小さなリポジトリと、`run_fix` の 2 件で確かめた（`design.md` §21.1 の 4）
- [ ] T5b-2 `[tester]` 全テストとリントをもう一度確かめる（T3-1 と同じ項目）。

## Phase 5c: main に入れる前のレビューへの対応

（`design.md` §22。）

- [x] T5c-1 `[tester]` 指摘を再現するテストを先に書く（`tests/unit/fixer/test_fingerprint_limits.py`）。
- [x] D5c-1 `[developer]` J1: 設定とフックの照合に上限を付ける（1,000,000 バイトを超えるファイルは読まない。深さ 8 段、2,000 個まで。再帰を使わない）。
- [x] D5c-2 `[developer]` J2: 照合の失敗を、例外の種類に関わらず `run_fix` から漏らさない。起動の前の照合を、`git archive` の前に移す。
- [x] D5c-3 `[developer]` J3: `.git/info/attributes` を、読む前に大きさを確かめる。
- [x] D5c-4 `[developer]` J4: 「既存のテスト 2 件が、検査の実行では落ちる」という記述を、今のコードに合わせる（`design.md` §16.2、`docs/execution-fixer.md` §9）。
- [x] D5c-5 `[developer]` 文書（`design.md` §16・§22、`docs/execution-fixer.md`）。
  - 確かめ方（2026-10-10）: `tests/unit/fixer` が全件通る（1,878 件）。全テストは 4,343 件が通る。`ruff check src/agent/diagnosis src/fixer` の指摘なし。実行の後、`/tmp/fixer-*` は残っていない

## Phase 6: 永続的な文書

- [x] D6-1 `[developer]` `docs/glossary.md`（用語の登録。名称は司令塔がユーザーに確認してから）、`docs/repository-structure.md`（`src/fixer/` の置き場所）、`docs/development-guidelines.md`（修正役の成果物を取り込むときの手順）。
  - 完了の確かめ方: 3 つの文書に追記がある。用語の名称が未確定なら、保留にして司令塔へ報告する
  - 結果（2026-10-05）: いったん保留にした後、名称の確定を受けて実施した。`docs/glossary.md`（エージェントの節、新しい小節「実行の診断と修正」、§7 の使い分け）、`docs/repository-structure.md`（置き場所は追記済み。位置づけの書き方を「隔離された環境」に揃えた）、`docs/development-guidelines.md`（5 章「修正役の成果物の取り込み」）。あわせて、`docs/product-requirements.md`（F11 と状態、FR-9）と `docs/functional-design.md`（8 章「診断にもとづく修正」）にも要点と参照を足した。位置づけ（隔離された環境で、試験的に動かせる段階）と、実際の LLM では未検証であることを、各文書に書いた

## Phase 7: ユーザーの承認が要る作業（承認があるまで着手しない）

- [ ] U7-1 ADR-0003 の作成（`design.md` §17 の推奨）。
- [ ] U7-2 コミット（ユーザーが求めたとき）。
- [ ] U7-3 プッシュと、CLAUDE.md 作業規律 §4 の Codex レビューゲート。
- [ ] U7-4 手動確認（任意条件 O1）: 過去の不具合 2 件（`design.md` §13.1）を題材に、修正役を実際に動かし、終了理由・検査の結果・足取りの実出力を記録する。あわせて、`tools=[]` の実際の挙動と、初期化のメッセージのツール一覧を確かめる。**本番の認証情報がない環境で行う**。未実施のあいだは「未検証」と明記する。

## 次の作業単位の候補

`requirements.md` §7 を参照（記録する側で場所を構造化して残す、CI で動かす、プッシュと PR 作成の機能、など）。
