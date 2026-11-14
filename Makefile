.PHONY: test test-live lint format build install

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

install:
	uv tool install .
