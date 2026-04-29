# 要求内容: レビュー修正再生成における サマリ ↔ code_files 乖離の最小コスト緩和

> 起票: 2026-04-29 / 案 E (プロンプト層スコープ制約追記) ベース
> 過去に検討した案 A (fix loop に workspace 再呼び出し追加) / 案 C (fix loop 廃止) / 案 D (TypedDict 化) は **本タスクのスコープ外**。理由は「スコープ外 (代替案の却下根拠)」に明記する。

## 背景

### システム全体での「成果物生成 → レビュー → 修正再生成」の流れ

Catch-Expander の Orchestrator は `_run` メソッド (`src/agent/orchestrator.py:497-693`) で次の経路を実行する。

```
[ワークフロー計画] generator が text 成果物 (content_blocks / summary 等) を JSON で返す
       ↓
[コード成果物] iac_code / program_code がある場合のみ、workspace モードで独立生成
              call_claude_with_workspace() が sandbox cwd 内に LLM の Write ツールで
              ファイルを書き出し、orchestrator が読み取って deliverables["code_files"] にマージ
       ↓
[レビュー] reviewer が成果物全体 (text + code_files を含む JSON) をチェック
       ↓
[修正再生成 (loop, 最大 2 回)] errors があれば generator を「修正指示」付きで再呼び出し
       ↓
[Notion / GitHub 投稿] 最終 deliverables を Notion へ post_page、code_files を GitHub へ push
       ↓
[DynamoDB / Slack] deliverables レコード保存 → 完了通知
```

### 現在のレビュー修正ループの実装 (該当バグ箇所)

`_run_review_loop` (`src/agent/orchestrator.py:772-849`) の修正適用部分:

```python
# 修正指示でジェネレーターを再実行 (テキスト用 gen_prompt のみを再呼び出し)
fix_prompt = (
    f"{gen_prompt}\n\n"
    f"## 修正指示\n\n"
    f"以下のレビュー指摘に基づき、成果物を修正してください。\n\n"
    f"指摘事項:\n```json\n{fix_instructions}\n```\n\n"
    f"現在の成果物:\n```json\n{json.dumps(current_deliverables, ensure_ascii=False)}\n```"
)
fix_raw = call_claude(fix_prompt)
parsed = _parse_claude_response(fix_raw)
...
preserved = {
    k: current_deliverables[k] for k in _PRESERVED_DELIVERABLE_FIELDS if k in current_deliverables
}
current_deliverables = parsed
current_deliverables.update(preserved)   # ← ここで古い code_files を強制上書き保持
```

ここで `_PRESERVED_DELIVERABLE_FIELDS = ("code_files",)` (`orchestrator.py:24`) は次の意図で導入されている。

```python
# generator は text 成果物のみを返す契約のため、レビュー修正レスポンスを
# current_deliverables に代入すると iac_code/program_code 由来の code_files が失われる。
# 修正適用後に明示的に引き継ぐ独立生成フィールドの一覧。
```

つまり、テキスト generator はコードを返さない契約なので、修正再生成のレスポンス (`parsed`) には `code_files` が含まれない / 含まれても破棄される。これを意図的に古い値で上書き復元している。

### generator / reviewer プロンプトの責務分担

- `src/agent/prompts/generator.md:5-9, 76` で **generator はテキスト成果物のみを返す契約**。`code_files` フィールドは「出力しないでください」と明示。
- `src/agent/prompts/reviewer.md:60-66` で **reviewer はコード関連指摘 (構文・API バージョン・ハードコード・README 整合性) を `issues[]` に出してよい契約**。

この非対称により、reviewer がコード関連の error を出しても、fix_prompt が renderer する LLM (generator) は「コードを修正できない」ことを知らないまま「修正指示への対応」を求められる。LLM は自然な振る舞いとして **summary / content_blocks に「filter ブロックを削除しました」「変数を追加しました」等の修正主張を書き込む** が、実 code_files は preserve されたままなので、サマリ宣言と実コードが乖離する。

