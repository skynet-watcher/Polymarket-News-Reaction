from __future__ import annotations

import asyncio

from app.main import healthz


def test_healthz_identifies_vercel_git_build(monkeypatch) -> None:
    monkeypatch.setenv("VERCEL", "1")
    monkeypatch.setenv("VERCEL_ENV", "production")
    monkeypatch.setenv("VERCEL_GIT_COMMIT_REF", "main")
    monkeypatch.setenv("VERCEL_GIT_COMMIT_SHA", "abc123")
    monkeypatch.setenv("VERCEL_GIT_REPO_OWNER", "skynet-watcher")
    monkeypatch.setenv("VERCEL_GIT_REPO_SLUG", "Polymarket-News-Reaction")
    monkeypatch.setenv("VERCEL_URL", "preview.example.vercel.app")

    out = asyncio.run(healthz())

    assert out["ok"] == "true"
    assert out["app"] == "polymarket-news-reaction"
    assert out["build_marker"] == "fastapi-main-vercel"
    assert out["runtime"] == "vercel"
    assert out["vercel_env"] == "production"
    assert out["git_branch"] == "main"
    assert out["git_commit"] == "abc123"
    assert out["git_repo"] == "skynet-watcher/Polymarket-News-Reaction"
    assert out["deployment_url"] == "preview.example.vercel.app"
