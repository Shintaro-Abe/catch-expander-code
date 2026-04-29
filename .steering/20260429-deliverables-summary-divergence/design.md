# 設計: fix_prompt へのスコープ制約追記によるサマリ嘘の最小コスト緩和

> 採用案: **案 E (プロンプト層スコープ制約追記)**
> 代替案 (A/C/D) の却下根拠は `requirements.md` を参照。本設計では案 E の実装方針のみを扱う。

## 1. 設計方針

### 1.1 大方針

`src/agent/orchestrator.py:820-826` の `fix_prompt` 文字列に、本ループの修正可能範囲を LLM に伝える制約セクション (約 5〜8 行) を追記する。

- **触る箇所はこの 1 文字列のみ**。`_run_review_loop` のロジック・シグネチャ・戻り値・呼び出しグラフ・`_PRESERVED_DELIVERABLE_FIELDS` には触れない。
- `prompts/generator.md` / `prompts/reviewer.md` は変更しない (LLM 全体への責務契約は維持し、fix_prompt はその場でのスコープ宣言として機能する)。
- 「コード関連指摘か否か」の判定は LLM 側に委ねる (orchestrator 側で正規表現・キーワード判定を導入しない)。

### 1.2 アンチパターン回避との接続

`obsidian/2026-04-26_symptomatic-fix-anti-pattern.md` の「3 層代替案」原則に従い、最小コスト層 (プロンプト層) から塞ぐ。本設計はその選択結果であり、パイプライン層 (案 A) / 型層 (案 D) は将来の独立タスクとして判断余地を残す。

### 1.3 LLM 指示遵守の確率的性質に対する立場

LLM への自然言語指示は確率的に効く / 効かないが揺れる。本設計はこれを承知のうえで:

- **完全な乖離排除を狙わない**。観測症状 (サマリ嘘) の発生率を下げ、未修正を notes に正直記録させることを目的とする。
- ユニットテストは「LLM の挙動」ではなく「fix_prompt 文字列に制約セクションが含まれていること」と「`_run_review_loop` が制約付き fix_prompt を生成して call_claude に渡していること」を検証する。LLM の応答に対しては既存テストと同じ mock 戦略で挙動を再現する。
- 実機での効果測定 (Route 53 のような実トピック投入での再現) は本タスクのスコープ外とし、投入後の運用観測で評価する (=`feedback_anti_pattern_discipline.md` の規律に従い、効果未確認のまま縮小判断はしない)。

## 2. 変更箇所

### 2.1 ファイル一覧

| ファイル | 変更種別 | 内容 |
|---|---|---|
| `src/agent/orchestrator.py` | 修正 | `fix_prompt` 文字列にスコープ制約セクションを追記 (1 箇所、約 5〜8 行) |
| `tests/unit/agent/test_orchestrator.py` | 追加 | TestReviewLoop クラスにテスト 2 ケース追加 |
| `docs/functional-design.md` | 修正 | 「レビューループ」節に「修正可能範囲」段落を 1 段落追加 |

これ以外のファイル (Lambda trigger・GitHub クライアント・Notion クライアント・DynamoDB クライアント・`template.yaml`・`prompts/*.md`) は変更しない。

### 2.2 `src/agent/orchestrator.py` の変更詳細

#### 現状 (orchestrator.py:820-826)

```python
fix_instructions = json.dumps(review_result.get("issues", []), ensure_ascii=False)
fix_prompt = (
    f"{gen_prompt}\n\n"
    f"## 修正指示\n\n"
    f"以下のレビュー指摘に基づき、成果物を修正してください。\n\n"
    f"指摘事項:\n```json\n{fix_instructions}\n```\n\n"
    f"現在の成果物:\n```json\n{json.dumps(current_deliverables, ensure_ascii=False)}\n```"
)
```

#### 変更後 (案)

```python
fix_instructions = json.dumps(review_result.get("issues", []), ensure_ascii=False)
fix_prompt = (
    f"{gen_prompt}\n\n"
    f"## 修正指示\n\n"
    f"以下のレビュー指摘に基づき、成果物を修正してください。\n\n"
    f"### 本ループでの修正可能範囲\n"
    f"- 修正できるのは text 成果物 (`content_blocks` / `summary`) のみ。"
    f"`code_files` (`*.tf`, `*.py`, README 等) は別パイプラインで独立生成されており本ループでは修正できません。\n"
    f"- コード関連指摘 (構文・API バージョン・リソース定義・README 整合性等) を受け取った場合、"
    f"`summary` および `content_blocks` に「コードを修正した」「filter ブロックを削除した」等の修正主張を**書かないでください**。"
    f"代わりに `quality_metadata.notes` (list[str]) に「コード関連指摘 N 件は本ループ未修正」として正直に記録してください。\n"
    f"- テキスト関連指摘 (本文表現・出典記述・構成変更) は従来通り反映してください。\n\n"
    f"指摘事項:\n```json\n{fix_instructions}\n```\n\n"
    f"現在の成果物:\n```json\n{json.dumps(current_deliverables, ensure_ascii=False)}\n```"
)
```