### 観測された現象 (2026-04-29 14:46 JST のワークフロー)

利用者が AWS Route 53 関連のトピックを投稿した結果、Notion 成果物のサマリに次の文言が含まれた。

> 「※ 本出力は IaC コード修正（resolver.tf の data source スキーマ違反修正・変数追加・README 更新）のみを含む。」

しかし実際の `code_files` には:

- `resolver.tf` の `filter` ブロック削除が反映されていない
- `aggregate_threat_list_id` 変数の追加が反映されていない
- README の CLI 取得ステップ追記が反映されていない

加えて `quality_metadata.notes` に次のような自己申告が入っていた。

> 「サマリ記載の修正内容（filter ブロック削除・aggregate_threat_list_id 変数追加・README への CLI 取得ステップ追記）が実際のコードに一切反映されていない。サマリと code_files / readme_content の内容が乖離しており、ジェネレータの出力差分管理に問題があった可能性が高い。」
> 「レビュー修正上限（2回）に到達。未修正の指摘: 2件」

レビュー上限到達時点でも、修正版の code_files は一度も再生成されないため、reviewer は何度回しても同じ code_files を見続けることになる。

### 直前の関連変更 (2026-04-29)

- 同日 `feat: persist GitHub URL on deliverables and surface in F9 history` (commit `d0900c8`) で `deliverables.github_url` の永続化・F9 履歴表示を追加した。これにより GitHub に push される code_files の URL は DynamoDB に残るようになったが、その code_files の **内容** がサマリと乖離しているため、利用者が GitHub URL から到達した先のファイルがサマリの説明と一致しないという二次的な信用毀損リスクが顕在化した。
- 直前のセッションで `catch-expander-deliverables` 専用リポジトリへ移行済み。今後 push される code_files はすべてこの新リポジトリに配置されるため、誤ったコードが「正しいリポジトリの最新コミット」として残る点も問題を増幅する。

### 関連する既存ナレッジ・過去 steering

- `obsidian/2026-04-26_symptomatic-fix-anti-pattern.md` — 「同じ場所に 3 回目のパッチを入れる前に 3 層の代替案を並べる」ルール。本タスクの方針選定で適用した。
- `.steering/20260418-quality-fix/` (M2) — fix loop 戻り値を tuple 化して呼出元 rebind
- `.steering/20260423-review-loop-code-files-loss/` — `_PRESERVED_DELIVERABLE_FIELDS` を導入し fix 適用時に code_files を保護
- `.steering/20260425-code-gen-redesign-filesystem/` — code 生成を JSON 経路から workspace モード (Write ツール直書き) へ
- `.steering/20260418-review-fix-reflection-verification/` — fix loop の実機効果検証 (受動観測中、効果未確認)

`_run_review_loop` 周辺は過去 4 件の steering で連続パッチされた高リスク領域であり、5 件目の構造的変更は対症療法アンチパターンに該当する可能性が高い。本タスクは **構造を変えずプロンプト層で観測症状を最小コストで塞ぐ** 案 E を採用する。

## 課題

### 1. コード関連指摘を受けると LLM がサマリだけ書き換えてしまう

`_run_review_loop` の `fix_prompt` (`orchestrator.py:820-826`) は、コード関連指摘もテキスト関連指摘も区別せず generator に丸投げする。`prompts/generator.md` でコード出力禁止が宣言されているにもかかわらず、fix_prompt 自体には「code_files はこのループで修正できない」という制約が書かれていないため、LLM は「指摘に対応した」体裁を取りつつ、自身が触れる範囲の summary / content_blocks に修正主張を書き込んでしまう。

| reviewer が出す指摘の種別 | LLM の振る舞い | 実コードへの反映 |
|---|---|---|
| サマリ・本文の表現修正 | content_blocks / summary を書き換える | ○ 反映される |
| content_blocks の構造変更 | 配列を再構成する | ○ 反映される |
| **コード (iac_code / program_code) の修正** | **summary に「修正した」と宣言** | **× 反映されない** |
| README (code_files.readme_content) の修正 | summary に「修正した」と宣言 | × 反映されない |

