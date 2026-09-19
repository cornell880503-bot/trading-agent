"""The auth errors that cost the most time to diagnose.

50119 and 50101 both read like "your key is wrong" and neither is: the first
means the key is on a different OKX regional entity than base_url points at,
the second means the key is live and the request went to demo. Each needs a
different fix, so each carries its own hint.
"""

import pytest

from okxbot.errors import ERROR_HINTS, OkxApiError


def test_a_code_with_a_hint_explains_the_fix_not_just_the_symptom():
    err = OkxApiError("50119", "API key doesn't exist", "/api/v5/account/balance")
    text = str(err)
    assert "50119" in text
    assert "hint:" in text
    assert "base_url" in text


def test_the_two_lookalike_auth_failures_give_different_advice():
    wrong_host = str(OkxApiError("50119", "API key doesn't exist"))
    wrong_env = str(OkxApiError("50101", "APIKey does not match current environment."))
    assert wrong_host != wrong_env
    assert "regional" in wrong_host
    assert "Demo Trading" in wrong_env


@pytest.mark.parametrize(
    "code, must_mention",
    [
        ("50110", "checkip"),
        ("50113", "CRLF"),
        ("50114", "passphrase"),
        ("50102", "timedatectl"),
        ("50111", "OKX_API_KEY"),
    ],
)
def test_each_hint_names_something_actionable(code, must_mention):
    assert must_mention in str(OkxApiError(code, "some message"))


def test_an_unknown_code_still_reports_cleanly():
    err = OkxApiError("59999", "brand new failure", "/api/v5/trade/order")
    assert err.hint is None
    assert "hint:" not in str(err)
    assert "59999" in str(err) and "/api/v5/trade/order" in str(err)


def test_the_original_fields_survive_for_programmatic_handling():
    err = OkxApiError("51006", "duplicate", "/api/v5/trade/order", {"x": 1})
    assert (err.code, err.msg, err.path, err.data) == ("51006", "duplicate", "/api/v5/trade/order", {"x": 1})


def test_every_hint_is_a_sentence_not_a_restatement_of_the_code():
    for code, hint in ERROR_HINTS.items():
        assert len(hint) > 40, f"{code} hint is too thin to act on"
        assert code not in hint, f"{code} hint just repeats the code"
