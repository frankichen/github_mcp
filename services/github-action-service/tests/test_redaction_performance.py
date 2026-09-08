import pytest

from app.development_failure_pack import redact_text


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("token=secret-value", "token=[REDACTED]"),
        ("prefix-token-suffix=secret-value", "prefix-token-suffix=[REDACTED]"),
        ("prefix.token.suffix=secret-value", "prefix.token.suffix=[REDACTED]"),
        ("client-key=secret-value", "client-key=[REDACTED]"),
        ("headers=secret-value", "headers=[REDACTED]"),
    ],
)
def test_sensitive_assignment_redaction_keeps_key_and_redacts_value(raw, expected):
    assert redact_text(raw) == expected


def test_long_hyphenated_non_sensitive_text_is_preserved():
    raw = "evidence-" * 10_000

    assert redact_text(raw) == raw


def test_long_hyphenated_sensitive_key_still_redacts_value():
    prefix = "prefix-" * 10_000

    assert redact_text(prefix + "token=secret-value") == prefix + "token=[REDACTED]"
