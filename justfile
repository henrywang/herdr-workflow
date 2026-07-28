set positional-arguments := true

build:
    rm -rf dist
    uv build

run *args:
    uv run wq "$@"

test:
    uv run ruff format --check .
    uv run ruff check .
    uv run pyright
    uv run pytest -m "not integration"

release: test build
    uv publish dist/*
