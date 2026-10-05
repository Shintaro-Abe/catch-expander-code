"""プルリクエスト本文の下書き（design.md §12。``render_pr_draft``）。

対応: AC-23（R15 の節がすべてあり、冒頭に R16 の固定の文面がある。検査の結果はコードが実行した値と一致する。
モデルの説明は長さの上限内で、「未検証」の明示がある）、AC-34（固定の文面）、AC-24 の部品（足取りの表示）。
``run_fix`` が実際に書き出した下書きの確認は ``test_runner.py``。

決まった文面（developer は、この語を下書きに入れる）:

- 節の見出し（``#`` で始まる行に、この順で）: 「取り込む前に人が行うこと」「診断」「変更点」「検査の結果」
  「修正役の説明」（同じ行に「モデルの説明・未検証」）「確かめていないこと」「足取り」
- 冒頭: 「steering」「Codex」「main への取り込みは人が行う」、
  「再現テストが、実際に起きた失敗と同じ原因を突いているかは確かめていません」
  「最後の確認は、このプルリクエストを読む人が行ってください」
- 確かめていないこと: 「実機での確認は未実施」「診断の場所が正しいかは未確認」
  「再現テストが、実際に起きた失敗と同じ原因を突いているかは未確認」「既存のテストに無い経路への影響は未確認」
- ``rejected`` のとき: 「コミットなし」
"""

import pytest

from ..agent.diagnosis.factories import fake_tokens
from . import repo_factory
from .fakes import EXPLANATION, GATE_ORDER, SECTIONS, gate_detail, heading_positions, make_fix_run, section
from .repo_factory import BRANCH, EXECUTION_ID, NEW_TEST, SRC

DIFF_STAT = [(SRC, 2, 0), (NEW_TEST, 5, 0)]
RARE = "〓"


def handoff(tmp_path, **overrides):
    from src.fixer.handoff_input import load_handoff

    path = repo_factory.write_handoff(tmp_path / "handoff.json", repo_factory.handoff_dict(**overrides))
    return load_handoff(path)


def render(tmp_path, run=None, diff_stat=None) -> str:
    from src.fixer.draft import render_pr_draft

    stat = DIFF_STAT if diff_stat is None else diff_stat
    return render_pr_draft(make_fix_run() if run is None else run, handoff(tmp_path), stat)


def plain(text: str) -> str:
    """強調の記号を除いた本文（固定の文面を、記号の付け方に依らずに探すため）。"""
    return text.replace("*", "").replace("_", "")


def rejected_run(**overrides):
    from src.fixer.models import GateResult

    gates = [GateResult(gate=gate, passed=gate != "G5", detail=gate_detail(gate)) for gate in GATE_ORDER]
    fields = {"status": "rejected", "stop_detail": "G5", "branch": None, "commit": None, "gates": gates}
    fields.update(overrides)
    return make_fix_run(**fields)


