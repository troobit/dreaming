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
