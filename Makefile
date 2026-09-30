.PHONY: build run test

build:
	uv sync

run:
	uv run memo $(ARGS)

test:
	uv run pytest
