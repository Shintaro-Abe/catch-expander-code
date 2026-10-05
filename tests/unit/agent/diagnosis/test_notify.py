"""Slack の投稿文（design.md §9.3）。

対応: AC-21（メンション記法の無効化）、R25（固定項目からコードが組み立てる、マスクを通す）。
"""

from unittest.mock import patch

import pytest

from . import fakes, scenarios
from .factories import VALID_EXECUTION_ID, bearer_value, fake_tokens, make_run, valid_submission
from .sdk_fake import FakeSdk

MENTIONS = "<@U0123ABCDE> <!channel> <!here> @here @channel <https://evil.example|ここを押す>"


def _assert_no_active_mention(text: str) -> None:
    for pattern in ("<@", "<!", "@here", "@channel", "<http", "|ここを押す>"):
        assert pattern not in text


class TestFormatSlackMessage:
    def test_format_slack_message_with_rate_limit_run_shows_japanese_labels(self):
        from diagnosis.notify import format_slack_message

        text = format_slack_message(make_run())

        assert "実行の診断" in text
        assert "調査中" in text  # failed_stage=researching（docs/glossary.md の状態名）
        assert "レート制限" in text  # cause_category=rate_limit
        assert "高" in text  # confidence=high
        assert "時間を置いて再投入" in text  # action_type=retry_later
        assert "レート制限に 3 回当たって失敗しました" in text  # summary
        assert "3 件" in text  # 有効な根拠の件数
        assert f"`{VALID_EXECUTION_ID}`" in text

    def test_format_slack_message_lists_inspected_records_in_order(self):
        from diagnosis.notify import format_slack_message

        text = format_slack_message(make_run())

        assert text.index("概要") < text.index("イベント") < text.rindex("提出")

    @pytest.mark.parametrize(
        ("field", "values_attr"),
        [
            ("failed_stage", "FAILED_STAGES"),
            ("cause_category", "CAUSE_CATEGORIES"),
            ("action_type", "ACTION_TYPES"),
        ],
    )
    def test_format_slack_message_translates_every_fixed_value(self, field, values_attr):
        """決まった値は日本語の対応表で表示する。生の値（英語の識別子）は投稿文に出ない。"""
        from diagnosis import models
        from diagnosis.notify import format_slack_message

        for value in getattr(models, values_attr):
            text = format_slack_message(make_run(**{field: value}))

            assert text
            if "_" in value:
                assert value not in text

    @pytest.mark.parametrize("confidence", ["high", "medium", "low"])
    def test_format_slack_message_translates_confidence(self, confidence):
        from diagnosis.notify import format_slack_message

        text = format_slack_message(make_run(confidence=confidence))

        assert {"high": "高", "medium": "中", "low": "低"}[confidence] in text

    def test_format_slack_message_neutralizes_mentions_in_summary_and_action_note(self):
        from diagnosis.notify import format_slack_message

        text = format_slack_message(make_run(summary=f"要約 {MENTIONS}", action_note=f"対処 {MENTIONS}"))

        _assert_no_active_mention(text)
        assert "＠here" in text
        assert "要約" in text

    def test_format_slack_message_does_not_include_evidence_notes_or_tool_arguments(self):
        from diagnosis.models import EvidenceRef
        from diagnosis.notify import format_slack_message

        run = make_run(evidence=[EvidenceRef(id="EV-2", note=f"EVIDENCE_NOTE_MARKER {MENTIONS}")])

        text = format_slack_message(run)

        _assert_no_active_mention(text)
        assert "EVIDENCE_NOTE_MARKER" not in text
        assert "1 件" in text

    def test_format_slack_message_with_no_evidence_shows_zero(self):
        from diagnosis.notify import format_slack_message

        text = format_slack_message(make_run(cause_category="insufficient_records", evidence=[]))

        assert "0 件" in text

    def test_format_slack_message_returns_plain_string(self):
        from diagnosis.notify import format_slack_message

        assert isinstance(format_slack_message(make_run()), str)


