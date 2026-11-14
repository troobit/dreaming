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

install:
	uv tool install --force .
