"""Git worker: clone / pull / commit / push against the Overleaf Git bridge.

Design rules (from the plan + Overleaf's documented behavior):

* One clone per project, cached on local disk; the cache is disposable and
  re-created on demand if it disappears.
* A per-project async lock so two edits never race.
* Always **pull then push**; never force-push (Overleaf rejects history
  rewrites, and the web editor auto-commits so the remote moves independently).
* The auth token is passed on the URL only for the network call and is never
  written into ``.git/config`` or surfaced in error text.
* Git runs in a worker thread (``asyncio.to_thread``) so it works regardless of
  the host event loop, and per-project serialization keeps that safe.
"""

from __future__ import annotations

import asyncio
import os
import subprocess
import time
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path

from .config import ProjectConfig

# Overleaf keeps projects small; a shallow clone is polite and plenty for our
# "recent history" needs while still allowing pushes.
CLONE_DEPTH = 50

# Don't re-pull more often than this for read operations (seconds). Writes
# always sync first regardless. Keeps us polite to Overleaf's rate limits.
SYNC_TTL_SECONDS = 15.0

# Bump git's HTTP post buffer so larger pushes don't fail (Overleaf tip).
_POST_BUFFER = str(20 * 1024 * 1024)

_COMMIT_NAME = "LeafBridge"
_COMMIT_EMAIL = "leafbridge@users.noreflect"


class GitError(Exception):
    """A git operation failed. The message is already token-scrubbed."""


class PushConflict(GitError):
    """The remote moved and our change could not be replayed automatically."""


@dataclass
class CommitResult:
    committed: bool
    pushed: bool
    hash: str | None
    message: str
    detail: str = ""