class TestEndToEndMessage:
    def test_message_built_from_accepted_submission_has_no_token_and_no_mention(self):
        """R25: 提出の自由記述に入ったトークンとメンションが、投稿文に残らない。"""
        from diagnosis import diagnose_execution
        from diagnosis.notify import format_slack_message

        tokens = fake_tokens()
        scenario = scenarios.rate_limited()
        bundle = fakes.build_reader(scenario)

        async def script(call):
            events = (await call("list_events", {"event_type": "rate_limit_hit"})).data["events"]
            await call(
                "submit_diagnosis",
                valid_submission(
                    evidence=[{"id": events[0]["evidence_id"], "note": "429"}],
                    summary=f"<!channel> {tokens['slack']} で失敗",
                    action_note=f"<@U999ZZZ999> Bearer {bearer_value()}",
                ),
            )

        with patch("diagnosis.runner._invoke_sdk", FakeSdk(script)):
            run = diagnose_execution(scenario.execution_id, reader=bundle.reader, emitter=fakes.FakeEmitter())

        text = format_slack_message(run)

        assert run.status == "completed"
        assert tokens["slack"] not in text
        assert bearer_value() not in text
        _assert_no_active_mention(text)
        assert scenario.execution_id in text
        for identifier in scenarios.SLACK_IDENTIFIERS:
            assert identifier not in text


# ---------------------------------------------------------------------------
# design.md §9.3 の文面、§17 の 18、レビュー後の修正仕様 M2
# ---------------------------------------------------------------------------

HEADING = "\U0001fa7a 実行の診断（自動・参考情報）"  # design.md §9.3 の見出し（先頭は聴診器の絵文字）
UNVERIFIED = "未検証"  # M2: モデルが書いた自由記述に付く印（表示の文言は実装が決める。この語を含むこと）


def message_lines(run) -> list[str]:
    from diagnosis.notify import format_slack_message

    return format_slack_message(run).splitlines()


def line_with(lines: list[str], text: str) -> str:
    found = [line for line in lines if text in line]
    assert len(found) == 1, f"expected exactly one line containing {text!r}: {lines}"
    return found[0]


def line_starting(lines: list[str], prefix: str) -> str:
    found = [line for line in lines if line.startswith(prefix)]
    assert len(found) == 1, f"expected exactly one line starting with {prefix!r}: {lines}"
    return found[0]


