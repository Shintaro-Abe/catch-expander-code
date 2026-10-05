"""マスク・識別子の除去・切り詰め・Slack 向けの無効化（design.md §6）。

対応: AC-10（マスク）、AC-1（識別子の除去）、AC-3（切り詰め）、AC-21・R25（メンションの無効化）、
AC-25（ソケットの禁止が効いていること）。
"""

import asyncio
import socket

import pytest

from .conftest import NetworkBlockedError
from .factories import adversarial_inputs, bearer_value, extra_secrets, fake_tokens, more_secrets

DROPPED_KEYS = (
    "user_id",
    "user_id_hash",
    "slack_channel",
    "slack_thread_ts",
    "channel_id",
    "thread_ts",
    "ttl",
    "gsi_pk",
)


# ---------------------------------------------------------------------------
# AC-25: テストの土台
# ---------------------------------------------------------------------------


class TestNetworkGuard:
    def test_create_connection_when_called_raises(self):
        with pytest.raises(NetworkBlockedError):
            socket.create_connection(("example.invalid", 443))

    def test_getaddrinfo_when_called_raises(self):
        with pytest.raises(NetworkBlockedError):
            socket.getaddrinfo("example.invalid", 443)

    def test_socket_connect_when_called_raises(self):
        sock = socket.socket()
        try:
            with pytest.raises(NetworkBlockedError):
                sock.connect(("example.invalid", 443))
        finally:
            sock.close()

    def test_asyncio_run_when_sockets_blocked_still_works(self):
        async def _answer() -> int:
            return await asyncio.to_thread(lambda: 42)

        assert asyncio.run(_answer()) == 42

    def test_environment_when_test_runs_has_no_credentials(self):
        import os

        for name in ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "ANTHROPIC_API_KEY", "CLAUDE_CODE_OAUTH_TOKEN"):
            assert name not in os.environ


# ---------------------------------------------------------------------------
# AC-10: マスク
# ---------------------------------------------------------------------------


class TestMaskSecrets:
    @pytest.mark.parametrize("kind", sorted(fake_tokens()))
    def test_mask_secrets_with_token_shaped_string_replaces_it(self, kind):
        from diagnosis.redaction import mask_secrets

        token = fake_tokens()[kind]
        masked = mask_secrets(f"前の文 {token} 後の文")

        assert token not in masked
        assert "[MASKED]" in masked
        assert masked.startswith("前の文 ")
        assert masked.endswith(" 後の文")

    @pytest.mark.parametrize("separator", [" ", "  ", "\t"])
    def test_mask_secrets_with_bearer_value_replaces_it(self, separator):
        from diagnosis.redaction import mask_secrets

        masked = mask_secrets(f"Authorization: Bearer{separator}{bearer_value()} end")

        assert bearer_value() not in masked
        assert "[MASKED]" in masked
        assert masked.endswith(" end")

    def test_mask_secrets_with_several_tokens_replaces_all(self):
        from diagnosis.redaction import mask_secrets

        tokens = fake_tokens()
        masked = mask_secrets(" / ".join(tokens.values()))

        for token in tokens.values():
            assert token not in masked
        assert masked.count("[MASKED]") == len(tokens)

    def test_mask_secrets_with_token_in_json_text_replaces_it(self):
        from diagnosis.redaction import mask_secrets

        token = fake_tokens()["slack"]
        masked = mask_secrets('{"token": "' + token + '", "ok": true}')

        assert token not in masked
        assert '"ok": true' in masked

    @pytest.mark.parametrize(
        "text",
        [
            "",
            "普通の日本語の文です。",
            "exec-20261004120000-abcdef12",
            "xo" + "xb-short",  # 10 字に満たない
            "gh" + "p_tooshort",
            "AK" + "IA" + "TOOSHORT",
            "Bearer short",
            "secret_value と書いただけの文",
            'File "/app/orchestrator.py", line 2211, in _run_text_generator_with_retries',
        ],
    )
    def test_mask_secrets_with_ordinary_text_keeps_it(self, text):
        from diagnosis.redaction import mask_secrets

        assert mask_secrets(text) == text


# ---------------------------------------------------------------------------
# AC-1: 識別子の除去
# ---------------------------------------------------------------------------


