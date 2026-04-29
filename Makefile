.PHONY: run run-dev run-realtime test setup

PYTHON := .venv/bin/python

# First-time setup: create venv, install deps, create required dirs.
setup:
	python3 -m venv .venv
	.venv/bin/pip install --upgrade pip -q
	.venv/bin/pip install -r requirements.txt -q
	mkdir -p app/static

# Hands-off server (no auto-reload). Binds all interfaces so LAN access works.
run: setup
	$(PYTHON) -m uvicorn app.main:app --host 0.0.0.0 --port 8000

# Faster news → candidates → paper cadence (see README "Realtime paper").
run-realtime: setup
	REALTIME_PAPER_QUICKSTART=1 $(PYTHON) -m uvicorn app.main:app --host 0.0.0.0 --port 8000

# Local development with auto-reload.
run-dev: setup
	$(PYTHON) -m uvicorn app.main:app --host 127.0.0.1 --port 8000 --reload

test: setup
	$(PYTHON) -m pytest -q