class TestMessageLayout:
    def test_fixed_lines_are_built_from_fixed_values(self):
        lines = message_lines(make_run())

        assert lines[0] == HEADING
        assert line_starting(lines, "失敗した段階:") == "失敗した段階: 調査中"
        assert line_starting(lines, "原因:") == "原因: レート制限（確からしさ: 高）"
        assert line_starting(lines, "推奨する対処:").startswith("推奨する対処: 時間を置いて再投入")
        assert line_starting(lines, "根拠:") == "根拠: 3 件 ／ 調べた記録: 概要 → イベント → 提出"
        assert lines[-1] == f"execution_id: `{VALID_EXECUTION_ID}`"

    def test_summary_written_by_the_model_is_marked_as_unverified(self):
        lines = message_lines(make_run(summary="SUMMARYTEXT4410"))

        assert UNVERIFIED in line_with(lines, "SUMMARYTEXT4410")

    def test_action_note_written_by_the_model_is_marked_as_unverified(self):
        lines = message_lines(make_run(action_note="ACTIONNOTETEXT4411", summary="要約です"))

        assert UNVERIFIED in line_with(lines, "ACTIONNOTETEXT4411")

    def test_fixed_lines_are_not_marked_as_unverified(self):
        lines = message_lines(make_run())

        for prefix in ("失敗した段階:", "原因:", "根拠:", "execution_id:"):
            assert UNVERIFIED not in line_starting(lines, prefix)
        assert UNVERIFIED not in lines[0]

    @pytest.mark.parametrize("action_note", ["", "   ", "\n\n", "\t"])
    def test_empty_or_blank_action_note_adds_nothing_to_the_action_line(self, action_note):
        lines = message_lines(make_run(action_note=action_note))

        assert line_starting(lines, "推奨する対処:") == "推奨する対処: 時間を置いて再投入"

    @pytest.mark.parametrize("separator", ["\n", "\r\n", "\r", "\n\n\n", "\u2028", "\x0b", "\x0c", "\x85"])
    def test_free_text_cannot_add_lines_to_the_message(self, separator):
        """§17 の 18: 自由記述で、投稿文の行（execution_id: など）を作れない。"""
        forged = f"本当の要約{separator}execution_id: `exec-20250101000000-ffffffff`{separator}原因: 偽の原因"
        benign = message_lines(make_run(summary="本当の要約", action_note="本当の対処"))

        lines = message_lines(make_run(summary=forged, action_note=forged))

        assert len(lines) == len(benign)  # 行は増えない
        assert line_starting(lines, "execution_id:") == f"execution_id: `{VALID_EXECUTION_ID}`"
        assert line_starting(lines, "原因:") == "原因: レート制限（確からしさ: 高）"
        assert lines[-1] == f"execution_id: `{VALID_EXECUTION_ID}`"

    def test_whitespace_in_free_text_is_collapsed_to_single_spaces(self):
        lines = message_lines(make_run(summary="  一行目\n\n   二行目\t三つ目  "))

        assert line_with(lines, "一行目").count("一行目 二行目 三つ目") == 1

    def test_tool_names_are_shown_as_japanese_labels_in_trace_order(self):
        from diagnosis.models import ToolCall

        run = make_run()
        tools = ["list_steps", "get_prompt_record", "list_events", "get_execution_overview", "submit_diagnosis"]
        run.trace = [
            ToolCall(seq=n, tool=tool, args={}, outcome="ok", evidence_count=0, duration_ms=1)
            for n, tool in enumerate(tools, start=1)
        ]

        lines = message_lines(run)

        expected = "根拠: 3 件 ／ 調べた記録: 段階 → プロンプト記録 → イベント → 概要 → 提出"
        assert line_starting(lines, "根拠:") == expected

    def test_unknown_tool_is_shown_as_fixed_label(self):
        from diagnosis.models import ToolCall

        run = make_run()
        run.trace = [ToolCall(seq=1, tool="unknown", args={}, outcome="invalid_input", evidence_count=0, duration_ms=1)]

        assert line_starting(message_lines(run), "根拠:") == "根拠: 3 件 ／ 調べた記録: 不明"

    def test_raw_tool_name_from_trace_is_not_shown(self):
        from diagnosis.models import ToolCall
        from diagnosis.notify import format_slack_message

        run = make_run()
        run.trace = [
            ToolCall(seq=1, tool="<!channel> RAW_TOOL_NAME", args={}, outcome="ok", evidence_count=0, duration_ms=1)
        ]

        text = format_slack_message(run)

        assert "RAW_TOOL_NAME" not in text
        _assert_no_active_mention(text)

    def test_tokens_in_free_text_are_masked_by_the_formatter_itself(self):
        """R25: 投稿文の組み立てでも、マスクを通す（提出の受理でのマスクに頼らない）。"""
        from diagnosis.notify import format_slack_message

        tokens = fake_tokens()
        run = make_run(summary=f"鍵 {tokens['aws_akia']}", action_note=f"Bearer {bearer_value()} を失効")

        text = format_slack_message(run)

        assert tokens["aws_akia"] not in text
        assert bearer_value() not in text
        assert text.count("[MASKED]") == 2

    def test_unknown_default_diagnosis_is_shown_with_fixed_labels(self):
        from diagnosis.models import Diagnosis

        run = make_run()
        run.diagnosis = Diagnosis.unknown()
        run.trace = []

        lines = message_lines(run)

        assert line_starting(lines, "失敗した段階:") == "失敗した段階: 不明"
        assert line_starting(lines, "原因:") == "原因: 不明（確からしさ: 低）"
        assert line_starting(lines, "推奨する対処:").startswith("推奨する対処: 人による調査")
        assert line_starting(lines, "根拠:").startswith("根拠: 0 件")

    @pytest.mark.parametrize(
        ("field", "value", "prefix", "expected"),
        [
            ("failed_stage", "none", "失敗した段階:", "失敗した段階: 該当なし（実行は失敗していない）"),
            ("failed_stage", "analyzing", "失敗した段階:", "失敗した段階: 解析中"),
            ("failed_stage", "storing", "失敗した段階:", "失敗した段階: 格納中"),
            ("cause_category", "insufficient_records", "原因:", "原因: 記録の不足（確からしさ: 高）"),
            ("cause_category", "output_validation_failed", "原因:", "原因: 出力の検証失敗（確からしさ: 高）"),
            ("confidence", "medium", "原因:", "原因: レート制限（確からしさ: 中）"),
        ],
    )
    def test_each_fixed_value_is_shown_on_its_own_line(self, field, value, prefix, expected):
        assert line_starting(message_lines(make_run(**{field: value})), prefix) == expected

    @pytest.mark.parametrize(
        ("action_type", "label"), [("fix_code", "コードの修正"), ("no_action", "対処不要"), ("retry_now", "すぐ再投入")]
    )
    def test_action_type_is_shown_as_label_at_the_start_of_the_action_line(self, action_type, label):
        lines = message_lines(make_run(action_type=action_type))

        assert line_starting(lines, "推奨する対処:").startswith(f"推奨する対処: {label}")