class GitWorker:
    def __init__(self, data_dir: Path):
        self.data_dir = Path(data_dir)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self._locks: dict[str, asyncio.Lock] = defaultdict(asyncio.Lock)
        self._branch: dict[str, str] = {}
        self._last_sync: dict[str, float] = {}

    # -- public API ---------------------------------------------------------

    def lock_for(self, project: ProjectConfig) -> asyncio.Lock:
        return self._locks[project.project_id]

    def repo_path(self, project: ProjectConfig) -> Path:
        return self.data_dir / project.project_id

    async def ensure_repo(self, project: ProjectConfig, *, sync: bool = True) -> Path:
        """Return the local clone path, cloning if needed and optionally syncing.

        For reads, ``sync`` respects a short TTL to avoid hammering Overleaf; for
        writes the caller uses :meth:`sync` with ``force=True`` inside the lock.
        """
        path = self.repo_path(project)
        if not (path / ".git").exists():
            await self._clone(project)
        elif sync:
            await self.sync(project, force=False)
        return path

    async def sync(self, project: ProjectConfig, *, force: bool) -> None:
        """Fast-forward the local clone to match the Overleaf remote.

        We keep no local uncommitted state between operations, so a hard reset to
        the remote branch is the simplest correct "pull".
        """
        pid = project.project_id
        if not force:
            last = self._last_sync.get(pid, 0.0)
            if (time.monotonic() - last) < SYNC_TTL_SECONDS:
                return
        path = self.repo_path(project)
        if not (path / ".git").exists():
            await self._clone(project)
            return
        branch = await self._get_branch(project)
        await self._git(
            project,
            ["fetch", "--depth", str(CLONE_DEPTH), project.authed_url(), branch],
            authed=True,
        )
        await self._git(project, ["reset", "--hard", "FETCH_HEAD"])
        await self._git(project, ["clean", "-fd"])
        self._last_sync[pid] = time.monotonic()

    async def commit_and_push(
        self, project: ProjectConfig, message: str
    ) -> CommitResult:
        """Stage all changes, commit, and push. Assumes caller holds the lock and
        has already applied file changes on disk after a fresh sync.
        """
        # Stage everything and see whether anything actually changed.
        await self._git(project, ["add", "-A"])
        status = await self._git(project, ["status", "--porcelain"])
        if not status.strip():
            return CommitResult(False, False, None, "No changes to commit.")

        await self._git(
            project,
            [
                "-c",
                f"user.name={_COMMIT_NAME}",
                "-c",
                f"user.email={_COMMIT_EMAIL}",
                "commit",
                "-m",
                message,
            ],
        )
        commit_hash = (await self._git(project, ["rev-parse", "--short", "HEAD"])).strip()

        branch = await self._get_branch(project)
        try:
            await self._push(project, branch)
        except PushConflict:
            # Remote moved between our sync and our push. Replay our single
            # commit on top of the new remote tip, then push once more.
            await self._git(
                project,
                ["fetch", "--depth", str(CLONE_DEPTH), project.authed_url(), branch],
                authed=True,
            )
            try:
                await self._git(project, ["rebase", "FETCH_HEAD"])
            except GitError as exc:
                await self._git(project, ["rebase", "--abort"], check=False)
                # Leave the local clone matching the remote so the next op is clean.
                await self._git(project, ["reset", "--hard", "FETCH_HEAD"], check=False)
                raise PushConflict(
                    "Someone edited this project in Overleaf at the same time and "
                    "the changes overlap, so the edit could not be applied "
                    "automatically. Nothing was pushed. Please re-read the file "
                    "and try again."
                ) from exc
            commit_hash = (
                await self._git(project, ["rev-parse", "--short", "HEAD"])
            ).strip()
            await self._push(project, branch)

        self._last_sync[project.project_id] = time.monotonic()
        return CommitResult(True, True, commit_hash, "Committed and pushed.")

    async def log(self, project: ProjectConfig, limit: int = 10) -> str:
        """Return a compact recent-history view with per-commit stat summaries."""
        await self.ensure_repo(project)
        fmt = "%h %an %ar %s"
        out = await self._git(
            project,
            ["log", f"-{max(1, min(limit, CLONE_DEPTH))}", f"--pretty=format:{fmt}", "--stat"],
        )
        return out.strip() or "(no history)"

    # -- internals ----------------------------------------------------------

    async def _clone(self, project: ProjectConfig) -> None:
        path = self.repo_path(project)
        if path.exists():
            # Stale/partial dir — remove and re-clone.
            await asyncio.to_thread(_rmtree, path)
        await self._git(
            project,
            [
                "clone",
                "--depth",
                str(CLONE_DEPTH),
                project.authed_url(),
                str(path),
            ],
            cwd=self.data_dir,
            authed=True,
        )
        # Drop the token: point origin at the clean URL. We always pass the
        # authed URL explicitly on fetch/push instead.
        await self._git(project, ["remote", "set-url", "origin", project.clone_url])
        self._last_sync[project.project_id] = time.monotonic()

    async def _push(self, project: ProjectConfig, branch: str) -> None:
        try:
            await self._git(
                project,
                ["push", project.authed_url(), f"HEAD:{branch}"],
                authed=True,
            )
        except GitError as exc:
            msg = str(exc).lower()
            if "non-fast-forward" in msg or "fetch first" in msg or "rejected" in msg:
                raise PushConflict(str(exc)) from exc
            raise

    async def _get_branch(self, project: ProjectConfig) -> str:
        pid = project.project_id
        if pid in self._branch:
            return self._branch[pid]
        try:
            branch = (
                await self._git(project, ["symbolic-ref", "--short", "HEAD"])
            ).strip()
        except GitError:
            branch = ""
        if not branch:
            branch = "main"  # Overleaf's modern default; older repos are "master".
        self._branch[pid] = branch
        return branch

    async def _git(
        self,
        project: ProjectConfig,
        args: list[str],
        *,
        cwd: Path | None = None,
        check: bool = True,
        authed: bool = False,
    ) -> str:
        work_dir = cwd if cwd is not None else self.repo_path(project)
        return await asyncio.to_thread(
            self._git_sync, project, args, work_dir, check, authed
        )

    def _git_sync(
        self,
        project: ProjectConfig,
        args: list[str],
        work_dir: Path,
        check: bool,
        authed: bool,
    ) -> str:
        env = dict(os.environ)
        # Never prompt for credentials interactively — fail fast instead of hang.
        env["GIT_TERMINAL_PROMPT"] = "0"
        env["GCM_INTERACTIVE"] = "never"
        cmd = [
            "git",
            "-c",
            f"http.postBuffer={_POST_BUFFER}",
            "-c",
            "credential.helper=",
            *args,
        ]
        try:
            proc = subprocess.run(
                cmd,
                cwd=str(work_dir),
                env=env,
                capture_output=True,
                text=True,
                timeout=180,
            )
        except subprocess.TimeoutExpired as exc:
            raise GitError(self._scrub("git command timed out", project)) from exc
        except FileNotFoundError as exc:
            raise GitError("git is not installed or not on PATH.") from exc

        if check and proc.returncode != 0:
            detail = (proc.stderr or proc.stdout or "").strip()
            raise GitError(self._scrub(detail or f"git {args[0]} failed", project))
        return proc.stdout

    @staticmethod
    def _scrub(text: str, project: ProjectConfig) -> str:
        """Remove the auth token (and any embedded credentials) from text."""
        if not text:
            return text
        cleaned = text.replace(project.token, "***")
        # Also catch the whole user:token@ segment if git echoed the URL.
        import re

        cleaned = re.sub(r"https://[^@/\s]+:[^@/\s]+@", "https://***@", cleaned)
        return cleaned


def _rmtree(path: Path) -> None:
    import shutil
    import stat

    def _on_error(func, p, exc_info):  # handle read-only files (e.g. .git objects)
        try:
            os.chmod(p, stat.S_IWRITE)
            func(p)
        except Exception:
            pass

    shutil.rmtree(path, onerror=_on_error)