class TestSections:
    def test_render_pr_draft_has_all_sections_in_the_fixed_order(self, tmp_path):
        """AC-23: R15 の節がすべてある（順序は固定）。"""
        draft = render(tmp_path)

        positions = heading_positions(draft)

        assert all(position >= 0 for position in positions), dict(zip(SECTIONS, positions, strict=True))
        assert positions == sorted(positions)
        assert len(set(positions)) == len(SECTIONS)

    def test_render_pr_draft_starts_with_what_a_human_must_do_before_merging(self, tmp_path):
        """AC-23・R16: 冒頭に、取り込む前に人が行うことの固定の文面がある。"""
        draft = render(tmp_path)

        opening = plain(section(draft, "取り込む前に人が行うこと"))

        assert heading_positions(draft)[0] < 200
        for phrase in ("steering", "Codex", "main への取り込みは人が行う"):
            assert phrase in opening, phrase

    def test_render_pr_draft_states_the_limit_of_the_automatic_checks_in_the_opening(self, tmp_path):
        """AC-34: 「自動の検査は、再現テストが実際の失敗と同じ原因を突いていることまでは確かめていない。
        最後の確認は人が行う」という趣旨の固定の文面。"""
        draft = render(tmp_path)

        opening = plain(section(draft, "取り込む前に人が行うこと"))

        assert "再現テストが、実際に起きた失敗と同じ原因を突いているかは確かめていません" in opening
        assert "最後の確認は、このプルリクエストを読む人が行ってください" in opening
        assert "自動の診断と自動の修正" in opening

    @pytest.mark.parametrize("status", ["proposed", "rejected"])
    def test_render_pr_draft_fixed_opening_does_not_depend_on_the_result(self, tmp_path, status):
        run = make_fix_run() if status == "proposed" else rejected_run()
        other = make_fix_run(explanation="別の説明", patch_kind="structural", changed_files=[SRC])

        assert section(render(tmp_path, run), "取り込む前に人が行うこと") == section(
            render(tmp_path, other), "取り込む前に人が行うこと"
        )

    def test_render_pr_draft_lists_what_was_not_verified(self, tmp_path):
        text = plain(section(render(tmp_path), "確かめていないこと"))

        for phrase in (
            "実機での確認は未実施",
            "診断の場所が正しいかは未確認",
            "再現テストが、実際に起きた失敗と同じ原因を突いているかは未確認",
            "既存のテストに無い経路への影響は未確認",
        ):
            assert phrase in text, phrase


class TestDiagnosisAndChanges:
    def test_render_pr_draft_diagnosis_section_shows_the_fixed_fields(self, tmp_path):
        text = section(render(tmp_path), "診断")

        for value in (EXECUTION_ID, "generating", "output_validation_failed", "ZeroDivisionError", SRC, "average", "9"):
            assert value in text, value

    def test_render_pr_draft_changes_section_shows_line_counts_branch_and_commits(self, tmp_path):
        """R15: 変更点（コードが出した差分の要約）。"""
        stat = [(SRC, 12, 3), (NEW_TEST, 45, 0)]

        text = section(render(tmp_path, diff_stat=stat), "変更点")

        source_line = next(line for line in text.splitlines() if SRC in line)
        test_line = next(line for line in text.splitlines() if NEW_TEST in line)
        assert "12" in source_line
        assert "3" in source_line.replace("12", "")
        assert "45" in test_line
        assert BRANCH in text
        assert "a" * 40 in text
        assert "b" * 40 in text

    def test_render_pr_draft_rejected_run_says_there_is_no_commit(self, tmp_path):
        """design.md §12: ``rejected`` のときも下書きを書く（変更点は「コミットなし」）。"""
        text = section(render(tmp_path, rejected_run()), "変更点")

        assert "コミットなし" in text
        assert BRANCH not in text


class TestGateResults:
    def test_render_pr_draft_shows_every_gate_with_the_detail_from_the_run(self, tmp_path):
        """AC-23: 検査の結果は、コードが実行した値（``FixRun.gates``）と一致する。"""
        text = section(render(tmp_path), "検査の結果")

        for gate in GATE_ORDER:
            line = next((line for line in text.splitlines() if gate_detail(gate) in line), None)
            assert line is not None, gate
            assert gate in line

    def test_render_pr_draft_marks_failed_gates_differently_from_passed_ones(self, tmp_path):
        passed_text = section(render(tmp_path), "検査の結果")
        failed_text = section(render(tmp_path, rejected_run()), "検査の結果")

        def line_of(text: str, gate: str) -> str:
            return next(line for line in text.splitlines() if gate_detail(gate) in line)

        assert line_of(passed_text, "G5") != line_of(failed_text, "G5")
        assert line_of(passed_text, "G1") == line_of(failed_text, "G1")

    def test_render_pr_draft_only_shows_gates_that_were_run(self, tmp_path):
        from src.fixer.models import GateResult

        static_only = [GateResult(gate=gate, passed=gate != "G1", detail=gate_detail(gate)) for gate in GATE_ORDER[:6]]
        run = rejected_run(stop_detail="G1", gates=static_only)

        text = section(render(tmp_path, run), "検査の結果")

        assert gate_detail("G1") in text
        assert gate_detail("G5") not in text