class TestDropIdentifiers:
    @pytest.mark.parametrize("key", DROPPED_KEYS)
    def test_drop_identifiers_with_identifier_key_removes_it(self, key):
        from diagnosis.redaction import drop_identifiers

        cleaned = drop_identifiers({key: "IDENTIFIER", "topic": "残る"})

        assert cleaned == {"topic": "残る"}

    def test_drop_identifiers_with_nested_structures_removes_keys_at_every_depth(self):
        from diagnosis.redaction import drop_identifiers

        cleaned = drop_identifiers(
            {
                "status": "failed",
                "payload": {"channel_id": "C1", "thread_ts": "1.2", "detail": {"user_id_hash": "h", "keep": 1}},
                "items": [{"user_id": "U1", "name": "a"}, {"slack_channel": "C2", "name": "b"}],
            }
        )

        assert cleaned == {
            "status": "failed",
            "payload": {"detail": {"keep": 1}},
            "items": [{"name": "a"}, {"name": "b"}],
        }

    def test_drop_identifiers_with_scalar_returns_it_unchanged(self):
        from diagnosis.redaction import drop_identifiers

        assert drop_identifiers("text") == "text"
        assert drop_identifiers(3) == 3
        assert drop_identifiers(None) is None


class TestRedact:
    def test_redact_with_identifiers_and_tokens_drops_and_masks(self):
        from diagnosis.redaction import redact

        token = fake_tokens()["github"]
        cleaned = redact(
            {
                "user_id": "U0123ABCDE",
                "payload": {"error_message": f"push failed with {token}", "thread_ts": "1.2"},
                "lines": [f"token={token}", "ok"],
                "count": 3,
            }
        )

        assert "user_id" not in cleaned
        assert "thread_ts" not in cleaned["payload"]
        assert token not in cleaned["payload"]["error_message"]
        assert "[MASKED]" in cleaned["payload"]["error_message"]
        assert token not in cleaned["lines"][0]
        assert cleaned["lines"][1] == "ok"
        assert cleaned["count"] == 3


# ---------------------------------------------------------------------------
# AC-3・AC-4: 切り詰め
# ---------------------------------------------------------------------------


class TestTruncateMiddle:
    def test_truncate_middle_with_short_text_returns_it_unchanged(self):
        from diagnosis.redaction import truncate_middle

        assert truncate_middle("short", 300, 500) == ("short", False)

    def test_truncate_middle_with_text_exactly_at_limit_returns_it_unchanged(self):
        from diagnosis.redaction import truncate_middle

        text = "a" * 800
        assert truncate_middle(text, 300, 500) == (text, False)

    def test_truncate_middle_with_long_text_keeps_head_and_tail_with_marker(self):
        from diagnosis.redaction import truncate_middle

        text = "H" * 300 + "M" * 1200 + "T" * 500
        truncated, was_truncated = truncate_middle(text, 300, 500)

        assert was_truncated is True
        assert truncated.startswith("H" * 300)
        assert truncated.endswith("T" * 500)
        assert "M" not in truncated
        assert "字省略" in truncated
        assert "1200" in truncated  # 省略した字数
        assert len(truncated) < len(text)

    def test_truncate_middle_with_one_char_over_limit_marks_truncation(self):
        from diagnosis.redaction import truncate_middle

        text = "H" * 300 + "M" + "T" * 500
        truncated, was_truncated = truncate_middle(text, 300, 500)

        assert was_truncated is True
        assert "字省略" in truncated
        assert truncated.startswith("H" * 300)
        assert truncated.endswith("T" * 500)


# ---------------------------------------------------------------------------
# AC-21・R25: Slack 向けの無効化
# ---------------------------------------------------------------------------


class TestNeutralizeSlack:
    @pytest.mark.parametrize(
        "text",
        ["<@U0123ABCDE> 見てください", "<!channel> 全員へ", "<!here>", "@here @channel", "<https://example.com|ここ>"],
    )
    def test_neutralize_slack_with_mention_or_link_removes_active_characters(self, text):
        from diagnosis.redaction import neutralize_slack

        neutralized = neutralize_slack(text)

        assert "<" not in neutralized
        assert ">" not in neutralized
        assert "@" not in neutralized

    def test_neutralize_slack_with_special_characters_uses_fullwidth_replacements(self):
        from diagnosis.redaction import neutralize_slack

        assert neutralize_slack("<@U1> a@b") == "＜＠U1＞ a＠b"

    def test_neutralize_slack_with_plain_text_keeps_it(self):
        from diagnosis.redaction import neutralize_slack

        assert neutralize_slack("レート制限に達しました (3 回)") == "レート制限に達しました (3 回)"


