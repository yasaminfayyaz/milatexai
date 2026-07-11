"""Regression tests for the Git worker's rate-limit resilience.

Overleaf's Git bridge throttles rapid pushes. A burst of edits (each doing
fetch+commit+push) used to surface the throttle as a hard failure on the second
push. The worker now (a) spaces pushes out and (b) backs off + retries any
networked op that comes back rate-limited, instead of failing the edit.

These tests exercise that logic in isolation by stubbing the low-level ``_git``
call — no real network, no real repo.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from leafbridge import git_worker
from leafbridge.config import ProjectConfig
from leafbridge.git_worker import GitError, GitWorker, PushConflict


def _project() -> ProjectConfig:
    return ProjectConfig(
        name="thesis",
        project_id="0123456789abcdef01234567",
        token="olp_testtoken",
    )


@pytest.fixture(autouse=True)
def _fast_backoff(monkeypatch):
    # Keep the retry semantics (three attempts) but don't actually sleep for
    # seconds during the test run.
    monkeypatch.setattr(git_worker, "RETRY_DELAYS", (0.0, 0.0, 0.0))
    monkeypatch.setattr(git_worker, "MIN_PUSH_INTERVAL_SECONDS", 0.0)


def test_networked_op_retries_then_succeeds(tmp_path, monkeypatch):
    """A rate-limited op that later succeeds should be retried, not surfaced."""
    worker = GitWorker(tmp_path)
    project = _project()
    calls = {"n": 0}

    async def fake_git(proj, args, *, cwd=None, check=True, authed=False):
        calls["n"] += 1
        if calls["n"] < 3:
            raise GitError("remote: 429 Too Many Requests\nfatal: unable to access")
        return "ok"

    monkeypatch.setattr(worker, "_git", fake_git)
    result = asyncio.run(worker._git_networked(project, ["fetch", "x", "main"]))
    assert result == "ok"
    assert calls["n"] == 3  # failed twice, succeeded on the third attempt


def test_networked_op_gives_up_after_retry_budget(tmp_path, monkeypatch):
    """Persistent rate-limiting eventually propagates rather than looping forever."""
    worker = GitWorker(tmp_path)
    project = _project()
    calls = {"n": 0}

    async def always_limited(proj, args, *, cwd=None, check=True, authed=False):
        calls["n"] += 1
        raise GitError("error: rate limit exceeded, slow down")

    monkeypatch.setattr(worker, "_git", always_limited)
    with pytest.raises(GitError):
        asyncio.run(worker._git_networked(project, ["push", "x", "HEAD:main"]))
    # Initial attempt + one retry per RETRY_DELAYS entry.
    assert calls["n"] == len(git_worker.RETRY_DELAYS) + 1


def test_non_rate_limit_error_is_not_retried(tmp_path, monkeypatch):
    """Ordinary git errors must fail fast, not burn the retry budget."""
    worker = GitWorker(tmp_path)
    project = _project()
    calls = {"n": 0}

    async def fake_git(proj, args, *, cwd=None, check=True, authed=False):
        calls["n"] += 1
        raise GitError("fatal: Authentication failed for git.overleaf.com")

    monkeypatch.setattr(worker, "_git", fake_git)
    with pytest.raises(GitError):
        asyncio.run(worker._git_networked(project, ["push", "x", "HEAD:main"]))
    assert calls["n"] == 1  # no retries


def test_push_conflict_is_classified_not_retried(tmp_path, monkeypatch):
    """A non-fast-forward rejection is a real conflict, surfaced as PushConflict."""
    worker = GitWorker(tmp_path)
    project = _project()
    calls = {"n": 0}

    async def fake_git(proj, args, *, cwd=None, check=True, authed=False):
        calls["n"] += 1
        raise GitError("! [rejected] main -> main (non-fast-forward)")

    monkeypatch.setattr(worker, "_git", fake_git)
    with pytest.raises(PushConflict):
        asyncio.run(worker._push(project, "main"))
    assert calls["n"] == 1  # conflicts are not rate-limit retries


def test_push_records_timestamp_and_throttles(tmp_path, monkeypatch):
    """Successful pushes record a timestamp so the next push can be spaced out."""
    worker = GitWorker(tmp_path)
    project = _project()

    async def fake_git(proj, args, *, cwd=None, check=True, authed=False):
        return ""

    monkeypatch.setattr(worker, "_git", fake_git)
    assert project.project_id not in worker._last_push
    asyncio.run(worker._push(project, "main"))
    assert project.project_id in worker._last_push
