# Define the shell to use when executing commands
SHELL := /usr/bin/env bash -o pipefail -o errexit

install:	## Install dependencies (pulls the SDK from its pinned git tag)
	poetry install

lock:	## Re-lock dependencies after a pyproject change (poetry.lock + pip-compatible requirements.lock)
	poetry lock
	poetry export --without dev --without-hashes -o requirements.lock

lint:	## Check formatting + lint (isort, black, flake8, mypy)
	poetry run isort --check-only --diff .
	poetry run black --check --diff .
	poetry run flake8 --ignore=E501,E203,W503 --extend-exclude=.venv .
	poetry run mypy --config-file pyproject.toml ./ $(wildcard .github/scripts/*.py)

format:	## Auto-format with isort + black
	poetry run isort .
	poetry run black .

test:	## Run the unit test suite (mocks the SDK, no network)
	poetry run pytest agent_kit/tests $(wildcard .github/scripts/test_*.py) -v

health:	## Check connectivity to the Reya API
	poetry run python -m agent_kit.scripts.health

skill-link:	## Symlink this repo into agent skills dirs (Claude Code, agentskills.io runtimes, Codex if installed); refuses another checkout's link
	@kit="$$(cd "$(CURDIR)" && pwd -P)"; codex="$${CODEX_HOME:-$$HOME/.codex}"; \
	set -- "$$HOME/.claude/skills" "$$HOME/.agents/skills"; \
	if [ -d "$$codex" ]; then set -- "$$@" "$$codex/skills"; fi; \
	for dir in "$$@"; do \
		dest="$$dir/reya-trading-agent-kit"; \
		if { [ -e "$$dest" ] || [ -L "$$dest" ]; } && [ "$$(cd "$$dest" 2>/dev/null && pwd -P || true)" != "$$kit" ]; then \
			echo "error: $$dest belongs to another installation; unlink it explicitly before switching versions." >&2; exit 1; \
		fi; \
		mkdir -p "$$dir" && ln -snf "$(CURDIR)" "$$dest" && echo "Linked: $$dest -> $(CURDIR)"; \
	done

cleanup:	## Remove caches and build artifacts
	find . | grep -E "(__pycache__|\.pyc|\.pyo$$)" | xargs rm -rf
	find . | grep -E ".mypy_cache" | xargs rm -rf
	find . | grep -E ".pytest_cache" | xargs rm -rf

.PHONY: all $(MAKECMDGOALS)
