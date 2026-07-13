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
import json
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

from . import __version__, latex, site, texcompile, web
from .billing import Billing, plan_change_from_event
from .capacity import CapacityGate
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
bridge. If the user has no project connected yet, just proceed with their request:
any file tool returns a secure link where they paste their Overleaf Git token
(never in chat) — relay that link and ask them to come back. Refer to a project
by its name; list_projects shows the connected ones. To change a token or add
another project, run start_connect (or connect that project again) — same secure
form. Every write (edit_file/write_file/delete_file/upload_file) commits and
pushes immediately and counts toward the monthly limit; reads are free and
unlimited. Before editing, read the file so edit_file's old_string matches exactly.
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


def workos_email_resolver(api_key: str):
    """An async ``(user_id) -> email`` lookup against the WorkOS Management API,
    for when the access token doesn't carry an email claim. Returns "" on any
    problem — email is nice-to-have, never load-bearing for a request."""

    async def resolve(user_id: str) -> str:
        if not api_key or not user_id.startswith("user_"):
            return ""
        import aiohttp

        url = f"https://api.workos.com/user_management/users/{user_id}"
        try:
            timeout = aiohttp.ClientTimeout(total=10)
            async with aiohttp.ClientSession(timeout=timeout) as sess:
                async with sess.get(
                    url, headers={"Authorization": f"Bearer {api_key}"}
                ) as resp:
                    if resp.status != 200:
                        return ""
                    data = await resp.json()
            return data.get("email") or ""
        except Exception:  # noqa: BLE001
            return ""

    return resolve


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
        billing: Billing | None = None,
        capacity: CapacityGate | None = None,
        email_resolver=None,
    ):
        self.service = AccountService(store, cipher)
        self.cipher = cipher
        self.worker = GitWorker(data_dir)
        self.admin_emails = admin_emails
        self._identity = identity_provider
        self._email_resolver = email_resolver
        self.base_url = base_url.rstrip("/")
        # Disabled billing (no Stripe env) is a valid state — the tools just say so.
        self.billing = billing if billing is not None else Billing(
            api_key="", price_id="", webhook_secret="",
            success_url="", cancel_url="", portal_return_url="",
        )
        # No capacity gate given (e.g. tests) -> a disabled gate (free always ok).
        self.capacity = capacity if capacity is not None else CapacityGate(
            subscription_id="", resource_group="", stripe_api_key="",
        )

    async def ensure_capacity(self, user: User) -> None:
        """Admission control for the git-backed tools. Paid users and admins are
        never gated; free users are refused when we're over free capacity."""
        if user.is_admin or user.plan == "pro":
            return
        if not await self.capacity.free_allowed():
            raise ToolError(
                "MiLatexAI's free tier is at capacity right now. Reads and edits "
                "resume automatically once capacity frees up. For guaranteed, "
                "uninterrupted access, upgrade to Pro (run `upgrade`)."
            )

    async def user(self) -> User:
        user_id, email = self._identity()
        user = await self.service.get_or_create_user(
            user_id, email, admin_emails=self.admin_emails
        )
        # WorkOS access tokens don't always carry an email claim. If we still have
        # no email for this user, look it up once from WorkOS and reconcile (this
        # also promotes admins whose email finally becomes known).
        if not user.email and self._email_resolver is not None:
            resolved = await self._email_resolver(user_id)
            if resolved:
                user = await self.service.get_or_create_user(
                    user_id, resolved, admin_emails=self.admin_emails
                )
        return user

    async def resolve_or_onboard(self, user: User, project: str | None) -> ProjectConfig:
        """Resolve the caller's project. If they have NONE connected yet, don't
        just error — hand back a secure connect link so onboarding happens on the
        first action, without anyone needing to know the start_connect tool."""
        try:
            return await self.service.resolve_project(user.user_id, project)
        except ProjectNotConnected:
            projects = await self.service.store.list_projects(user.user_id)
            if projects:
                raise  # they have projects; this is an ambiguous/no-match message
            code = mint_connect_code(self.cipher, user.user_id, user.email)
            url = f"{self.base_url}/connect?code={quote(code, safe='')}"
            raise ToolError(
                "You haven't connected an Overleaf project yet. Open this secure "
                "link to connect one — you enter your Overleaf Git token there, "
                f"never in this chat:\n\n{url}\n\nOnce it's connected, ask me again."
            )

    async def apply_and_push(
        self, user: User, proj: ProjectConfig, mutate, message: str,
        *, guard_path: str | None = None, allow_shrink: bool = False,
    ) -> str:
        # Free users are refused when we're over capacity; paid/admin never are.
        await self.ensure_capacity(user)
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
    billing: Billing | None = None,
    capacity: CapacityGate | None = None,
    email_resolver=None,
) -> FastMCP:
    """Build the hosted server. Tests pass ``auth=False`` + a fake
    ``identity_provider`` + an ``InMemoryStore`` to drive it without WorkOS."""
    store = store if store is not None else InMemoryStore()
    if cipher is None:
        cipher, _ = TokenCipher.from_env()
    resolved_base = base_url or os.environ.get("BASE_URL", "http://localhost:8000")
    if email_resolver is None and os.environ.get("WORKOS_API_KEY"):
        email_resolver = workos_email_resolver(os.environ["WORKOS_API_KEY"])
    app = HostedApp(
        store=store,
        cipher=cipher,
        data_dir=Path(data_dir) if data_dir else default_data_dir(),
        admin_emails=admin_emails or _admin_emails_from_env(),
        identity_provider=identity_provider,
        base_url=resolved_base,
        billing=billing if billing is not None else Billing.from_env(resolved_base),
        capacity=capacity if capacity is not None else CapacityGate.from_env(),
        email_resolver=email_resolver,
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

    @mcp.tool(annotations={"readOnlyHint": True})
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

    @mcp.tool
    async def add_project(overleaf_url: str, name: str | None = None) -> str:
        """Give the AI access to ANOTHER of your Overleaf projects. Reuses the
        Overleaf token you already saved — you only provide the project link, no
        token needed. (Run start_connect first if you've never connected one.)

        Args:
            overleaf_url: The project URL, https://www.overleaf.com/project/<id>.
            name: A short label for the project (optional).
        """
        try:
            user = await app.user()
            proj = await app.service.add_project(user.user_id, overleaf_url, name)
        except Exception as exc:  # noqa: BLE001
            raise _wrap(exc)
        return f"Added project {proj.name!r} ({proj.project_id}). You can now edit it."

    @mcp.tool
    async def manage_projects() -> str:
        """Get a secure link to view, add, or remove the Overleaf projects the AI
        can access. No token needed — the AI only ever touches projects you list."""
        try:
            user = await app.user()
            code = mint_connect_code(app.cipher, user.user_id, user.email)
        except Exception as exc:  # noqa: BLE001
            raise _wrap(exc)
        url = f"{app.base_url}/projects?code={quote(code, safe='')}"
        return (
            "Manage which Overleaf projects the AI can access here — add or remove "
            f"any time, no token needed:\n\n{url}\n\nThe link is valid for 15 minutes."
        )

    @mcp.tool
    async def change_token() -> str:
        """Get a secure link to change or revoke your stored Overleaf Git token
        (for example if you regenerated it in Overleaf). Entered on a web form,
        never in this chat."""
        try:
            user = await app.user()
            code = mint_connect_code(app.cipher, user.user_id, user.email)
        except Exception as exc:  # noqa: BLE001
            raise _wrap(exc)
        url = f"{app.base_url}/token?code={quote(code, safe='')}"
        return (
            "Change or revoke your Overleaf token here (it never appears in this "
            f"chat):\n\n{url}\n\nThe link is valid for 15 minutes."
        )

    # -- billing -----------------------------------------------------------

    @mcp.tool
    async def upgrade() -> str:
        """Upgrade to MiLatexAI Pro (unlimited projects + unlimited write-commits,
        $4.99/mo, local currency where available). Returns a secure Stripe checkout
        link; your plan updates automatically once payment completes."""
        try:
            user = await app.user()
        except Exception as exc:  # noqa: BLE001
            raise _wrap(exc)
        if user.is_admin:
            return "You're an admin — you already have unlimited access."
        if user.plan == "pro":
            return "You're already on Pro. Use manage_subscription to view or cancel."
        if not app.billing.enabled:
            raise ToolError("Billing isn't configured yet. Please try again later.")
        try:
            url, cid = await app.billing.create_checkout(user, user.stripe_customer_id)
            await app.service.set_stripe_customer(user.user_id, cid)
        except Exception as exc:  # noqa: BLE001
            raise _wrap(exc)
        return (
            "Complete your upgrade to Pro here (secure Stripe checkout — your card "
            "never touches this chat):\n\n"
            f"{url}\n\n"
            "Your plan switches to Pro automatically once payment goes through."
        )

    @mcp.tool
    async def manage_subscription() -> str:
        """Open the Stripe billing portal to view, update, or cancel your Pro
        subscription. Returns a secure link."""
        try:
            user = await app.user()
        except Exception as exc:  # noqa: BLE001
            raise _wrap(exc)
        if not user.stripe_customer_id or not app.billing.enabled:
            return "You don't have a subscription yet. Use `upgrade` to go Pro."
        try:
            url = await app.billing.create_portal(user.stripe_customer_id)
        except Exception as exc:  # noqa: BLE001
            raise _wrap(exc)
        return f"Manage your subscription (view invoices, update card, or cancel):\n\n{url}"

    # -- reads (unmetered) -------------------------------------------------

    @mcp.tool(annotations={"readOnlyHint": True})
    async def list_files(project: str | None = None, all_files: bool = False) -> str:
        """List files in one of your projects."""
        try:
            user = await app.user()
            await app.ensure_capacity(user)
            proj = await app.resolve_or_onboard(user, project)
            async with app.worker.open_repo(proj) as repo:
                entries = list_source_files(repo, all_files=all_files)
        except Exception as exc:  # noqa: BLE001
            raise _wrap(exc)
        if not entries:
            return f"No files in {proj.name!r}."
        return "\n".join([f"{len(entries)} file(s) in {proj.name!r}:"]
                         + [f"- {e.path}  ({e.size} bytes)" for e in entries])

    @mcp.tool(annotations={"readOnlyHint": True})
    async def read_file(path: str, project: str | None = None, with_line_numbers: bool = True) -> str:
        """Read a file's content from one of your projects."""
        try:
            user = await app.user()
            await app.ensure_capacity(user)
            proj = await app.resolve_or_onboard(user, project)
            async with app.worker.open_repo(proj) as repo:
                content = read_text(safe_join(repo, path))
        except Exception as exc:  # noqa: BLE001
            raise _wrap(exc)
        return number_lines(content) if with_line_numbers else content

    @mcp.tool(annotations={"readOnlyHint": True})
    async def get_sections(path: str, project: str | None = None) -> str:
        """Return the LaTeX section outline of a .tex file."""
        try:
            user = await app.user()
            await app.ensure_capacity(user)
            proj = await app.resolve_or_onboard(user, project)
            async with app.worker.open_repo(proj) as repo:
                content = read_text(safe_join(repo, path))
        except Exception as exc:  # noqa: BLE001
            raise _wrap(exc)
        return f"Sections in {path}:\n{latex.outline(latex.find_sections(content))}"

    @mcp.tool(annotations={"readOnlyHint": True})
    async def read_section(path: str, title: str, project: str | None = None) -> str:
        """Return one section of a .tex file by title."""
        try:
            user = await app.user()
            await app.ensure_capacity(user)
            proj = await app.resolve_or_onboard(user, project)
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

    @mcp.tool(annotations={"readOnlyHint": True})
    async def get_history(project: str | None = None, limit: int = 10) -> str:
        """Recent commits for one of your projects."""
        try:
            user = await app.user()
            await app.ensure_capacity(user)
            proj = await app.resolve_or_onboard(user, project)
            async with app.worker.lock_for(proj):
                return await app.worker.log(proj, limit=max(1, min(limit, 50)))
        except Exception as exc:  # noqa: BLE001
            raise _wrap(exc)

    @mcp.tool(annotations={"readOnlyHint": True})
    async def check_compile(project: str | None = None) -> str:
        """Build one of your projects with a local LaTeX engine (read-only)."""
        try:
            user = await app.user()
            await app.ensure_capacity(user)
            proj = await app.resolve_or_onboard(user, project)
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
            await app.ensure_capacity(user)
            proj = await app.resolve_or_onboard(user, project)
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
            await app.ensure_capacity(user)
            proj = await app.resolve_or_onboard(user, project)
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
            await app.ensure_capacity(user)
            proj = await app.resolve_or_onboard(user, project)
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
            await app.ensure_capacity(user)
            proj = await app.resolve_or_onboard(user, project)
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

    # The marketing site + /account are static per process — render once, cache.
    _pages: dict[str, str] = {}

    @mcp.custom_route("/", methods=["GET"])
    async def landing(request: Request) -> Response:
        if "home" not in _pages:
            _pages["home"] = site.render_site()
        return HTMLResponse(_pages["home"])

    @mcp.custom_route("/account", methods=["GET"])
    async def account(request: Request) -> Response:
        status = request.query_params.get("status")
        return HTMLResponse(site.render_account(status=status))

    @mcp.custom_route("/og.svg", methods=["GET"])
    async def og_image(request: Request) -> Response:
        return Response(site.render_og_image(), media_type="image/svg+xml")

    @mcp.custom_route("/robots.txt", methods=["GET"])
    async def robots(request: Request) -> Response:
        return Response(site.robots_txt(), media_type="text/plain")

    @mcp.custom_route("/sitemap.xml", methods=["GET"])
    async def sitemap(request: Request) -> Response:
        return Response(site.sitemap_xml(), media_type="application/xml")

    @mcp.custom_route("/health/capacity", methods=["GET"])
    async def health_capacity(request: Request) -> Response:
        # Non-sensitive: booleans only, no dollar figures. `fresh` confirms the
        # live cost/revenue signals were fetched (i.e. the managed identity works).
        snap = await app.capacity.snapshot()
        payload = {
            "gating_enabled": app.capacity.enabled,
            "free_open": snap.free_open,
            "signals_fresh": snap.fresh,
        }
        return Response(json.dumps(payload), media_type="application/json")

    @mcp.custom_route("/stripe/webhook", methods=["POST"])
    async def stripe_webhook(request: Request) -> Response:
        if not app.billing.enabled:
            return Response("billing disabled", status_code=503)
        payload = await request.body()
        sig = request.headers.get("stripe-signature", "")
        try:
            event = app.billing.parse_event(payload, sig)
        except Exception:  # noqa: BLE001  (bad signature / malformed)
            return Response("invalid signature", status_code=400)
        user_id, plan, customer_id = plan_change_from_event(event)
        if user_id and plan:
            try:
                await app.service.apply_subscription(user_id, plan, customer_id)
            except Exception:  # noqa: BLE001
                # 500 tells Stripe to retry the delivery later.
                return Response("apply failed", status_code=500)
        return Response("ok", status_code=200)

    def _verified(request_code: str):
        """Return (user_id, email) for a valid capability code, else None. Codes
        are valid for their TTL and reusable within it — the manage/token forms
        submit several times per session."""
        try:
            return verify_connect_code(app.cipher, request_code)
        except ConnectCodeError:
            return None

    def _expired() -> Response:
        return HTMLResponse(
            web.render_notice(
                "Link expired",
                "This link is invalid or has expired. Ask MiLatexAI for a fresh "
                "one (start_connect / manage_projects / change_token).",
                icon="⏰",
            ),
            status_code=400,
        )

    def _form_fields(body: bytes):
        fields = parse_qs(body.decode("utf-8", "replace"), keep_blank_values=True)
        return lambda key: (fields.get(key) or [""])[0].strip()

    # -- onboarding: token + first project ---------------------------------

    @mcp.custom_route("/connect", methods=["GET"])
    async def connect_page(request: Request) -> Response:
        ident = _verified(request.query_params.get("code", ""))
        if ident is None:
            return _expired()
        return HTMLResponse(web.render_connect_form(request.query_params["code"], email=ident[1]))

    @mcp.custom_route("/connect", methods=["POST"])
    async def connect_submit(request: Request) -> Response:
        field = _form_fields(await request.body())
        code, overleaf_url, token = field("code"), field("overleaf_url"), field("token")
        name = field("name") or None
        ident = _verified(code)
        if ident is None:
            return _expired()
        user_id, email = ident

        def form_error(msg: str, status: int = 400) -> Response:
            return HTMLResponse(
                web.render_connect_form(code, overleaf_url=overleaf_url,
                                        name=name or "", email=email, error=msg),
                status_code=status,
            )

        if not overleaf_url or not token:
            return form_error("Please provide both your project link and Git token.")
        try:
            await app.service.get_or_create_user(user_id, email, admin_emails=app.admin_emails)
            proj = await app.service.connect_project(user_id, overleaf_url, token, name)
        except (LimitExceeded, ProjectNotConnected, ServiceError) as exc:
            return form_error(str(exc))
        except Exception:  # noqa: BLE001
            return form_error("Something went wrong connecting the project. Please try again.", status=500)
        return HTMLResponse(web.render_success(proj.name, proj.project_id))

    # -- manage the list of projects (add / remove, no token) --------------

    @mcp.custom_route("/projects", methods=["GET"])
    async def projects_page(request: Request) -> Response:
        ident = _verified(request.query_params.get("code", ""))
        if ident is None:
            return _expired()
        user_id, email = ident
        await app.service.get_or_create_user(user_id, email, admin_emails=app.admin_emails)
        projects = await app.service.store.list_projects(user_id)
        return HTMLResponse(web.render_manage_projects(request.query_params["code"], projects, email=email))

    @mcp.custom_route("/projects", methods=["POST"])
    async def projects_submit(request: Request) -> Response:
        field = _form_fields(await request.body())
        code, action = field("code"), field("action")
        ident = _verified(code)
        if ident is None:
            return _expired()
        user_id, email = ident
        await app.service.get_or_create_user(user_id, email, admin_emails=app.admin_emails)

        async def show(error: str | None = None, status: int = 200) -> Response:
            projects = await app.service.store.list_projects(user_id)
            return HTMLResponse(
                web.render_manage_projects(code, projects, email=email, error=error),
                status_code=status,
            )

        try:
            if action == "add":
                url = field("overleaf_url")
                if not url:
                    return await show("Please provide the project link.", 400)
                await app.service.add_project(user_id, url, field("name") or None)
            elif action == "remove":
                await app.service.store.delete_project(user_id, field("project_id"))
            else:
                return await show("Unknown action.", 400)
        except (LimitExceeded, ProjectNotConnected, ServiceError) as exc:
            return await show(str(exc), 400)
        except Exception:  # noqa: BLE001
            return await show("Something went wrong. Please try again.", 500)
        return await show()

    # -- change / revoke the Overleaf token --------------------------------

    @mcp.custom_route("/token", methods=["GET"])
    async def token_page(request: Request) -> Response:
        ident = _verified(request.query_params.get("code", ""))
        if ident is None:
            return _expired()
        user_id, email = ident
        await app.service.get_or_create_user(user_id, email, admin_emails=app.admin_emails)
        has = await app.service.has_token(user_id)
        return HTMLResponse(web.render_token_form(request.query_params["code"], has, email=email))

    @mcp.custom_route("/token", methods=["POST"])
    async def token_submit(request: Request) -> Response:
        field = _form_fields(await request.body())
        code, action = field("code"), field("action")
        ident = _verified(code)
        if ident is None:
            return _expired()
        user_id, email = ident
        await app.service.get_or_create_user(user_id, email, admin_emails=app.admin_emails)
        try:
            if action == "set":
                await app.service.set_token(user_id, field("token"))
                return HTMLResponse(web.render_notice(
                    "Token saved", "Your Overleaf token is updated. You can close "
                    "this tab and go back to your assistant.", icon="✅"))
            if action == "revoke":
                await app.service.revoke_token(user_id)
                return HTMLResponse(web.render_notice(
                    "Token revoked", "The AI's access is removed until you add a "
                    "token again.", icon="🔒"))
        except ServiceError as exc:
            has = await app.service.has_token(user_id)
            return HTMLResponse(web.render_token_form(code, has, email=email, error=str(exc)), status_code=400)
        return HTMLResponse(web.render_notice("Unknown action", "Please try again.", icon="⚠️"), status_code=400)

    return mcp


def _admin_emails_from_env() -> tuple[str, ...]:
    raw = os.environ.get("LEAFBRIDGE_ADMIN_EMAILS", "")
    return tuple(e.strip() for e in raw.split(",") if e.strip())