# ---------------------------------------------------------------------------
# レビュー後の修正仕様 L3: マスクの対象を足す。辞書のキーもマスクする
# ---------------------------------------------------------------------------


class TestMoreSecretShapes:
    @pytest.mark.parametrize("kind", sorted(extra_secrets()))
    def test_mask_secrets_with_added_secret_shape_replaces_it(self, kind):
        from diagnosis.redaction import mask_secrets

        secret, must_not_remain = extra_secrets()[kind]

        masked = mask_secrets(f"前の文 {secret} 後の文")

        for part in must_not_remain:
            assert part not in masked
        assert "[MASKED]" in masked
        assert masked.startswith("前の文")
        assert masked.endswith("後の文")

    @pytest.mark.parametrize("kind", sorted(extra_secrets()))
    def test_mask_secrets_with_added_secret_shape_inside_json_text_replaces_it(self, kind):
        from diagnosis.redaction import mask_secrets

        secret, must_not_remain = extra_secrets()[kind]

        masked = mask_secrets('{"value": "' + secret.replace("\n", "\\n") + '", "other": 1}')

        for part in must_not_remain:
            assert part.replace("\n", "\\n") not in masked
        assert '"other": 1' in masked

    def test_mask_secrets_with_all_added_shapes_in_one_text_replaces_all(self):
        from diagnosis.redaction import mask_secrets

        secrets = extra_secrets()
        text = "\n".join(f"{kind}: {secret}" for kind, (secret, _) in secrets.items())

        masked = mask_secrets(text)

        for kind, (_, must_not_remain) in secrets.items():
            assert f"{kind}: " in masked  # 前後の文は残る
            for part in must_not_remain:
                assert part not in masked

    @pytest.mark.parametrize(
        "text",
        [
            "JWT は eyJ で始まる 3 区切りの文字列です",
            "xapp という接頭辞のトークン",
            "AIza で始まるキー",
            "Slack の Webhook は hooks.slack.com で受ける",
            "-----BEGIN CERTIFICATE-----",
            "a.b.c と x.y.z",
            "version 1.2.3",
        ],
    )
    def test_mask_secrets_with_text_that_only_mentions_the_shapes_keeps_it(self, text):
        from diagnosis.redaction import mask_secrets

        assert mask_secrets(text) == text


class TestRedactKeys:
    def test_redact_masks_secret_in_dictionary_key(self):
        from diagnosis.redaction import redact

        token = fake_tokens()["github"]

        result = redact({token: "value", "plain": 1})

        assert token not in json_text(result)
        assert "[MASKED]" in json_text(result)
        assert result["plain"] == 1
        assert "value" in result.values()

    def test_redact_masks_secret_in_nested_dictionary_keys(self):
        from diagnosis.redaction import redact

        token = fake_tokens()["slack"]
        webhook, must_not_remain = extra_secrets()["slack_webhook"]

        result = redact({"outer": [{f"Authorization {token}": {webhook: ["x"]}}]})

        assert token not in json_text(result)
        for part in must_not_remain:
            assert part not in json_text(result)
        assert json_text(result).count("[MASKED]") == 2

    def test_redact_still_drops_identifier_keys_and_masks_values(self):
        from diagnosis.redaction import redact

        token = fake_tokens()["github"]

        result = redact({"user_id": "U0123ABCDE", "message": f"token={token}", "count": 2})

        assert result == {"message": "token=[MASKED]", "count": 2}

    def test_redact_keeps_non_string_keys(self):
        from diagnosis.redaction import redact

        assert redact({1: "a", "b": 2}) == {1: "a", "b": 2}


def json_text(value) -> str:
    import json

    return json.dumps(value, ensure_ascii=False)


