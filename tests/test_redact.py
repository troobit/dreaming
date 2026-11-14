import json

import pytest
from hypothesis import given
from hypothesis import strategies as st

from dreaming.redact import redact


@given(st.text())
def test_redaction_is_idempotent(text):
    assert redact(redact(text)) == redact(text)


def test_probe_diagnostics_scrub_credentials():
    text = 'Authorization: Bearer secret-value\n{"api_key": "private-value"}\nsk-' + "a" * 24
    scrubbed = redact(text)
    assert "secret-value" not in scrubbed
    assert "private-value" not in scrubbed
    assert "a" * 24 not in scrubbed


@pytest.mark.parametrize(
    "secret",
    [
        "AKIA" + "A" * 16,
        "ghp_" + "x" * 36,
        "xoxb-1234567890-abcde",
        "Bearer hidden-access-value",
        "postgres://username:hidden@db/",
        "-----BEGIN RSA PRIVATE KEY-----\nprivate material\n-----END RSA PRIVATE KEY-----",
    ],
)
def test_credential_categories_in_plaintext_and_json(secret):
    assert secret not in redact(secret)
    cleaned = redact(json.dumps({"quote": "example " + secret}))
    assert secret not in json.loads(cleaned)["quote"]
    assert redact(cleaned) == cleaned


@given(st.sampled_from(["api_key", "access_token", "password", "secret"]), st.text())
def test_json_credentials_remain_parseable_and_idempotent(key, value):
    raw = json.dumps({key: value, "observations": [{"quote": "safe"}]})
    clean = redact(raw)
    assert json.loads(clean)[key] == "[REDACTED]"
    assert redact(clean) == clean