これは reviewer / generator のプロンプト不整合（reviewer はコード指摘を出してよいが、fix loop の generator はコードに触れられない）を fix_prompt 文字列が仲介できていない結果である。

### 2. サマリの "嘘" による信用毀損

修正再生成で text 成果物 (summary / content_blocks) は実際に LLM が書き換えるため、LLM は「修正しました」「filter ブロックを削除しました」と主観的に宣言する。しかし code_files は変わらないため、

- 利用者は Notion ページのサマリを信じて「修正版のコードが GitHub にある」と誤認する
- 利用者がそのコードを `terraform apply` などに使うと、想定と異なる挙動でデプロイ事故が起きうる
- 「Catch-Expander のサマリは信用できない」という長期的なツール不信を招く

これは単なるバグではなく、**LLM 生成物に対するユーザー信頼の根幹を損なう** 種類の不具合である。

### 3. レビュー修正上限の半ば形骸化

`MAX_REVIEW_LOOPS = 2` (`orchestrator.py:18`) はコード品質を底上げするための仕組みだが、コード関連指摘に対しては毎回同じ code_files を reviewer に渡すため、

- 1 回目: reviewer がコードの誤りを指摘 → summary は「修正したつもり」 → code_files は変わらない
- 2 回目: 同じ code_files を見て同じ誤りを指摘 → summary は「修正したつもり」 → code_files は変わらない
- 上限到達: notes に「未修正の指摘: N 件」と記録されるが、各ループでサマリ宣言と実コードが乖離した状態で進行している

リトライ予算がコード関連指摘に対しては実質 0 試行となっており、その間にサマリ嘘が累積する。

### 4. 自己申告検出への偶然依存

今回のケースでは LLM 自身が `quality_metadata.notes` に「サマリと code_files が乖離している」と書き残してくれたため発覚したが、これは LLM が「気づいた場合に書く」確率的挙動であり、検出機構として保証はない。

- 同様の乖離が起きても LLM が気づかなければ、利用者は乖離に気づけない
- レビュアーが指摘するか・notes に書くかは prompt 設計と実行ごとのゆらぎ次第

検出の偶然性に頼るのではなく、**fix_prompt 側で「コード関連指摘は本ループで修正できない」「修正したと書くな」「未修正として notes に記録せよ」を明示的に指示する** ことで、サマリ嘘の発生確率を下げ、未修正の正直な記録を取れるようにする。

### 5. ドキュメントへの未記載

`docs/functional-design.md` のレビュー機能の節には、レビュー修正ループの責務範囲（コード関連指摘は本ループで修正されない設計上の限界）が明記されていない。本対応で fix_prompt に制約を追記する場合、ドキュメント側にも「修正再生成ループの守備範囲はテキスト成果物のみ。コード関連指摘は notes に記録される」旨の 1 段落を追加し、設計と実装の整合を取る。

## 目的

レビュー指摘の中にコード成果物 (iac_code / program_code / readme_content) に対する指摘が含まれる場合に、**LLM がサマリで「修正した」と宣言してしまう確率を下げ、代わりに `quality_metadata.notes` に「本ループでは未修正」として正直に記録される** 状態にする。

これは "サマリと code_files の乖離を構造的に消す" 大規模改修ではなく、**プロンプト層への約 5 行の制約追記による最小コスト緩和** であり、観測症状 (サマリ嘘) を縮小しつつ、構造的な再設計 (案 A/D) は将来の独立タスクとして判断余地を残す。

## ユーザーストーリー