class TestPemWithoutEnd:
    """秘密鍵の PEM ブロックに END の行が無い（途中で切れた）ときは、文字列の終わりまでをマスクする。"""

    BODY = "MIIEvQIBADANBgkqhkiG9w0BAQEFAASCBKcwggSj" + "\n" + "Zm9vYmFyYmF6cXV4" * 4

    def _begin(self, label: str = "RSA PRIVATE KEY") -> str:
        return "-----BEGIN " + label + "-----"

    @pytest.mark.parametrize("label", ["RSA PRIVATE KEY", "PRIVATE KEY", "EC PRIVATE KEY", "OPENSSH PRIVATE KEY"])
    def test_pem_block_cut_before_the_end_line_is_masked_to_the_end_of_text(self, label):
        from diagnosis.redaction import mask_secrets

        masked = mask_secrets(f"鍵はこれ: {self._begin(label)}\n{self.BODY}")

        assert "Zm9vYmFyYmF6cXV4" not in masked
        assert "MIIEvQIBADANBgkqhkiG9w0BAQEFAASCBKcwggSj" not in masked
        assert masked.startswith("鍵はこれ: ")
        assert "[MASKED]" in masked

    def test_pem_block_with_a_broken_end_line_is_masked_to_the_end_of_text(self):
        from diagnosis.redaction import mask_secrets

        masked = mask_secrets(f"{self._begin()}\n{self.BODY}\n-----END RSA PRIV")

        assert "Zm9vYmFyYmF6cXV4" not in masked

    def test_pem_block_with_end_line_keeps_the_text_after_it(self):
        from diagnosis.redaction import mask_secrets

        end = "-----END RSA PRIVATE KEY-----"
        masked = mask_secrets(f"前 {self._begin()}\n{self.BODY}\n{end} 後ろの文")

        assert "Zm9vYmFyYmF6cXV4" not in masked
        assert masked.endswith(" 後ろの文")
        assert masked.startswith("前 ")

    def test_truncated_pem_in_event_payload_does_not_reach_the_model(self):
        from diagnosis.models import DiagnosisLimits
        from diagnosis.tools import ToolSession

        from . import fakes, scenarios

        scenario = scenarios.rate_limited()
        message = f"key dump: {self._begin()}\n{self.BODY}"
        scenario.events = [scenarios.make_event(scenario.execution_id, 1, "error", {"error_message": message})]
        session = ToolSession(fakes.build_reader(scenario).reader, DiagnosisLimits())

        assert "Zm9vYmFyYmF6cXV4" not in session.call("list_events", {}).to_text()


# ===========================================================================
# 再レビュー後の修正仕様 A5: マスクする形を足す
# ===========================================================================


