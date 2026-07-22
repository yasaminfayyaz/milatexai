"""Git worker: clone / pull / commit / push against the Overleaf Git bridge.

Design rules (from the plan + Overleaf's documented behavior):

* One clone per project, cached on local disk; the cache is disposable and
  re-created on demand if it disappears.
* A per-project async lock so two edits never race.
* Always **pull then push**; never force-push (Overleaf rejects history
  rewrites, and the web editor auto-commits so the remote moves independently).
* The auth token is passed on the URL only for the network call. It is briefly
  present in ``.git/config`` during the initial clone, then stripped immediately
  (origin is reset to the tokenless URL); fetch/push pass the authed URL
  explicitly. The token is never surfaced in error/log text (see ``_scrub``).
* Git runs in a worker thread (``asyncio.to_thread``) so it works regardless of
  the host event loop, and per-project serialization keeps that safe.
"""

from __future__ import annotations

import asyncio
import os
import subprocess
import time
from collections import defaultdict
from contextlib import asynccontextmanager
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

_COMMIT_NAME = "MiLatexAI"
_COMMIT_EMAIL = "leafbridge@users.noreflect"

# Overleaf's Git bridge throttles rapid pushes. Absorb it: space pushes out, and
# back off + retry when a network op is rate-limited (rather than failing the edit).
_RATE_LIMIT_MARKERS = (
    "429", "too many requests", "rate limit", "rate-limit", "slow down", "throttl",
)
RETRY_DELAYS = (3.0, 8.0, 20.0)  # waits after successive rate-limited attempts
MIN_PUSH_INTERVAL_SECONDS = 1.5  # minimum spacing between pushes to one project


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
        self._last_push: dict[str, float] = {}

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

    @asynccontextmanager
    async def open_repo(self, project: ProjectConfig, *, sync: bool = True):
        """Acquire the per-project lock for the WHOLE duration of a read, then
        yield the clone path. This serializes reads with writes so a concurrent
        write's ``reset --hard`` / ``clean -fd`` can never wipe the working tree
        while a read is in progress (and two reads never collide on index.lock)."""
        async with self.lock_for(project):
            yield await self.ensure_repo(project, sync=sync)

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
        await self._fetch(project, branch)
        await self._git(project, ["reset", "--hard", "FETCH_HEAD"])
        await self._git(project, ["clean", "-fd"])
        self._last_sync[pid] = time.monotonic()

    async def commit_and_push(
        self, project: ProjectConfig, message: str, *, allow_empty: bool = False
    ) -> CommitResult:
        """Stage all changes, commit, and push. Assumes caller holds the lock and
        has already applied file changes on disk after a fresh sync.
        ``allow_empty`` commits even with no file changes (checkpoint markers).
        """
        # Stage everything and see whether anything actually changed.
        await self._git(project, ["add", "-A"])
        status = await self._git(project, ["status", "--porcelain"])
        if not status.strip() and not allow_empty:
            return CommitResult(False, False, None, "No changes to commit.")

        commit_args = [
            "-c", f"user.name={_COMMIT_NAME}",
            "-c", f"user.email={_COMMIT_EMAIL}",
            "commit", "-m", message,
        ]
        if allow_empty:
            commit_args.append("--allow-empty")
        await self._git(project, commit_args)
        commit_hash = (await self._git(project, ["rev-parse", "--short", "HEAD"])).strip()

        branch = await self._get_branch(project)
        try:
            await self._push(project, branch)
        except PushConflict:
            # Remote moved between our sync and our push. Replay our single
            # commit on top of the new remote tip, then push once more.
            await self._fetch(project, branch)
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

    async def log_matching(self, project: ProjectConfig, needle: str, limit: int = 30) -> str:
        """Commits whose message contains ``needle`` (checkpoint listing)."""
        await self.ensure_repo(project)
        out = await self._git(
            project, ["log", f"-{limit}", "--format=%h  %ar  %s", "--grep", needle]
        )
        return out.strip()

    async def diff_stat(self, project: ProjectConfig, ref: str) -> str:
        """File-level summary of what changed between ``ref`` and now."""
        await self.ensure_repo(project)
        out = await self._git(project, ["diff", "--stat", f"{ref}..HEAD"])
        return out.strip()

    async def show_file(self, project: ProjectConfig, ref: str, path: str) -> str:
        """A file's content as of ``ref`` (raises GitError if unknown)."""
        await self.ensure_repo(project)
        return await self._git(project, ["show", f"{ref}:{path}"])

    async def log_deleted(self, project: ProjectConfig, prefix: str) -> str:
        """Raw ``git log`` of deletions under ``prefix`` (within the shallow-clone
        window), used to remember figures whose source was deleted. Best-effort:
        history problems just mean an empty answer, never a failed request."""
        await self.ensure_repo(project)
        try:
            return await self._git(
                project,
                ["log", "--diff-filter=D", "--format=@%h", "--name-only", "--", prefix],
            )
        except GitError:
            return ""

    # -- internals ----------------------------------------------------------

    async def _git_networked(
        self, project: ProjectConfig, args: list[str], *, cwd: Path | None = None
    ) -> str:
        """Run a networked git op, backing off and retrying when Overleaf's Git
        bridge rate-limits us (429 / 'too many requests'), rather than failing
        the edit. Non-rate-limit errors (e.g. conflicts) propagate immediately."""
        attempt = 0
        while True:
            try:
                return await self._git(project, args, cwd=cwd, authed=True)
            except GitError as exc:
                rate_limited = any(m in str(exc).lower() for m in _RATE_LIMIT_MARKERS)
                if rate_limited and attempt < len(RETRY_DELAYS):
                    await asyncio.sleep(RETRY_DELAYS[attempt])
                    attempt += 1
                    continue
                raise

    async def _clone(self, project: ProjectConfig) -> None:
        path = self.repo_path(project)
        if path.exists():
            # Stale/partial dir — remove and re-clone.
            await asyncio.to_thread(_rmtree, path)
        # Prefer a shallow clone (polite), but fall back to a full clone if the
        # server doesn't support shallow fetch (Overleaf's Git bridge is custom).
        try:
            await self._git_networked(
                project,
                ["clone", "--depth", str(CLONE_DEPTH), project.authed_url(), str(path)],
                cwd=self.data_dir,
            )
        except GitError:
            if path.exists():
                await asyncio.to_thread(_rmtree, path)
            await self._git_networked(
                project,
                ["clone", project.authed_url(), str(path)],
                cwd=self.data_dir,
            )
        # Drop the token: point origin at the clean URL. We always pass the
        # authed URL explicitly on fetch/push instead.
        await self._git(project, ["remote", "set-url", "origin", project.clone_url])
        self._last_sync[project.project_id] = time.monotonic()

    async def _fetch(self, project: ProjectConfig, branch: str) -> None:
        """Fetch the branch tip into FETCH_HEAD, falling back from shallow to full."""
        try:
            await self._git_networked(
                project,
                ["fetch", "--depth", str(CLONE_DEPTH), project.authed_url(), branch],
            )
        except GitError:
            await self._git_networked(
                project, ["fetch", project.authed_url(), branch]
            )

    async def _push(self, project: ProjectConfig, branch: str) -> None:
        # Proactively space pushes so we don't trip Overleaf's rate limiter.
        pid = project.project_id
        last = self._last_push.get(pid)
        if last is not None:
            gap = time.monotonic() - last
            if gap < MIN_PUSH_INTERVAL_SECONDS:
                await asyncio.sleep(MIN_PUSH_INTERVAL_SECONDS - gap)
        try:
            await self._git_networked(
                project, ["push", project.authed_url(), f"HEAD:{branch}"]
            )
        except GitError as exc:
            msg = str(exc).lower()
            if "non-fast-forward" in msg or "fetch first" in msg or "rejected" in msg:
                raise PushConflict(str(exc)) from exc
            raise
        self._last_push[pid] = time.monotonic()

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
            # Never translate line endings. Git for Windows defaults autocrlf=true
            # at the system level, which would rewrite the working copy to CRLF and
            # cause line-ending churn / corruption on write. Keep the working tree
            # byte-identical to the repo (LF), matching Overleaf.
            "-c",
            "core.autocrlf=false",
            "-c",
            "core.eol=lf",
            # A committer identity for EVERY command (not just commit) so the
            # conflict-recovery `rebase` can replay a commit on hosts/containers
            # with no global git identity, instead of aborting and misreporting a
            # mergeable edit as an unresolvable conflict.
            "-c",
            f"user.name={_COMMIT_NAME}",
            "-c",
            f"user.email={_COMMIT_EMAIL}",
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