class TestModelExplanation:
    def test_render_pr_draft_marks_the_explanation_as_unverified_model_text(self, tmp_path):
        """AC-23: モデルの説明には、「未検証」の明示がある。"""
        draft = render(tmp_path, make_fix_run(patch_kind="structural"))

        heading = next(line for line in draft.splitlines() if line.lstrip().startswith("#") and "修正役の説明" in line)
        text = section(draft, "修正役の説明")

        assert "モデルの説明・未検証" in heading
        assert EXPLANATION in text
        assert "structural" in text

    def test_render_pr_draft_explanation_appears_only_in_its_own_section(self, tmp_path):
        mark = "EXPLANATION_ONLY_MARK_2210"
        draft = render(tmp_path, make_fix_run(explanation=f"説明 {mark}"))

        assert draft.count(mark) == 1
        assert mark in section(draft, "修正役の説明")

    def test_render_pr_draft_cuts_the_explanation_to_the_length_limit(self, tmp_path):
        """AC-23: モデルの説明は、長さの上限内（800 字）。"""
        draft = render(tmp_path, make_fix_run(explanation=RARE * 5_000))

        assert 1 <= draft.count(RARE) <= 800

    @pytest.mark.parametrize("kind", list(fake_tokens()))
    def test_render_pr_draft_masks_token_shaped_strings_in_the_explanation(self, tmp_path, kind):
        token = fake_tokens()[kind]

        draft = render(tmp_path, make_fix_run(explanation=f"鍵は {token} です"))

        assert token not in draft

    def test_render_pr_draft_removes_control_characters_from_the_explanation(self, tmp_path):
        draft = render(tmp_path, make_fix_run(explanation="前\x1b[31m赤\x00\x07\x08後\x7f\x9b"))

        for char in ("\x1b", "\x00", "\x07", "\x08", "\x7f", "\x9b"):
            assert char not in draft, repr(char)
        assert "前" in draft
        assert "後" in draft

    def test_render_pr_draft_empty_explanation_still_renders_every_section(self, tmp_path):
        draft = render(tmp_path, make_fix_run(explanation="", patch_kind="unspecified"))

        assert all(position >= 0 for position in heading_positions(draft))
        assert "unspecified" in section(draft, "修正役の説明")


class TestTrace:
    def test_render_pr_draft_shows_the_trace_in_order_with_test_counts(self, tmp_path):
        """AC-24 の部品: 足取りが同じ順で並び、テスト実行には件数がある。"""
        text = section(render(tmp_path), "足取り")

        tools = ["read_file", "write_file", "run_tests", "replace_in_file", "run_tests", "finish"]
        cursor = 0
        for tool in tools:
            cursor = text.index(tool, cursor) + len(tool)
        run_lines = [line for line in text.splitlines() if "run_tests" in line]
        assert len(run_lines) == 2
        assert "0" in run_lines[0] and "1" in run_lines[0]
        assert NEW_TEST in run_lines[0]
        assert SRC in text

    def test_render_pr_draft_shows_refused_calls_with_their_outcome(self, tmp_path):
        from src.fixer.models import FixToolCall

        trace = [
            FixToolCall(seq=1, tool="write_file", path=None, outcome="denied", duration_ms=1),
            FixToolCall(seq=2, tool="unknown", path=None, outcome="invalid_input", duration_ms=1),
            FixToolCall(seq=3, tool="run_tests", path=None, outcome="call_limit", duration_ms=1),
        ]

        text = section(render(tmp_path, make_fix_run(trace=trace)), "足取り")

        for value in ("denied", "invalid_input", "call_limit", "unknown"):
            assert value in text, value

    def test_render_pr_draft_with_empty_trace_still_renders(self, tmp_path):
        draft = render(tmp_path, make_fix_run(trace=[]))

        assert all(position >= 0 for position in heading_positions(draft))