class TestMoreMaskedShapes:
    @pytest.mark.parametrize("kind", sorted(more_secrets()))
    def test_added_shape_is_masked(self, kind):
        from diagnosis.redaction import mask_secrets

        secret, must_not_remain = more_secrets()[kind]

        masked = mask_secrets(f"前の文 {secret} 後の文")

        for part in must_not_remain:
            assert part not in masked
        assert "[MASKED]" in masked
        assert masked.startswith("前の文")
        assert masked.endswith("後の文")

    @pytest.mark.parametrize("kind", sorted(more_secrets()))
    def test_added_shape_is_masked_at_the_edges_of_the_text_and_between_lines(self, kind):
        from diagnosis.redaction import mask_secrets

        secret, must_not_remain = more_secrets()[kind]

        for text in (secret, f"{secret}\n次の行", f"前の行\n{secret}"):
            masked = mask_secrets(text)
            for part in must_not_remain:
                assert part not in masked

    def test_all_added_shapes_in_one_text_are_masked(self):
        from diagnosis.redaction import mask_secrets

        secrets = more_secrets()
        text = "\n".join(f"{kind} => {secret}" for kind, (secret, _) in secrets.items())

        masked = mask_secrets(text)

        for kind, (_, must_not_remain) in secrets.items():
            assert f"{kind} => " in masked
            for part in must_not_remain:
                assert part not in masked

    def test_host_of_url_with_credentials_is_kept(self):
        """URL の中の認証情報は、password の部分をマスクする（どこへの接続かは、診断の材料として残す）。"""
        from diagnosis.redaction import mask_secrets

        secret, _ = more_secrets()["url_credentials"]

        masked = mask_secrets(f"接続に失敗: {secret}")

        assert "db.example.com" in masked
        assert masked.startswith("接続に失敗: ")

    def test_jwt_cut_in_the_third_part_is_still_masked_by_the_first_two(self):
        from diagnosis.redaction import mask_secrets

        secret, _ = extra_secrets()["jwt"]
        header, payload, _signature = secret.split(".")

        for text in (f"{header}.{payload}", f"{header}.{payload}.", f"{header}.{payload}.Sfl"):
            masked = mask_secrets(f"token={text}")
            assert payload not in masked
            assert header not in masked

    @pytest.mark.parametrize(
        "text",
        [
            "task-oriented design",
            "risk-based approach",
            "ask-me-anything about this",
            "mask-secrets-in-every-tool-result-first",  # sk- の後ろに 20 字以上続くが、語の途中
            "desk-top publishing and disk-based storage systems",
            "eyJ",
            "eyJhbGciOi",  # 区切りの無い短い文字列
            "the eyJ prefix means a base64 encoded JSON object",
            "https://example.com/path",
            "https://example.com/path?x=1&y=2#fragment",
            "https://example.com:8443/path",  # ポートの : はあるが、認証情報は無い
            "http://a.example:8080/x see me" + "@b.example",  # : と @ が、別の語にある
            "contact: admin@example.com",
            "ratio 3:2 @ 60fps",
            "Basic usage of the tool",
            "Basic",
            "Authorization: Basic",  # 値が無い
            "the bearer of this message",
            "Bearer token is missing",
            "xoxo and xox are not tokens",
            "glpat- is the GitLab prefix",
            "ghp_ is the GitHub prefix",
            "aws_secret_access_key is not set",
            "set aws_secret_access_key in the environment",
            "日本語の普通の文です。sk- や eyJ という接頭辞の説明。",
        ],
    )
    def test_ordinary_text_is_kept(self, text):
        from diagnosis.redaction import mask_secrets

        assert mask_secrets(text) == text

    def test_added_shapes_are_masked_in_tool_results(self):
        from diagnosis.models import DiagnosisLimits
        from diagnosis.tools import ToolSession

        from . import fakes, scenarios

        secrets = more_secrets()
        scenario = scenarios.rate_limited()
        scenario.events = [
            scenarios.make_event(scenario.execution_id, n, "error", {"error_message": f"failed with {secret}"})
            for n, (secret, _) in enumerate(secrets.values(), start=1)
        ]
        session = ToolSession(fakes.build_reader(scenario).reader, DiagnosisLimits())

        text = session.call("list_events", {}).to_text()

        for _, must_not_remain in secrets.values():
            for part in must_not_remain:
                assert part not in text


# ===========================================================================
# 再レビュー後の修正仕様 A1: マスクは、入力の長さに対して、ほぼ線形の時間で終わる
# ===========================================================================
#
# 時間で確かめるテストを安定させるための作り:
# - 大きさを 32KB から 1MB まで倍々に上げ、各段で予算（2 秒）を超えたら、その場で失敗にする。
#   二乗の時間の実装でも、分単位で止まらずに、数秒で失敗する（1MB をいきなり通さない）。
# - 線形の実装なら 1MB で数十ミリ秒なので、予算には数十倍の余裕がある（遅い機械でも落ちない）。
# - 併せて、倍にしたときの時間の比が 3 倍を超えないことを見る。ただし、短すぎる時間は比が安定しないので、
#   前の段が 0.05 秒以上かかったときだけ比べる（線形の実装は、そもそもここに届かない）。
#   比が超えたときは、両方を測り直して（3 回の最小値）、それでも超えるときだけ失敗にする。

STAGES = (32_000, 64_000, 128_000, 256_000, 512_000, 1_000_000)
BUDGET_SECONDS = 2.0
RATIO_FLOOR_SECONDS = 0.05
MAX_RATIO = 3.0


def _elapsed(action, value, repeats: int = 1) -> float:
    import time

    best = float("inf")
    for _ in range(repeats):
        started = time.perf_counter()
        action(value)
        best = min(best, time.perf_counter() - started)
    return best


