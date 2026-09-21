set shell := ["zsh", "-cu"]

default: check

setup:
    uv sync --dev

lint:
    uv run ruff check .
    uv run ruff format --check .

test:
    uv run pytest

typecheck:
    uv run python -m compileall -q src tests

sync:
    PYTHONPATH=src uv run python -m jev_docs sync

validate:
    PYTHONPATH=src uv run python -m jev_docs validate

check: lint test typecheck validate

check-strict: check
    PYTHONPATH=src uv run python -m jev_docs validate --strict