- **U1**: 私 (利用者) は Notion サマリで「コードを修正した」と書かれていれば、GitHub の実ファイルにもその修正が反映されていることを期待する。本対応では「修正したと書かれていない」状態を実現する (修正主張がそもそも出ない)。
- **U2**: 私 (利用者) はレビュー指摘の修正に失敗した場合は「修正できなかった」と正直に通知され、未修正のまま放置されたファイルを `terraform apply` などに使わずに済むことを期待する。本対応では `quality_metadata.notes` に「コード関連指摘 N 件は本ループで未修正」として記録される。
- **U3**: 私 (運用者) はテキストのみの指摘 (本文表現、出典記述等) に対しては従来通り修正再生成が機能することを期待する。本対応はテキスト経路の挙動を変えない (回帰なし)。
- **U4**: 私 (開発者) は本タスクが `_run_review_loop` への 5 件目のパッチであることを踏まえ、構造変更を最小化したい。本対応はプロンプト層への約 5 行追記のみで、パイプライン・型・呼び出しグラフは変更しない。

## 受け入れ条件

- [ ] AC1: `_run_review_loop` の `fix_prompt` 文字列に、次の意図を含むスコープ制約セクション (約 5 行) が追加されている。
  - (a) 本ループで修正できるのは text 成果物 (`content_blocks` / `summary`) のみ
  - (b) `code_files` (`*.tf`, `*.py`, README) はこのループでは修正できない
  - (c) コード関連指摘を受けた場合、`summary` / `content_blocks` に「コードを修正した」「filter ブロックを削除した」等の修正主張を書かない
  - (d) 代わりに、修正版 deliverables の `quality_metadata.notes` (もしくは LLM が出力する notes 相当フィールド) に「コード関連指摘 N 件を受領したが本ループでは未修正」として記録する
  - (e) テキスト関連指摘 (本文表現・出典・構成) は従来通り反映する
- [ ] AC2: ユニットテスト (`tests/unit/agent/test_orchestrator.py`) に新規 2 ケースが追加されている。
  - **ケース 1 (コード関連指摘)**: reviewer が「resolver.tf の filter ブロック削除」相当の error を返すモックレビュー結果を入力したとき、fix_raw として返されるモック generator 応答を `_run_review_loop` に通した結果、最終 `current_deliverables` の `summary` / `content_blocks` テキスト全体に「修正した」「削除した」等の修正完了を主張する語が含まれない。かつ `quality_metadata.notes` 相当に「未修正」を示す記録が残る。
  - **ケース 2 (テキスト指摘 — 回帰)**: reviewer が「セクション X の表現修正」相当の error を返したとき、現状ロジックと同様に summary / content_blocks が更新され、`code_files` は preserve される (既存の review-fix 系テストの挙動に整合する)。
- [ ] AC3: 既存の review loop テスト (passed / 上限到達 / parse_error / preserved fields など) が引き続き pass する。`tests/unit/agent/test_orchestrator.py` 全 66 件と、orchestrator が間接的に呼ばれる統合テストを含めて回帰なし。
- [ ] AC4: `docs/functional-design.md` のレビュー機能の節に、「修正再生成ループはテキスト成果物のみを対象とし、コード関連指摘は `quality_metadata.notes` に未修正として記録される」旨の説明 (短く 1〜2 段落) が追加されている。
- [ ] AC5: `prompts/generator.md` および `prompts/reviewer.md` は本タスクで変更しない (責務契約は維持)。
- [ ] AC6: `_run_review_loop` のシグネチャ・戻り値・呼び出しグラフ・`_PRESERVED_DELIVERABLE_FIELDS` 定義は本タスクで変更しない (型・パイプライン層は不変)。

## 制約事項