class TestUrlsInFreeText:
    """M2: 自由記述の中の URL を無効化する（``://`` を ``[:]//`` に。Slack が自動でリンクにしないように）。"""

    @pytest.mark.parametrize(
        "url",
        ["https://evil.example/login?next=1", "http://evil.example", "HTTPS://EVIL.EXAMPLE/", "slack://open?team=T1"],
    )
    def test_url_in_summary_and_action_note_is_neutralized(self, url):
        from diagnosis.notify import format_slack_message

        text = format_slack_message(make_run(summary=f"詳しくは {url} を見て", action_note=f"{url} で再実行"))

        assert "://" not in text
        assert text.count(url.replace("://", "[:]//")) == 2

    def test_bare_scheme_separator_is_neutralized(self):
        from diagnosis.notify import format_slack_message

        text = format_slack_message(make_run(summary="a://b://c"))

        assert "://" not in text
        assert "a[:]//b[:]//c" in text

    def test_mentions_are_still_neutralized_together_with_urls(self):
        from diagnosis.notify import format_slack_message

        text = format_slack_message(make_run(summary=f"{MENTIONS} https://evil.example", action_note=MENTIONS))

        _assert_no_active_mention(text)
        assert "://" not in text
        assert "＠here" in text

    def test_message_without_urls_has_no_neutralization_marker(self):
        from diagnosis.notify import format_slack_message

        assert "[:]" not in format_slack_message(make_run())


# ---------------------------------------------------------------------------
# 再レビュー後の修正仕様 A6: 自由記述の、書式・不可視の文字を無効化する
# ---------------------------------------------------------------------------


def free_text_lines(summary: str, action_note: str = "対処の補足") -> tuple[str, str, list[str]]:
    """（要約の行, 対処の行, 全部の行）。自由記述には、行を見つけるための目印を付ける。"""
    lines = message_lines(make_run(summary=f"要約目印 {summary}", action_note=f"対処目印 {action_note}"))
    return line_with(lines, "要約目印"), line_with(lines, "対処目印"), lines


