.PHONY: run run-dev test

# Hands-off server (no auto-reload). Binds all interfaces so LAN access works.
run:
	python -m uvicorn app.main:app --host 0.0.0.0 --port 8000

# Local development with auto-reload.
run-dev:
	python -m uvicorn app.main:app --host 127.0.0.1 --port 8000 --reload

test:
	python -m pytest -q