差分は **追加のみ・既存行は無変更**。文字列構築の f-string が継続するだけなので、テストや呼び出し側への影響はゼロ。

#### 文言の根拠

- `text 成果物 (content_blocks / summary)` という用語は `prompts/generator.md:7-9, 76` の責務契約と一致。
- `code_files (*.tf, *.py, README 等)` は実観測バグ (Route 53 ワークフロー) で乖離した具体ファイル種別を反映。
- `quality_metadata.notes` (list[str]) は既存スキーマ (`prompts/reviewer.md:122` および orchestrator.py:809-811) を踏襲。新スキーマ追加なし。
- 「N 件」という具体数指示は LLM が `issues[]` を読んで埋めることを期待。orchestrator 側で件数を埋め込むことも可能だが、LLM 任せのほうが「コード関連の判定」を LLM に閉じ込められる (orchestrator が判定ロジックを持たない方針と整合)。

### 2.3 テスト追加 (tests/unit/agent/test_orchestrator.py)

既存の `TestReviewLoop` クラス (line 314 以降) に追加。`test_review_loop_preserves_code_files_on_fix_success` (line 466) の直後に配置する。

#### ケース 1: コード関連指摘 → fix_prompt にスコープ制約が含まれる + summary に修正主張なし

```python
@patch("orchestrator.call_claude")
def test_review_loop_code_issue_does_not_claim_fix_in_summary(self, mock_claude):
    """コード関連指摘を受けても、generator は summary で「修正した」と主張しない（fix_prompt のスコープ制約が機能している前提）"""
    from orchestrator import Orchestrator

    code_issue = {
        "item": "コードの構文",
        "severity": "error",
        "description": "resolver.tf の filter ブロックは Route 53 Resolver では非対応",
        "fix_instruction": "resolver.tf から filter ブロックを削除する",
    }
    responses = [
        json.dumps({"result": json.dumps({"passed": False, "issues": [code_issue], "quality_metadata": {}})}),
        # generator はスコープ制約に従い、summary に修正主張を書かず notes に未修正を記録する
        json.dumps({"result": json.dumps({
            "content_blocks": [{"t": "unchanged"}],
            "summary": "Route 53 Resolver の概要レポート",
            "quality_metadata": {"notes": ["コード関連指摘 1 件は本ループ未修正"]},
        })}),
        json.dumps({"result": json.dumps({"passed": True, "issues": [], "quality_metadata": {}})}),
    ]
    mock_claude.side_effect = responses

    orch = Orchestrator(MagicMock(), MagicMock(), "token", "db_id", "gh_token", "owner/repo")
    original_code_files = {"files": {"resolver.tf": "..."}, "readme_content": "# README"}
    original = {"content_blocks": [{"t": "original"}], "summary": "初版", "code_files": original_code_files}

    result, final_deliverables = orch._run_review_loop("prompt", original, [], "技術", "gen_prompt", "C1", "ts1")

    # fix_prompt がスコープ制約セクションを含んでいること（call_claude 第 2 呼び出し = fix call）
    fix_call_prompt = mock_claude.call_args_list[1].args[0]
    assert "本ループでの修正可能範囲" in fix_call_prompt
    assert "code_files" in fix_call_prompt
    assert "quality_metadata.notes" in fix_call_prompt

    # summary に修正主張が含まれない
    assert "修正" not in final_deliverables["summary"]
    assert "削除" not in final_deliverables["summary"]
    # code_files は preserve されている
    assert final_deliverables["code_files"] == original_code_files
```

#### ケース 2: テキスト指摘 → 従来通り content_blocks / summary が更新される (回帰確認)

```python
@patch("orchestrator.call_claude")
def test_review_loop_text_issue_updates_text_normally(self, mock_claude):
    """テキスト関連指摘では、従来通り summary / content_blocks が更新される（回帰なし）"""
    from orchestrator import Orchestrator

    text_issue = {
        "item": "セクション 3 の表現",
        "severity": "error",
        "description": "数値 $100 億 が出典 [3] と矛盾 ($85 億)",
        "fix_instruction": "セクション 3 の市場規模を $85 億 に修正",
    }
    responses = [
        json.dumps({"result": json.dumps({"passed": False, "issues": [text_issue], "quality_metadata": {}})}),
        json.dumps({"result": json.dumps({
            "content_blocks": [{"t": "fixed-section-3"}],
            "summary": "市場規模を $85 億 に修正済みの最新版",
        })}),
        json.dumps({"result": json.dumps({"passed": True, "issues": [], "quality_metadata": {}})}),
    ]
    mock_claude.side_effect = responses

    orch = Orchestrator(MagicMock(), MagicMock(), "token", "db_id", "gh_token", "owner/repo")
    original_code_files = {"files": {"main.tf": "..."}, "readme_content": "# README"}
    original = {"content_blocks": [{"t": "original"}], "summary": "初版", "code_files": original_code_files}

    result, final_deliverables = orch._run_review_loop("prompt", original, [], "技術", "gen_prompt", "C1", "ts1")

    assert result["passed"] is True
    assert final_deliverables["content_blocks"] == [{"t": "fixed-section-3"}]
    assert "$85" in final_deliverables["summary"]
    # code_files は preserve されている
    assert final_deliverables["code_files"] == original_code_files
```

