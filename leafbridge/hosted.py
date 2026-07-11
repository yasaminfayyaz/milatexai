"""MiLatexAI hosted (Phase 2): the multi-tenant, authenticated MCP server.

Differences from the local Phase-1 ``server.py``:

* Auth via WorkOS AuthKit (``AuthKitProvider``). Every tool call carries a
  verified user identity.
* Each user's Overleaf token is fetched from the encrypted store (decrypted
  transiently) via :class:`~leafbridge.service.AccountService` — no
  ``projects.json``.
* Users onboard from inside Claude with ``connect_project`` (paste link + token),
  so no web pages are required yet.
* Writes are metered: the free-tier monthly commit cap is enforced, with pro /
  admin bypass.

The Phase-1 local server is untouched; this is an additive module.
"""

from __future__ import annotations

import base64
import difflib
import os
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qs, quote

from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from fastmcp.server.auth.providers.workos import AuthKitProvider
from fastmcp.server.dependencies import get_access_token
from starlette.requests import Request
from starlette.responses import HTMLResponse, Response

from . import __version__, latex, texcompile, web
from .config import ProjectConfig, default_data_dir
from .connect_link import ConnectCodeError, mint_connect_code, verify_connect_code
from .files import (
    PathError,
    list_source_files,
    number_lines,
    read_text,
    safe_join,
    write_bytes_exact,
    write_text_exact,
)
from .git_worker import GitError, GitWorker, PushConflict
from .service import (
    AccountService,
    LimitExceeded,
    ProjectNotConnected,
    ServiceError,
)
from .store import InMemoryStore, Store, TokenCipher, User

INSTRUCTIONS = """\
MiLatexAI edits the signed-in user's real Overleaf projects over Overleaf's Git
bridge. First-time users must run connect_project once with their Overleaf
project link and a Git token (Account Settings > Git Integration) — the token is
stored encrypted. Every write (edit_file/write_file/delete_file/upload_file)
commits and pushes immediately and counts toward the monthly limit; reads are
free and unlimited. Before editing, read the file so edit_file's old_string
matches exactly.
"""

MAX_UPLOAD_BYTES = 50 * 1024 * 1024


def _month() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m")


def _mini_diff(old: str, new: str, path: str, max_lines: int = 40) -> str:
    diff = list(
        difflib.unified_diff(
            old.splitlines(), new.splitlines(),
            fromfile=f"a/{path}", tofile=f"b/{path}", lineterm="",
        )
    )
    if not diff:
        return ""
    if len(diff) > max_lines:
        diff = diff[:max_lines] + [f"… ({len(diff) - max_lines} more diff lines)"]
    return "Change applied:\n" + "\n".join(diff)


def _wrap(exc: Exception) -> ToolError:
    if isinstance(exc, ToolError):
        return exc
    if isinstance(exc, (LimitExceeded, ProjectNotConnected, ServiceError, PathError)):
        return ToolError(str(exc))
    if isinstance(exc, PushConflict):
        return ToolError(str(exc))
    if isinstance(exc, GitError):
        return ToolError(f"Git operation failed: {exc}")
    return ToolError(f"Unexpected error: {exc}")


def _identity_from_token() -> tuple[str, str]:
    """Default identity provider: pull (user_id, email) from the WorkOS token."""
    token = get_access_token()
    if token is None:
        raise ToolError("Not authenticated.")
    claims = getattr(token, "claims", None) or {}
    user_id = claims.get("sub") or getattr(token, "client_id", None)
    email = claims.get("email") or claims.get("email_address") or ""
    if not user_id:
        raise ToolError("Access token has no subject (sub) claim.")
    return user_id, email


