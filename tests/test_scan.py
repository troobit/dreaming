"""The public-repository scan (9.12)."""

from gitkit import make_repo, out
from runkit import git

from dreaming import scan

QUOTE = "always write the release notes before the version is bumped, never after"
SECRET = "ghp_" + "a1B2" * 9


def staged(repo, files):
    """The staged diff after writing `files`."""
    for path, text in files.items():
        (repo / path).parent.mkdir(parents=True, exist_ok=True)
        (repo / path).write_text(text)
    git(repo, "add", "-A")
    return out(repo, "diff", "--cached", "--no-color")


def scanned(repo, diff, message="Prefer uv", names=(), quote_chars=40):
    return scan.scan(
        repo,
        "HEAD",
        diff,
        message,
        names=list(names),
        quotes=[("u1", QUOTE)],
        quote_chars=quote_chars,
    )


def test_only_added_lines_are_read_from_a_diff(tmp_path):
    repo = make_repo(tmp_path, "public", {"a.md": "keep\nold line\n"})
    diff = staged(repo, {"a.md": "keep\n++ starts with plus\n", "b.md": "new\n"})
    assert scan.added_text(diff) == {"a.md": "++ starts with plus", "b.md": "new"}


def test_a_credential_is_found_and_not_repeated(tmp_path):
    repo = make_repo(tmp_path, "public")
    problems = scanned(repo, staged(repo, {"rules/x.md": f"use {SECRET}\n"}))
    assert problems == ["a credential or token in rules/x.md"]


def test_removed_lines_are_not_scanned(tmp_path):
    repo = make_repo(tmp_path, "public", {"rules/x.md": f"use {SECRET}\n"})
    assert scanned(repo, staged(repo, {"rules/x.md": "use the keychain\n"})) == []


def test_listed_names_match_whole_words_in_any_case(tmp_path):
    repo = make_repo(tmp_path, "public")
    diff = staged(repo, {"a.md": "Work for ACME corp.\n", "b.md": "acme corporate\n"})
    problems = scanned(repo, diff, names=["Acme Corp"])
    assert problems == ["a name from the names list in a.md"]


def test_the_commit_message_is_scanned(tmp_path):
    repo = make_repo(tmp_path, "public")
    diff = staged(repo, {"a.md": "fine\n"})
    message = f"Prefer uv\n\nAs Acme asked: {QUOTE}"
    assert scanned(repo, diff, message, names=["acme"]) == [
        "a name from the names list in the commit message",
        "transcript text from u1 in the commit message",
    ]


def test_transcript_text_of_the_configured_length_is_found(tmp_path):
    repo = make_repo(tmp_path, "public")
    # Forty characters of the quote, wrapped differently.
    copied = QUOTE[7:47].replace(" ", "\n  ", 1)
    diff = staged(repo, {"a.md": f"-{copied}\n"})
    assert scanned(repo, diff) == ["transcript text from u1 in a.md"]
    assert scanned(repo, diff, quote_chars=41) == []


def test_transcript_text_already_in_the_repository_is_not_flagged(tmp_path):
    repo = make_repo(tmp_path, "public", {"NOTES.md": f"{QUOTE}\n"})
    diff = staged(repo, {"a.md": f"> {QUOTE}\n"})
    assert scanned(repo, diff) == []
