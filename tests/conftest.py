import subprocess

import pytest


@pytest.fixture
def instruction_repo(tmp_path):
    repo = tmp_path / "instructions-repo"
    repo.mkdir()

    def git(*args):
        return subprocess.run(
            ["git", "-C", str(repo), *args], check=True, capture_output=True, text=True
        ).stdout.strip()

    git("init", "-b", "main")
    git("config", "user.email", "test@example.invalid")
    git("config", "user.name", "Test")
    instructions = repo / "instructions"
    (instructions / "roles").mkdir(parents=True)
    (instructions / "roles" / "reader.md").write_text("Committed reader")
    (instructions / "lenses.toml").write_text("""
[[lenses]]
id = "preferences"
version = "1"
look_for = "Explicit preferences"
changes = ["knowledge"]
domains = ["software"]
projects = ["example"]
workflows = ["review"]
""")
    git("add", ".")
    git("commit", "-m", "instructions")
    return repo, git