class HostedApp:
    """Per-deployment state: the account service + one git worker, plus how to
    identify the current caller."""

    def __init__(
        self,
        *,
        store: Store,
        cipher: TokenCipher,
        data_dir: Path,
        admin_emails: tuple[str, ...] = (),
        identity_provider=_identity_from_token,
        base_url: str = "http://localhost:8000",
    ):
        self.service = AccountService(store, cipher)
        self.cipher = cipher
        self.worker = GitWorker(data_dir)
        self.admin_emails = admin_emails
        self._identity = identity_provider
        self.base_url = base_url.rstrip("/")
        # Best-effort single-use tracking for connect codes (TTL is the real
        # control; this just stops a link being replayed within its window).
        self._consumed_codes: set[str] = set()

    async def user(self) -> User:
        user_id, email = self._identity()
        return await self.service.get_or_create_user(
            user_id, email, admin_emails=self.admin_emails
        )

    async def apply_and_push(
        self, user: User, proj: ProjectConfig, mutate, message: str,
        *, guard_path: str | None = None, allow_shrink: bool = False,
    ) -> str:
        month = _month()
        # Enforce the metered limit BEFORE doing any work.
        await self.service.check_commit_allowed(user.user_id, month)
        async with self.worker.lock_for(proj):
            repo = await self.worker.ensure_repo(proj, sync=False)
            await self.worker.sync(proj, force=True)
            before = None
            if guard_path is not None:
                gp = safe_join(repo, guard_path)
                before = gp.stat().st_size if gp.is_file() else None
            mutate(repo)
            if before is not None and not allow_shrink:
                gp = safe_join(repo, guard_path)
                after = gp.stat().st_size if gp.is_file() else 0
                if after * 2 < before and (before - after) > 200:
                    pct = round(100 * (before - after) / before)
                    raise PathError(
                        f"Refusing to apply: removes {pct}% of {guard_path} "
                        f"({before} -> {after} bytes). If intentional, retry with "
                        f"allow_shrink=true."
                    )
            result = await self.worker.commit_and_push(proj, message)
        if not result.committed:
            return f"No change made: {result.message}"
        used = await self.service.record_commit(user.user_id, month)
        return (
            f"Done. Committed {result.hash} and pushed to Overleaf — live in "
            f"{proj.name!r}. (commit {used} this month)"
        )