class TestFreeTextNeutralization:
    @pytest.mark.parametrize(
        ("character", "name"),
        [
            ("\u200b", "ZERO WIDTH SPACE"),
            ("\u200d", "ZERO WIDTH JOINER"),
            ("\u202e", "RIGHT-TO-LEFT OVERRIDE"),
            ("\u202a", "LEFT-TO-RIGHT EMBEDDING"),
            ("\u2066", "LEFT-TO-RIGHT ISOLATE"),
            ("\u2069", "POP DIRECTIONAL ISOLATE"),
            ("\u00ad", "SOFT HYPHEN"),
            ("\ufeff", "ZERO WIDTH NO-BREAK SPACE"),
            ("\u2060", "WORD JOINER"),
            ("\x07", "BELL"),
            ("\x1b", "ESCAPE"),
            ("\x00", "NULL"),
            ("\x7f", "DELETE"),
            ("\x9b", "CONTROL SEQUENCE INTRODUCER"),
        ],
    )
    def test_format_and_control_characters_are_removed(self, character, name):
        import unicodedata

        assert unicodedata.category(character) in ("Cf", "Cc"), name
        text = f"前{character}後 txt{character}.exe"

        summary_line, action_line, lines = free_text_lines(text, text)

        assert character not in "\n".join(lines)
        assert "前後" in summary_line
        assert "前後" in action_line

    def test_no_format_or_control_character_remains_anywhere_in_the_message(self):
        import unicodedata

        from diagnosis.notify import format_slack_message

        codes = (*range(0x00, 0x20), 0x7F, 0x85, 0x200B, 0x200E, 0x202E, 0x2066, 0xFEFF)
        hostile = "".join(chr(code) for code in codes)
        text = format_slack_message(make_run(summary=f"a{hostile}b", action_note=f"c{hostile}d"))

        remaining = [repr(ch) for ch in text if unicodedata.category(ch) in ("Cf", "Cc") and ch != "\n"]
        assert remaining == []
        assert len(text.splitlines()) == len(message_lines(make_run()))  # 行は増えない

    @pytest.mark.parametrize(
        ("text", "symbol", "words"),
        [
            ("*太字* にする", "*", ("太字", "にする")),
            ("_斜体_ にする", "_", ("斜体", "にする")),
            ("~打ち消し~ にする", "~", ("打ち消し", "にする")),
            ("`コード` にする", "`", ("コード", "にする")),
            ("```\nコードの塊\n```", "`", ("コードの塊",)),
            ("&lt;!channel&gt; と &amp;", "&", ("lt;", "channel", "gt;")),
            ("!here と !channel と !everyone", "!", ("here", "channel", "everyone")),
            ("#general と #random", "#", ("general", "random")),
        ],
    )
    def test_formatting_symbols_are_neutralized(self, text, symbol, words):
        summary_line, action_line, _ = free_text_lines(text, text)

        for line, marker in ((summary_line, "要約目印"), (action_line, "対処目印")):
            free_part = line[line.index(marker) :]
            assert symbol not in free_part
            for word in words:
                assert word in free_part  # 記号だけを置き換え、文は残す

    def test_neutralized_symbols_are_replaced_not_dropped(self):
        """記号は、見た目の近い別の文字に置き換える（消して、前後の語をつなげない）。"""
        summary_line, _, _ = free_text_lines("a*b_c~d`e&f!g#h")
        free_part = summary_line[summary_line.index("要約目印") :]

        assert "abcdefgh" not in free_part
        assert len(free_part) >= len("要約目印 a*b_c~d`e&f!g#h")

    @pytest.mark.parametrize(
        ("text", "expected", "forbidden"),
        [
            ("www.evil.example/path を見て", "www[.]evil.example/path", "www."),
            ("WWW.EVIL.EXAMPLE", "WWW[.]EVIL.EXAMPLE", "WWW."),
            ("tel:+81" + "312345678 に電話", "tel[:]+81" + "312345678", "tel:"),
            ("mailto:someone に連絡", "mailto[:]someone", "mailto:"),
            ("https://evil.example/x", "https[:]//evil.example/x", "://"),
            ("https://www.evil.example/x", "https[:]//www[.]evil.example/x", "www."),
        ],
    )
    def test_links_without_scheme_are_neutralized(self, text, expected, forbidden):
        summary_line, action_line, _ = free_text_lines(text, text)

        for line in (summary_line, action_line):
            assert expected in line
            assert forbidden not in line

    def test_mail_address_is_neutralized_by_scheme_and_at_sign(self):
        summary_line, _, _ = free_text_lines("mailto:admin@example.com")

        assert "mailto:" not in summary_line
        assert "@" not in summary_line
        assert "mailto[:]admin＠example.com" in summary_line

    def test_reviewer_reproduction_is_fully_neutralized(self):
        """窓口の再現: 書式・不可視の文字・リンクになる文字列が、そのまま通っていた。"""
        hostile = "*太字* _斜体_ `code` \u202e\u200b www.evil.example/path tel:110 !here #general &lt;!channel&gt;"

        summary_line, action_line, lines = free_text_lines(hostile, hostile)

        for line, marker in ((summary_line, "要約目印"), (action_line, "対処目印")):
            free_part = line[line.index(marker) :]
            for forbidden in ("*", "_", "`", "\u202e", "\u200b", "www.", "tel:", "!", "#", "&", "<", ">", "@"):
                assert forbidden not in free_part
        assert len(lines) == len(message_lines(make_run()))

    def test_fixed_lines_are_not_touched_by_the_neutralization(self):
        """コードが作る行には、処理をかけない（今までどおりの表示）。"""
        hostile = "*x* _y_ `z` !here #general &amp; www.a.example"
        benign = message_lines(make_run())

        lines = message_lines(make_run(summary=hostile, action_note=hostile))

        assert lines[0] == benign[0] == HEADING
        for prefix in ("失敗した段階:", "原因:", "根拠:", "execution_id:"):
            assert line_starting(lines, prefix) == line_starting(benign, prefix)
        assert lines[-1] == f"execution_id: `{VALID_EXECUTION_ID}`"  # バッククォートは、固定の行ではそのまま
        assert "→" in line_starting(lines, "根拠:")

    def test_plain_japanese_text_is_shown_as_it_is(self):
        text = "レート制限に 3 回当たって失敗しました。30 分ほど置いてください（目安）。"

        summary_line, _, _ = free_text_lines(text)

        assert text in summary_line

    def test_masked_token_marker_survives_the_neutralization(self):
        tokens = fake_tokens()

        summary_line, _, _ = free_text_lines(f"鍵 {tokens['aws_akia']} が漏れた")

        assert "[MASKED]" in summary_line
        assert tokens["aws_akia"] not in summary_line