#### テスト設計上の注意

- ケース 1 は「`fix_prompt` 文字列にスコープ制約が含まれること」を `mock_claude.call_args_list` 経由で検証する → これは LLM 挙動に依存しない決定論的な検証であり、本対応の構造的な保証になる。
- 「summary に修正主張が含まれない」「notes に未修正記録が残る」は、mock 応答が制約を遵守したケースをシミュレートして "もし LLM が遵守すれば期待通りに伝搬する" ことを確認する。LLM が遵守しないケースは本テストでは検証できない (確率的性質)。
- 既存の `test_review_loop_preserves_code_files_on_fix_success` 等は **無変更で pass** することが必要 (回帰確認)。

### 2.4 ドキュメント更新 (docs/functional-design.md)

`docs/functional-design.md:495-508` の「レビューループ」節に、line 508 の「**独立生成フィールドの保護**」段落の直後に以下を追加する。

```markdown
**修正可能範囲のスコープ宣言**: `_run_review_loop` の修正再生成プロンプト (`fix_prompt`) には「本ループで修正できるのは text 成果物 (`content_blocks` / `summary`) のみ。`code_files` は別パイプラインで独立生成されており本ループでは修正できない」とのスコープ制約を明示する。これにより、reviewer が出すコード関連指摘 (構文・API バージョン・README 整合性等) を受け取った場合に、ジェネレーターが summary に「コードを修正した」と主張する（実体は変わらない）乖離を緩和する。コード関連指摘は `quality_metadata.notes` に「本ループ未修正」として記録され、Notion 出力で利用者が認識できる状態を保つ。
```

これにより設計意図 (「fix loop はテキストのみ守備、コード関連指摘は notes に未修正記録」) が永続的ドキュメントに残り、将来の改修者が背景を辿れるようになる。

## 3. 影響分析

### 3.1 機能影響

| 影響領域 | 影響 |
|---|---|
| テキストのみのワークフロー (例: 時事トピック) | 影響なし。fix_prompt の「コード関連指摘なら〜」セクションは LLM が「該当しない」と判断してスキップする。 |
| コード成果物ありのワークフロー (例: IaC レポート) でテキスト指摘のみ | 従来通り。fix_prompt の追記セクションは「テキスト関連指摘は従来通り反映」と書いているため挙動変化なし。 |
| コード成果物ありのワークフロー (例: IaC レポート) でコード関連指摘あり | **本変更の主対象**。LLM がスコープ制約に従えば、summary 嘘が抑制され、notes に未修正記録が残る。 |
| `_PRESERVED_DELIVERABLE_FIELDS` による code_files 保持 | 影響なし。code_files は従来通り preserve される。 |
| reviewer の挙動・出力スキーマ | 影響なし。`prompts/reviewer.md` 不変。 |
| generator の挙動・出力スキーマ | 影響なし。`prompts/generator.md` 不変。 |

### 3.2 性能・コスト影響

- API トークン消費: fix_prompt の長さが約 200〜400 字増える。1 ループあたりの増加分は無視できる水準。
- ループ回数: `MAX_REVIEW_LOOPS = 2` 不変。コード関連指摘ではフリッカリング (毎ループ同じ指摘を出して未修正記録) が起きうるが、これは既存挙動と同じ (現状もコード関連指摘は反映されない)。サマリ嘘が出ない分だけ品質改善。
- workspace 呼び出し: 追加なし (`call_claude_with_workspace` の再呼び出しは案 A の領域でスコープ外)。

### 3.3 リスクと緩和