def create_hosted_server(
    *,
    store: Store | None = None,
    cipher: TokenCipher | None = None,
    data_dir: str | Path | None = None,
    admin_emails: tuple[str, ...] = (),
    auth: bool = True,
    identity_provider=_identity_from_token,
    base_url: str | None = None,
) -> FastMCP:
    """Build the hosted server. Tests pass ``auth=False`` + a fake
    ``identity_provider`` + an ``InMemoryStore`` to drive it without WorkOS."""
    store = store if store is not None else InMemoryStore()
    if cipher is None:
        cipher, _ = TokenCipher.from_env()
    resolved_base = base_url or os.environ.get("BASE_URL", "http://localhost:8000")
    app = HostedApp(
        store=store,
        cipher=cipher,
        data_dir=Path(data_dir) if data_dir else default_data_dir(),
        admin_emails=admin_emails or _admin_emails_from_env(),
        identity_provider=identity_provider,
        base_url=resolved_base,
    )

    auth_provider = None
    if auth:
        auth_provider = AuthKitProvider(
            authkit_domain=os.environ["WORKOS_AUTHKIT_DOMAIN"],
            base_url=resolved_base,
        )
    mcp = FastMCP(
        name="MiLatexAI", instructions=INSTRUCTIONS, version=__version__, auth=auth_provider
    )

    # -- account management ------------------------------------------------

    @mcp.tool
    async def start_connect() -> str:
        """Get a secure link to connect an Overleaf project WITHOUT pasting your
        Git token into this chat. Recommended over connect_project. Returns a
        one-time link (valid 15 minutes) where you enter your token in a web form;
        the token is encrypted and never appears in the conversation."""
        try:
            user = await app.user()
            code = mint_connect_code(app.cipher, user.user_id, user.email)
        except Exception as exc:  # noqa: BLE001
            raise _wrap(exc)
        url = f"{app.base_url}/connect?code={quote(code, safe='')}"
        return (
            "Open this link in your browser to connect an Overleaf project "
            "securely — your Git token stays out of this chat:\n\n"
            f"{url}\n\n"
            "The link works once and expires in 15 minutes. After connecting, come "
            "back here and ask me to list your files or edit your paper."
        )

    @mcp.tool
    async def connect_project(
        overleaf_url: str, token: str, name: str | None = None
    ) -> str:
        """Connect one of your Overleaf projects (run once per project). The Git
        token is stored ENCRYPTED and never shown again. Prefer start_connect,
        which keeps your token out of the chat.

        Args:
            overleaf_url: Your project URL, https://www.overleaf.com/project/<id>.
            token: Your Overleaf Git token (Account Settings > Git Integration).
            name: A short label for the project (optional).
        """
        try:
            user = await app.user()
            proj = await app.service.connect_project(user.user_id, overleaf_url, token, name)
        except Exception as exc:  # noqa: BLE001
            raise _wrap(exc)
        return f"Connected project {proj.name!r} ({proj.project_id}). You can now edit it."

    @mcp.tool
    async def disconnect_project(project: str) -> str:
        """Disconnect a project and delete its stored token."""
        try:
            user = await app.user()
            ok = await app.service.disconnect_project(user.user_id, project)
        except Exception as exc:  # noqa: BLE001
            raise _wrap(exc)
        return "Disconnected." if ok else "No such project."

    @mcp.tool
    async def list_projects() -> str:
        """List your connected Overleaf projects."""
        try:
            user = await app.user()
            projects = await app.service.store.list_projects(user.user_id)
        except Exception as exc:  # noqa: BLE001
            raise _wrap(exc)
        if not projects:
            return "No projects connected yet. Use connect_project with your Overleaf link + Git token."
        return "\n".join(f"- {p.name}  (id {p.project_id})" for p in projects)

    # -- reads (unmetered) -------------------------------------------------

    @mcp.tool
    async def list_files(project: str | None = None, all_files: bool = False) -> str:
        """List files in one of your projects."""
        try:
            user = await app.user()
            proj = await app.service.resolve_project(user.user_id, project)
            async with app.worker.open_repo(proj) as repo:
                entries = list_source_files(repo, all_files=all_files)
        except Exception as exc:  # noqa: BLE001
            raise _wrap(exc)
        if not entries:
            return f"No files in {proj.name!r}."
        return "\n".join([f"{len(entries)} file(s) in {proj.name!r}:"]
                         + [f"- {e.path}  ({e.size} bytes)" for e in entries])

    @mcp.tool
    async def read_file(path: str, project: str | None = None, with_line_numbers: bool = True) -> str:
        """Read a file's content from one of your projects."""
        try:
            user = await app.user()
            proj = await app.service.resolve_project(user.user_id, project)
            async with app.worker.open_repo(proj) as repo:
                content = read_text(safe_join(repo, path))
        except Exception as exc:  # noqa: BLE001
            raise _wrap(exc)
        return number_lines(content) if with_line_numbers else content

    @mcp.tool
    async def get_sections(path: str, project: str | None = None) -> str:
        """Return the LaTeX section outline of a .tex file."""
        try:
            user = await app.user()
            proj = await app.service.resolve_project(user.user_id, project)
            async with app.worker.open_repo(proj) as repo:
                content = read_text(safe_join(repo, path))
        except Exception as exc:  # noqa: BLE001
            raise _wrap(exc)
        return f"Sections in {path}:\n{latex.outline(latex.find_sections(content))}"

    @mcp.tool
    async def read_section(path: str, title: str, project: str | None = None) -> str:
        """Return one section of a .tex file by title."""
        try:
            user = await app.user()
            proj = await app.service.resolve_project(user.user_id, project)
            async with app.worker.open_repo(proj) as repo:
                content = read_text(safe_join(repo, path))
        except Exception as exc:  # noqa: BLE001
            raise _wrap(exc)
        found = latex.find_section(content, title)
        if found is None:
            raise ToolError(
                f"No section {title!r} in {path}. Available:\n{latex.outline(latex.find_sections(content))}"
            )
        section, body = found
        return (f"# {section.command}: {section.title}  (lines {section.line}-{section.end_line})\n"
                f"{number_lines(body, start=section.line)}")

    @mcp.tool
    async def get_history(project: str | None = None, limit: int = 10) -> str:
        """Recent commits for one of your projects."""
        try:
            user = await app.user()
            proj = await app.service.resolve_project(user.user_id, project)
            async with app.worker.lock_for(proj):
                return await app.worker.log(proj, limit=max(1, min(limit, 50)))
        except Exception as exc:  # noqa: BLE001
            raise _wrap(exc)

    @mcp.tool
    async def check_compile(project: str | None = None) -> str:
        """Build one of your projects with a local LaTeX engine (read-only)."""
        try:
            user = await app.user()
            proj = await app.service.resolve_project(user.user_id, project)
            async with app.worker.open_repo(proj) as repo:
                main = texcompile.find_main_tex(repo)
                if not main:
                    raise ToolError("Could not find a root .tex to compile.")
                res = await texcompile.compile_project(repo, main)
        except Exception as exc:  # noqa: BLE001
            raise _wrap(exc)
        if not res.available:
            return f"Compile check unavailable: {res.message}"
        lines = [f"{main}: {res.message}"]
        if not res.ok and res.errors:
            lines += ["Errors:"] + [f"  {e}" for e in res.errors]
        return "\n".join(lines)

    # -- writes (metered) --------------------------------------------------

    @mcp.tool
    async def edit_file(
        path: str, old_string: str, new_string: str,
        project: str | None = None, allow_shrink: bool = False,
    ) -> str:
        """Replace one exact, unique occurrence of old_string, then commit+push."""
        if old_string == new_string:
            raise ToolError("old_string and new_string are identical.")
        try:
            user = await app.user()
            proj = await app.service.resolve_project(user.user_id, project)
        except Exception as exc:  # noqa: BLE001
            raise _wrap(exc)

        def mutate(repo: Path) -> None:
            target = safe_join(repo, path)
            content = read_text(target, strict=True)
            n = content.count(old_string)
            if n == 0:
                raise PathError(f"old_string not found in {path}; re-read the file.")
            if n > 1:
                raise PathError(f"old_string appears {n} times in {path}; make it unique.")
            write_text_exact(target, content.replace(old_string, new_string, 1))

        try:
            result = await app.apply_and_push(
                user, proj, mutate, f"Edit {path} (MiLatexAI)",
                guard_path=path, allow_shrink=allow_shrink,
            )
        except Exception as exc:  # noqa: BLE001
            raise _wrap(exc)
        if result.startswith("Done"):
            d = _mini_diff(old_string, new_string, path)
            if d:
                result = f"{result}\n\n{d}"
        return result

    @mcp.tool
    async def write_file(
        path: str, content: str, project: str | None = None, allow_shrink: bool = False
    ) -> str:
        """Create or overwrite a file, then commit+push."""
        try:
            user = await app.user()
            proj = await app.service.resolve_project(user.user_id, project)
        except Exception as exc:  # noqa: BLE001
            raise _wrap(exc)

        def mutate(repo: Path) -> None:
            target = safe_join(repo, path)
            target.parent.mkdir(parents=True, exist_ok=True)
            write_text_exact(target, content)

        try:
            return await app.apply_and_push(
                user, proj, mutate, f"Write {path} (MiLatexAI)",
                guard_path=path, allow_shrink=allow_shrink,
            )
        except Exception as exc:  # noqa: BLE001
            raise _wrap(exc)

    @mcp.tool
    async def delete_file(path: str, project: str | None = None) -> str:
        """Delete a file, then commit+push."""
        try:
            user = await app.user()
            proj = await app.service.resolve_project(user.user_id, project)
        except Exception as exc:  # noqa: BLE001
            raise _wrap(exc)

        def mutate(repo: Path) -> None:
            target = safe_join(repo, path)
            if not target.exists():
                raise PathError(f"Cannot delete {path}: does not exist.")
            if target.is_dir():
                raise PathError(f"{path} is a directory.")
            target.unlink()

        try:
            return await app.apply_and_push(user, proj, mutate, f"Delete {path} (MiLatexAI)")
        except Exception as exc:  # noqa: BLE001
            raise _wrap(exc)

    @mcp.tool
    async def upload_file(path: str, content_base64: str, project: str | None = None) -> str:
        """Add or replace a BINARY file (image/PDF) from base64, then commit+push."""
        try:
            data = base64.b64decode(content_base64, validate=True)
        except Exception as exc:  # noqa: BLE001
            raise ToolError(f"content_base64 is not valid base64: {exc}")
        if len(data) > MAX_UPLOAD_BYTES:
            raise ToolError(f"File too large ({len(data)} bytes; limit {MAX_UPLOAD_BYTES}).")
        try:
            user = await app.user()
            proj = await app.service.resolve_project(user.user_id, project)
        except Exception as exc:  # noqa: BLE001
            raise _wrap(exc)

        def mutate(repo: Path) -> None:
            target = safe_join(repo, path)
            target.parent.mkdir(parents=True, exist_ok=True)
            write_bytes_exact(target, data)

        try:
            return await app.apply_and_push(
                user, proj, mutate, f"Upload {path} ({len(data)} bytes, MiLatexAI)"
            )
        except Exception as exc:  # noqa: BLE001
            raise _wrap(exc)

    # -- web surface: token-out-of-chat onboarding -------------------------
    # These routes live OUTSIDE the MCP bearer auth. They self-authenticate via
    # the one-time connect code minted by start_connect, so the Overleaf token is
    # typed into a browser form (over HTTPS) instead of the chat transcript.

    @mcp.custom_route("/", methods=["GET"])
    async def landing(request: Request) -> Response:
        return HTMLResponse(web.render_landing())

    @mcp.custom_route("/connect", methods=["GET"])
    async def connect_page(request: Request) -> Response:
        code = request.query_params.get("code", "")
        try:
            _uid, email = verify_connect_code(app.cipher, code)
        except ConnectCodeError as exc:
            return HTMLResponse(
                web.render_notice("Link expired", str(exc), icon="⏰"),
                status_code=400,
            )
        if code in app._consumed_codes:
            return HTMLResponse(
                web.render_notice(
                    "Already used",
                    "This connect link has already been used. Run start_connect "
                    "in Claude to get a fresh one.",
                    icon="🔁",
                ),
                status_code=409,
            )
        return HTMLResponse(web.render_connect_form(code, email=email))

    @mcp.custom_route("/connect", methods=["POST"])
    async def connect_submit(request: Request) -> Response:
        # Parse the urlencoded body ourselves (no python-multipart dependency).
        body = (await request.body()).decode("utf-8", "replace")
        fields = parse_qs(body, keep_blank_values=True)

        def field(key: str) -> str:
            return (fields.get(key) or [""])[0].strip()

        code = field("code")
        overleaf_url = field("overleaf_url")
        token = field("token")
        name = field("name") or None

        try:
            user_id, email = verify_connect_code(app.cipher, code)
        except ConnectCodeError as exc:
            return HTMLResponse(
                web.render_notice("Link expired", str(exc), icon="⏰"),
                status_code=400,
            )
        if code in app._consumed_codes:
            return HTMLResponse(
                web.render_notice(
                    "Already used",
                    "This connect link has already been used. Run start_connect "
                    "in Claude to get a fresh one.",
                    icon="🔁",
                ),
                status_code=409,
            )

        def form_error(msg: str, status: int = 400) -> Response:
            # Re-render with the token field cleared; never echo the token back.
            return HTMLResponse(
                web.render_connect_form(
                    code, overleaf_url=overleaf_url, name=name or "",
                    email=email, error=msg,
                ),
                status_code=status,
            )

        if not overleaf_url or not token:
            return form_error("Please provide both your project link and Git token.")
        try:
            await app.service.get_or_create_user(
                user_id, email, admin_emails=app.admin_emails
            )
            proj = await app.service.connect_project(
                user_id, overleaf_url, token, name
            )
        except (LimitExceeded, ProjectNotConnected, ServiceError) as exc:
            return form_error(str(exc))
        except Exception:  # noqa: BLE001
            return form_error(
                "Something went wrong connecting the project. Please try again.",
                status=500,
            )
        app._consumed_codes.add(code)
        return HTMLResponse(web.render_success(proj.name, proj.project_id))

    return mcp


def _admin_emails_from_env() -> tuple[str, ...]:
    raw = os.environ.get("LEAFBRIDGE_ADMIN_EMAILS", "")
    return tuple(e.strip() for e in raw.split(",") if e.strip())
