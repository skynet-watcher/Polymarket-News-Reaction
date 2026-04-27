.PHONY: run run-dev run-realtime test

# Hands-off server (no auto-reload). Binds all interfaces so LAN access works.
run:
	python -m uvicorn app.main:app --host 0.0.0.0 --port 8000

# Faster news → candidates → paper cadence (see README "Realtime paper").
run-realtime:
	REALTIME_PAPER_QUICKSTART=1 python -m uvicorn app.main:app --host 0.0.0.0 --port 8000

# Local development with auto-reload.
run-dev:
	python -m uvicorn app.main:app --host 127.0.0.1 --port 8000 --reload

test:
	python -m pytest -q