| リスク | 発生確率 | 影響 | 緩和策 |
|---|---|---|---|
| LLM がスコープ制約を遵守せず summary に修正主張を書く | 中 | サマリ嘘継続 (現状と同等) | 確率的に発生率が下がる効果を狙う。完全排除は本タスクのスコープ外 (将来の案 A/D で扱う)。 |
| LLM が「コード関連 / テキスト関連」の境界判定を誤る (例: README 修正をテキスト指摘扱いにして summary に「README を修正した」と書く) | 中 | README 部分でサマリ嘘継続 | 制約文に "README" を例示して LLM の判定基準を補強する (上述 2.2 の文言参照)。 |
| 既存テスト (66 件) に回帰 | 低 | 既存挙動が壊れる | 変更は文字列追記のみで `current_deliverables` への代入ロジックは無変更。既存テストの assertion は維持される想定。回帰チェックは必須。 |
| `quality_metadata.notes` が `list` ではなく別型で返ってくる場合の互換性 | 低 | notes 記録が機能しない | 既存の orchestrator.py:809-811 と同じ `notes = review_result.get("quality_metadata", {}).get("notes", [])` パターンが他箇所で機能している前提に依拠。本タスクで型変換は追加しない。 |

### 3.4 ロールバック性

変更は文字列の追記のみ。問題発生時は該当コミットを `git revert` するだけで完全に元の状態に戻る。データ移行・スキーマ変更・外部システムへの永続的影響は一切ない。

## 4. 既存設計との整合性

### 4.1 `_PRESERVED_DELIVERABLE_FIELDS` との関係

本変更は `_PRESERVED_DELIVERABLE_FIELDS = ("code_files",)` の存在意義を強化する方向。

- 旧: 「generator が code_files を返さないので preserve」 (技術的な保護)
- 新: 「fix loop は code_files を意図的に修正しない設計なので preserve」 (設計意図の保護)

LLM への明示的な制約と orchestrator の preserve ロジックが意図整合する。

### 4.2 `MAX_REVIEW_LOOPS = 2` との関係

不変。コード関連指摘は本ループでは修正されない (これは現状と同じ) が、各ループでサマリ嘘が出ない・notes に未修正記録が残るぶんだけ、上限到達時の `notes` の品質が向上する。

### 4.3 reviewer 出力との関係

reviewer は引き続き任意のコード関連指摘を出してよい (出力スキーマ不変)。fix_prompt 側で「これは本ループでは修正できない」とフィルタするため、reviewer 側の責務は変わらない。

## 5. 検証手順

### 5.1 単体テスト

```bash
cd /workspaces/Catch-Expander
pytest tests/unit/agent/test_orchestrator.py::TestReviewLoop -v
```

- 既存 review loop テスト (パス / 上限到達 / parse_error / preserved fields など) が引き続き pass する
- 新規 2 ケース (コード関連指摘 / テキスト関連指摘の回帰) が pass する

### 5.2 全体テスト

```bash
cd /workspaces/Catch-Expander
pytest tests/unit/ -v
```

- orchestrator: 66 件 + 2 件 = 68 件 pass
- trigger: 60 件 pass (本変更で影響なし)
- 全体で 237 + 2 = 239 件 pass

### 5.3 リント・型チェック

```bash
cd /workspaces/Catch-Expander
ruff check src/agent/orchestrator.py tests/unit/agent/test_orchestrator.py
ruff format --check src/agent/orchestrator.py tests/unit/agent/test_orchestrator.py
```

### 5.4 実機検証 (任意・スコープ外)

本タスクの完了条件には含めないが、デプロイ後の運用観測で次を確認できると望ましい (将来の改修判断材料):

- コード関連指摘を含むワークフロー (例: 再度 Route 53 や別 IaC 系トピック) で、Notion サマリに「修正した」が出ない・notes に「未修正」が記録されることをサンプル観測
- 観測結果を元に、案 A (構造的解決) の起票が必要かを判断

## 6. 実装手順 (高レベル)

詳細粒度は `tasklist.md` で扱う。設計レベルでは次の順序を想定:

1. `src/agent/orchestrator.py` の `fix_prompt` 文字列にスコープ制約セクションを追記
2. `tests/unit/agent/test_orchestrator.py` に新規 2 ケースを追加
3. 単体テスト → 全体テスト → リントを実行し pass を確認
4. `docs/functional-design.md` に「修正可能範囲のスコープ宣言」段落を追加
5. ステアリング tasklist.md にチェックを入れて完了

## 7. オープン論点

承認時に確認させていただきたい点:

- **論点 1**: 2.2 で提示した fix_prompt 追記文言 (約 7 行) は、requirements.md AC1 の「約 5 行」より長い。意図を満たすには現実的な行数として 6〜8 行を許容するかたちで進めたいが OK か
- **論点 2**: 2.3 ケース 1 のテストで `assert "修正" not in final_deliverables["summary"]` とゆるい部分一致で書いているが、より厳密な検証 (例: 修正主張を示す語のリスト全部を検査) に変えるべきか
- **論点 3**: ドキュメント更新 (2.4) を本タスクに含めるか、別タスクに切るか (CLAUDE.md ルールでは「必要な場合のみ」)。本変更は設計意図を変えるため含める方針だが、見送る選択肢もある