- 変更箇所は `src/agent/orchestrator.py` の `fix_prompt` 文字列構築部 (orchestrator.py:820-826) **のみ**。`call_claude_with_workspace` 周辺・generator プロンプト・reviewer プロンプト・`_PRESERVED_DELIVERABLE_FIELDS` には触れない。
- 追記する制約文は約 5 行 (日本語で 200〜400 字程度) を上限とする。長文の責務再定義はしない。
- `MAX_REVIEW_LOOPS = 2` の値は変更しない。
- DynamoDB / Notion / GitHub / Slack 連携部・Lambda trigger・`template.yaml` は変更しない。
- 既存テスト (orchestrator: 66 件、trigger: 60 件、合計 237 件相当) を壊さない。
- `quality_metadata.notes` のスキーマは変更しない (既存の `list[str]` を踏襲)。
- 「コード関連指摘」の判定は LLM 側に任せる (fix_prompt 内で reviewer 出力を読んで自己判定する形)。orchestrator 側で正規表現や決定論的判定を導入しない。
- 本変更は ECS 側 (`src/agent/orchestrator.py`) で完結し、Lambda trigger 側のコード変更は不要。

## 非対応 / スコープ外 (代替案の却下根拠)

過去セッションで以下の代替案を検討済み。本タスクでは扱わない。各案の却下根拠を明記する。

### 案 A: fix loop に `call_claude_with_workspace` 再呼び出しを追加

コード関連指摘を検出した場合に、修正再生成内で workspace モードを再呼び出しして code_files も再生成する案。

- **却下根拠**: `_run_review_loop` および周辺 (`_PRESERVED_DELIVERABLE_FIELDS`) は過去 4 件の steering で連続パッチされた高リスク領域。本案は M2 (20260418) → 20260423 に続く 3 回目の同種パッチに該当し、`obsidian/2026-04-26_symptomatic-fix-anti-pattern.md` のチェックリスト ("3 回目を試す前に止まる") に該当する。
- **将来再評価の条件**: 案 E のプロンプト層緩和を投入後 1〜2 ヶ月、現実のワークフローで「コード関連指摘の未修正が累積し、利用者の運用阻害が観測される」場合に独立タスクとして再起票する。

### 案 C: fix loop 全廃止

修正再生成ループ自体を削除し、レビュー結果は notes に記録するのみとする案。

- **却下根拠**: `.steering/20260418-review-fix-reflection-verification/` で fix loop の実機効果は **未測定** のまま。「効果がない」のではなく「未確認」であり、廃止判断は absence of evidence に依拠する論理エラーになる (`feedback_anti_pattern_discipline.md` 参照)。
- **将来再評価の条件**: fix loop の実機効果計測 (テキスト指摘での修正成功率・ユーザー満足度) を別タスクで完了し、効果が無視できる水準であると判明した時点で再起票する。

### 案 D: deliverables / quality_metadata の TypedDict 化と ownership 整備

deliverables 構造を TypedDict 化し、`text` / `code` / `metadata` の ownership を型レベルで分離して、generator / reviewer / fix loop の責務違反をコンパイラまたはテストで検出可能にする案。

- **却下根拠**: 大差分・広範囲。本タスクの観測バグ (Route 53 ワークフローでのサマリ嘘) の直接解決には繋がらない。型整備は将来の再発予防として価値があるが、独立した設計レビューを伴うべき規模。
- **将来再評価の条件**: 本タスク以降に類似の責務違反バグが 2 件以上累積した時点で、独立した設計タスクとして起票する。

### 本タスクで扱わないその他項目

- 既存の乖離成果物 (Notion / GitHub に既に投稿済みのもの) の事後修正
- generator のテキスト生成パス自体を workspace モードに移行する大規模アーキテクチャ変更
- `MAX_REVIEW_LOOPS` の値変更 (上限緩和) — 別件で扱う
- reviewer 側で「指摘がコード関連かどうか」をフィールドとして明示させるプロンプト改修 — 必要なら fix_prompt 側で軽量に LLM 判定するに留め、reviewer 出力スキーマの破壊的変更はしない
- サマリと code_files の自動 diff 検証 (LLM ではない決定論的整合チェック) — 本変更で観測症状が縮小すれば次善でよく、別件で扱う
- workspace モードの再生成失敗時のリトライ機構強化 — 既存の `MAX_CLAUDE_RETRIES` で十分とみなす