def check_roughly_linear(build, action, *, stages=STAGES, budget: float = BUDGET_SECONDS) -> None:
    previous_time, previous_input = None, None
    for size in stages:
        value = build(size)
        elapsed = _elapsed(action, value)
        assert elapsed <= budget, f"{size:,} 字で {elapsed:.2f} 秒かかった（予算 {budget} 秒）"
        if previous_time is not None and previous_time >= RATIO_FLOOR_SECONDS and elapsed > previous_time * MAX_RATIO:
            again, before = _elapsed(action, value, 3), _elapsed(action, previous_input, 3)
            assert again <= before * MAX_RATIO, (
                f"大きさを倍にしたら {again / before:.1f} 倍の時間になった（{size:,} 字で {again:.2f} 秒）"
            )
        previous_time, previous_input = elapsed, value


class TestMaskingTime:
    @pytest.mark.parametrize("kind", sorted(adversarial_inputs()))
    def test_mask_secrets_finishes_one_megabyte_of_adversarial_input_in_time(self, kind):
        from diagnosis.redaction import mask_secrets

        check_roughly_linear(adversarial_inputs()[kind], mask_secrets)

    @pytest.mark.parametrize("kind", ["jwt_prefix_repeated", "jwt_brief_dotted_repeated", "bearer_repeated"])
    def test_redact_of_nested_values_and_keys_finishes_in_time(self, kind):
        from diagnosis.redaction import redact

        build = adversarial_inputs()[kind]

        def nested(size: int) -> dict:
            text = build(size // 4)
            return {text: [text, {"inner": text}], "plain": text}

        check_roughly_linear(nested, redact)

    @pytest.mark.parametrize(
        "kind", ["jwt_prefix_repeated", "jwt_brief_dotted_repeated", "url_user_repeated", "bearer_lower_repeated"]
    )
    def test_get_prompt_record_with_adversarial_prompt_finishes_in_time(self, kind):
        """プロンプト記録の上限は 1MB。マスクは、切り詰めの前に全文へ掛かる。"""
        import json

        from diagnosis.models import DiagnosisLimits
        from diagnosis.tools import ToolSession

        from . import fakes, scenarios

        build = adversarial_inputs()[kind]

        def session_for(size: int):
            scenario = scenarios.output_validation_failed()
            key = f"prompts/{scenario.execution_id}/researcher_big.json"
            body = json.dumps({"prompt": build(size // 2), "output": build(size // 2)}).encode("utf-8")
            assert len(body) <= 1_000_000
            bundle = fakes.build_reader(scenario, raw_objects={(fakes.PROMPTS_BUCKET, key): body})
            return ToolSession(bundle.reader, DiagnosisLimits())

        def read(session) -> None:
            result = session.call("get_prompt_record", {"subagent": "researcher", "index": "big"})
            assert result.outcome == "ok"

        stages = (32_000, 64_000, 128_000, 256_000, 512_000, 990_000)
        check_roughly_linear(session_for, read, stages=stages, budget=3.0)

    @pytest.mark.parametrize("kind", ["jwt_prefix_repeated", "jwt_brief_dotted_repeated", "url_user_repeated"])
    def test_list_events_with_adversarial_payload_finishes_in_time(self, kind):
        from diagnosis.models import DiagnosisLimits
        from diagnosis.tools import ToolSession

        from . import fakes, scenarios

        build = adversarial_inputs()[kind]

        def session_for(size: int):
            scenario = scenarios.rate_limited()
            payload = {"error_message": build(size // 2), "stack_trace": build(size // 2)}
            scenario.events = [scenarios.make_event(scenario.execution_id, 1, "error", payload)]
            return ToolSession(fakes.build_reader(scenario).reader, DiagnosisLimits())

        def read(session) -> None:
            assert session.call("list_events", {}).outcome == "ok"

        check_roughly_linear(session_for, read, budget=3.0)

    def test_submission_with_adversarial_free_text_is_bounded_by_its_length_limit(self):
        """提出の自由記述は 500 字・300 字までなので、時間の問題にならない（上限が効いていることを確かめる）。"""
        from diagnosis.models import DiagnosisLimits
        from diagnosis.tools import ToolSession

        from . import fakes, scenarios
        from .factories import valid_submission

        session = ToolSession(fakes.build_reader(scenarios.rate_limited()).reader, DiagnosisLimits())
        long_text = adversarial_inputs()["jwt_prefix_repeated"](100_000)

        result = session.call("submit_diagnosis", valid_submission(summary=long_text))

        assert result.outcome == "invalid_input"
