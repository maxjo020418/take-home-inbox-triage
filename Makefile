.PHONY: install up down tui inject audit test logs adduser eval

COMPOSE ?= docker compose# on podman: make COMPOSE="podman compose" ...

install:            ## local venv (TUI + dev tools) from uv.lock
	uv sync

up:                 ## start Postgres + mock API + backend
	$(COMPOSE) up -d --build

down:
	$(COMPOSE) down

tui:                ## run the TUI locally against the backend
	uv run python -m src.tui

inject:             ## simulate a new email arriving
	uv run python scripts/inject.py

audit:              ## side effects produced against the mock API
	@curl -s http://127.0.0.1:8099/_audit | uv run python -m json.tool

adduser:            ## make adduser NAME=alice
	$(COMPOSE) exec backend python -m src.backend.users add $(NAME)

test:
	uv run pytest -q

eval:               ## score the live pipeline against dataset/*.jsonl (inject them first)
	uv run python scripts/evaluate.py

logs:
	$(COMPOSE) logs -f backend
