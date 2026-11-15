"""Public output checks, including messages and text added by generators."""

import re

from dreaming.redact import redact


def scan_output(text, *, transcripts=(), existing="", quote_chars=80, names=(), patterns=()):
    """Return category names only; never persist the matched private text."""
    reasons = set()
    if redact(text) != text or any(re.search(pattern, text) for pattern in patterns):
        reasons.add("credentials")
    if any(name.strip() and name.strip().casefold() in text.casefold() for name in names):
        reasons.add("listed_name")
    # Collapse layout whitespace so line wrapping cannot bypass the quote limit.
    output, baseline = " ".join(text.split()), " ".join(existing.split())
    for transcript in transcripts:
        source = " ".join(transcript.split())
        for index in range(max(0, len(source) - quote_chars)):
            fragment = source[index : index + quote_chars + 1]
            if fragment in output and fragment not in baseline:
                reasons.add("transcript_text")
                break
    return sorted(reasons)
