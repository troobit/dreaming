.PHONY: test test-live lint format build install install-dreaming

test:
	uv run pytest -m 'not live'

test-live:
	DREAMING_LIVE=1 uv run pytest -m live

lint:
	uv run ruff check .
	uv run ruff format --check .

format:
	uv run ruff format .
	uv run ruff check --fix .

build:
	uv build

install: install-dreaming

install-dreaming:
	uv run python -m dreaming.install --source . $(if $(DREAMING_BIN_DIR),--bin-dir "$(DREAMING_BIN_DIR)")
