.PHONY: lint test test-live check install

lint:
	uv run ruff check .
	uv run ruff format --check .

test:
	uv run pytest

# Opt-in checks against the installed claude, codex, LM Studio and apsis;
# each test skips itself when its tool or login is absent.
test-live:
	uv run pytest -m live

check: lint test

# Installs the committed HEAD into ~/.dreaming/versions/<revision>/ and links
# ~/.local/bin/dream to the launcher (7.3).
install:
	uv run python -m dreaming.versions install
