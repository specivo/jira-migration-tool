# Development tasks for specivo-jira-migration. See README.md.

.PHONY: install test lint fixture-bundle

FIXTURE_OUT ?= out/fixture-cloud

install:
	uv sync

test:
	uv run pytest

lint:
	uv run ruff check .
	uv run ruff format --check .
	uv run mypy --strict src

fixture-bundle:  ## Write and validate a bundle from the hand-written Cloud fixture to FIXTURE_OUT
	rm -rf $(FIXTURE_OUT)
	uv run python -m tests.write_fixture_bundle $(FIXTURE_OUT)
