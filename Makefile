.PHONY: lint test test-live check build install

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

# Builds the wheel and source distribution into dist/ with the Python in
# .python-version.
build:
	uv build

# Installs the committed HEAD into ~/.dreaming/versions/<revision>/ and links
# ~/.local/bin/dream to the launcher (7.3).
install:
	uv run python -m dreaming.versions install
