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

import asyncio
import base64
import difflib
import json
import os
import secrets
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qs, quote

from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from fastmcp.server.auth.providers.workos import AuthKitProvider
from fastmcp.server.dependencies import get_access_token
from starlette.requests import Request
from starlette.responses import HTMLResponse, RedirectResponse, Response

from . import (
    __version__, arxivprep, citations, figures, latex, paperstats, site,
    texcompile, texdiff, texlocate, tikz, web,
)
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
    AlreadyConnected,
    LimitExceeded,
    ProjectNotConnected,
    ServiceError,
)
from .sessions import SessionsClient, SessionsError
from .store import InMemoryStore, Store, TokenCipher, User
from .web_session import SESSION_TTL_SECONDS, SessionError, mint_session, verify_session
from .workos_web import WorkOSWebAuth

INSTRUCTIONS = """\
MiLatexAI edits the signed-in user's real Overleaf projects over Overleaf's Git
bridge. If the user has no project connected yet, just proceed with their request:
any file tool returns a secure link where they paste their Overleaf Git token
(never in chat) — relay that link and ask them to come back. Refer to a project
by its name; list_projects shows the connected ones. Whenever the user wants to
check, view, change, update, rotate, or revoke their Overleaf token, use
change_token (it returns a secure link; the token itself can't be shown in chat).
To add or remove projects, use manage_projects, or add_project with just a project
URL (no token needed). Every write (edit_file/write_file/delete_file/upload_file) commits and
pushes immediately and counts toward the monthly limit; reads are free and
unlimited. Before editing, read the file so edit_file's old_string matches exactly.
When the user asks why their paper won't compile or why they're getting errors,
run check_compile to build it and get the exact LaTeX errors, then read the
offending file, fix the errors with edit_file, and check_compile again.
When the user says a table or figure "looks weird/off/strange", or asks you to
look at how it renders or to see a picture of it, use show_table or show_figure to
get an IMAGE of the actual rendered float, then fix it with edit_file. The server
does no semantic matching: if the user names the float in words rather than a
number, first read the document (get_sections / read_section) to find its number
or \\label, then pass that to show_table/show_figure. An unknown reference just
returns the list of tables/figures to choose from.
Figure Studio (Pro): to create or change a matplotlib chart, write the Python and
RENDER IT YOURSELF in your own code-execution environment first, show the user
the image, iterate until they approve, and only then call commit_figure with the
exact approved code (it must save figure.pdf). The server re-renders it in an
isolated sandbox and commits both the source and the PDF, so every figure stays
editable later: list_figures shows them, read the figures/src/<name>.py, modify,
and repeat. If you cannot execute Python yourself, commit first and verify with
show_figure instead. BEFORE editing an existing figure, check list_figures: if it
says the PDF was changed outside Figure Studio (or both diverged), the stored
code no longer matches the artifact, so tell the user and ask which version
should win instead of silently regenerating from stale code.
"""

MAX_UPLOAD_BYTES = 50 * 1024 * 1024

# Website sign-in cookies.
SESSION_COOKIE = "mila_session"
STATE_COOKIE = "mila_oauth_state"


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


async def _float_map(repo: Path, main: str) -> str:
    """Best-effort: where each table/figure landed, one line each. Never raises."""
    exe = texcompile.tectonic_path()
    if not exe:
        return ""
    try:
        res = await asyncio.to_thread(texlocate.compile_and_locate, str(repo), main, exe)
    except Exception:  # noqa: BLE001
        return ""
    if not res.floats:
        return ""
    lines = ["Where each table/figure landed (use show_table / show_figure to see one):"]
    for kind, num in sorted(res.floats):
        pg = res.floats[(kind, num)].pages
        if not pg:
            continue
        loc = f"p.{pg[0]}" if len(pg) == 1 else f"p.{pg[0]}-{pg[-1]} (spans)"
        lines.append(f"  {kind.title()} {num}: {loc}")
    labeled = sorted((n, p) for n, (_n, p) in res.labels.items() if n.startswith(("tab", "fig")))
    if labeled:
        lines.append("  Labels: " + ", ".join(f"{n} p.{p}" for n, p in labeled))
    return "\n".join(lines)


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
        web_auth: WorkOSWebAuth | None = None,
        sessions: SessionsClient | None = None,
    ):
        self.service = AccountService(store, cipher)
        self.cipher = cipher
        self.worker = GitWorker(data_dir)
        self.admin_emails = admin_emails
        self._identity = identity_provider
        self._email_resolver = email_resolver
        self.base_url = base_url.rstrip("/")
        self.cookie_secure = self.base_url.startswith("https")
        # Website sign-in (WorkOS AuthKit). A disabled instance just makes /login
        # report it's unavailable; the rest of the site is unaffected.
        self.web_auth = web_auth if web_auth is not None else WorkOSWebAuth(
            api_key="", client_id=""
        )
        # Disabled billing (no Stripe env) is a valid state — the tools just say so.
        self.billing = billing if billing is not None else Billing(
            api_key="", price_id="", webhook_secret="",
            success_url="", cancel_url="", portal_return_url="",
        )
        # No capacity gate given (e.g. tests) -> a disabled gate (free always ok).
        self.capacity = capacity if capacity is not None else CapacityGate(
            subscription_id="", resource_group="", stripe_api_key="",
        )
        # Figure Studio sandbox; disabled (no pool endpoint) is a valid state.
        self.sessions = sessions if sessions is not None else SessionsClient("")
        # Short-lived download bundles (arXiv zips); ephemeral by design.
        self.dl_dir = Path(tempfile.gettempdir()) / "mila_dl"

    def ensure_pro(self, user: User, feature: str) -> None:
        """Gate a paid-only feature. Admin and Pro pass; free users get a clear
        upgrade path instead of a confusing failure."""
        if user.is_admin or user.plan == "pro":
            return
        raise ToolError(
            f"{feature} is a Pro feature ($8.99/mo, unlimited projects and "
            "commits included). Run `upgrade` to unlock it."
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

    async def session_user(self, request: Request) -> User | None:
        """Resolve the signed-in website user from the session cookie, or None.

        The cookie carries only identity; the current plan / Stripe customer id
        are read fresh from the store (they change via webhooks)."""
        cookie = request.cookies.get(SESSION_COOKIE, "")
        if not cookie:
            return None
        try:
            user_id, email = verify_session(self.cipher, cookie)
        except SessionError:
            return None
        try:
            return await self.service.get_or_create_user(
                user_id, email, admin_emails=self.admin_emails
            )
        except Exception:  # noqa: BLE001
            return None

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
        allow_empty: bool = False,
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
            result = await self.worker.commit_and_push(proj, message, allow_empty=allow_empty)
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
    web_auth: WorkOSWebAuth | None = None,
    sessions: SessionsClient | None = None,
) -> FastMCP:
    """Build the hosted server. Tests pass ``auth=False`` + a fake
    ``identity_provider`` + an ``InMemoryStore`` to drive it without WorkOS."""
    store = store if store is not None else InMemoryStore()
    if cipher is None:
        cipher, _ = TokenCipher.from_env()
    resolved_base = base_url or os.environ.get("BASE_URL", "http://localhost:8000")
    if email_resolver is None and os.environ.get("WORKOS_API_KEY"):
        email_resolver = workos_email_resolver(os.environ["WORKOS_API_KEY"])
    if web_auth is None and os.environ.get("WORKOS_API_KEY") and os.environ.get("WORKOS_CLIENT_ID"):
        web_auth = WorkOSWebAuth(
            api_key=os.environ["WORKOS_API_KEY"],
            client_id=os.environ["WORKOS_CLIENT_ID"],
        )
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
        web_auth=web_auth,
        sessions=sessions if sessions is not None else SessionsClient.from_env(),
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
        """Connect one of your LaTeX repositories (run once per repo). The Git
        token is stored ENCRYPTED and never shown again. Prefer start_connect,
        which keeps your token out of the chat.

        Args:
            overleaf_url: The repository URL — an Overleaf project
                (https://www.overleaf.com/project/<id>), or a GitHub, GitLab,
                Bitbucket, or self-hosted HTTPS Git repo URL.
            token: The matching access token. Overleaf: Account Settings > Git
                Integration. GitHub: a fine-grained PAT with Contents read/write.
                GitLab: a project access token with read/write repository.
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
            return ("No projects connected yet. Use connect_project with your "
                    "Overleaf, GitHub, or GitLab repo link + its access token.")
        return "\n".join(f"- {p.name}  (id {p.project_id})" for p in projects)

    @mcp.tool
    async def add_project(
        overleaf_url: str, name: str | None = None, token: str | None = None
    ) -> str:
        """Give the AI access to ANOTHER of your LaTeX repositories. For an
        Overleaf project this reuses the Overleaf token you already saved — just
        the project link, no token needed. For a GitHub, GitLab, Bitbucket, or
        self-hosted Git repo you must also pass that repo's access token. (Run
        start_connect first if you've never connected one.)

        Args:
            overleaf_url: The repository URL — an Overleaf project, or a GitHub,
                GitLab, Bitbucket, or self-hosted HTTPS Git repo URL.
            name: A short label for the project (optional).
            token: The repo's access token — required for non-Overleaf repos,
                omit for Overleaf (the saved account token is reused).
        """
        try:
            user = await app.user()
            proj = await app.service.add_project(user.user_id, overleaf_url, name, token=token)
        except AlreadyConnected as exc:
            return str(exc)
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
        """Manage your stored Overleaf Git token. Use this whenever the user wants
        to check, view, see, change, update, rotate, or revoke their token: it
        returns a secure link to a web page for that. The token itself can't be
        shown in chat (it's encrypted), but this is how they update or revoke it."""
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
        $8.99/mo, local currency where available). Returns a secure Stripe checkout
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
        """Compile a project with a local LaTeX engine and report whether it builds,
        with the exact LaTeX errors and warnings. Use this whenever the user asks
        why their paper won't compile, why they're getting errors, or wants to
        verify a project builds. After it reports errors you can read the offending
        file, fix them with edit_file, and run this again to confirm."""
        try:
            user = await app.user()
            await app.ensure_capacity(user)
            proj = await app.resolve_or_onboard(user, project)
            float_map = ""
            async with app.worker.open_repo(proj) as repo:
                main = texcompile.find_main_tex(repo)
                if not main:
                    raise ToolError("Could not find a root .tex to compile.")
                res = await texcompile.compile_project(repo, main)
                if res.ok:
                    float_map = await _float_map(repo, main)
        except Exception as exc:  # noqa: BLE001
            raise _wrap(exc)
        if not res.available:
            return f"Compile check unavailable: {res.message}"
        lines = [f"{main}: {res.message}"]
        if not res.ok and res.errors:
            lines += ["Errors:"] + [f"  {e}" for e in res.errors]
        if float_map:
            lines += ["", float_map]
        return "\n".join(lines)

    async def _show_float(kind: str, ref: str, project: str | None):
        try:
            user = await app.user()
            await app.ensure_capacity(user)
            proj = await app.resolve_or_onboard(user, project)
            exe = texcompile.tectonic_path()
            if not exe:
                raise ToolError("The LaTeX engine is unavailable on the server right now.")
            async with app.worker.open_repo(proj) as repo:
                main = texcompile.find_main_tex(repo)
                if not main:
                    raise ToolError("Could not find a root .tex to compile.")
                res = await asyncio.to_thread(texlocate.compile_and_locate, str(repo), main, exe)
                number = texlocate.resolve_number(ref, res)
                f = res.floats.get((kind, number)) if number is not None else None
                if f is None or not f.pages:
                    # Can't pin it down. The server does NO semantic matching, so
                    # hand back the list and let the assistant pick the right number
                    # or label (that resolution is the model's job).
                    return (
                        f"I couldn't identify {kind} {ref!r}. "
                        + texlocate.float_listing(res, kind)
                        + f"\n\nTell me the {kind} number or its \\label and I'll show it."
                    )
                pages = f.pages
                imgs = await asyncio.to_thread(texlocate.render_pages, res.pdf_path, pages)
        except Exception as exc:  # noqa: BLE001
            raise _wrap(exc)
        if not imgs:
            raise ToolError("The page rendered empty; nothing to show.")
        from fastmcp.utilities.types import Image

        span = "" if len(pages) == 1 else f" (spans pages {pages[0]}-{pages[-1]})"
        note = f"{kind.title()} {number}: page {pages[0]}{span}."
        return [note, *[Image(data=b, format="png") for b in imgs]]

    @mcp.tool(annotations={"readOnlyHint": True})
    async def show_table(table: str, project: str | None = None):
        """Show a rendered IMAGE of a table so you can SEE how it actually looks
        (layout, column widths, overfull or misaligned cells) and fix it, things the
        LaTeX source alone can't tell you. Pass the table NUMBER (e.g. "4") or its
        \\label (e.g. "tab:results"). If the user describes a table in words, first
        read the document (get_sections / read_section) to find its number or label,
        then call this. A table that spans multiple pages returns all of them; an
        unknown reference returns the list of tables so you can pick."""
        return await _show_float("table", table, project)

    @mcp.tool(annotations={"readOnlyHint": True})
    async def show_figure(figure: str, project: str | None = None):
        """Show a rendered IMAGE of a figure so you can SEE how it renders. Pass the
        figure NUMBER (e.g. "3") or its \\label. If the user describes it in words,
        read the document first to find its number or label, then call this. Spanning
        figures return all pages; an unknown reference returns the list of figures."""
        return await _show_float("figure", figure, project)

    # -- tracked changes + arXiv export ---------------------------------------

    @mcp.tool(annotations={"readOnlyHint": True})
    async def tracked_changes_pdf(ref: str, project: str | None = None):
        """A tracked-changes PDF (latexdiff): additions and deletions between a
        commit/checkpoint id (list_checkpoints / get_history) and the CURRENT
        document, rendered as page images — what journals ask for in a revised
        submission. Nothing is committed."""
        try:
            user = await app.user()
            await app.ensure_capacity(user)
            proj = await app.resolve_or_onboard(user, project)
            async with app.worker.open_repo(proj) as repo:
                main = texcompile.find_main_tex(repo)
                if not main:
                    raise ToolError("Could not find a root .tex to diff.")
                old = await app.worker.show_file(proj, ref.strip(), main)
                pdf = await texdiff.diff_pdf(repo, main, old)
            pngs = texdiff.pdf_pages_to_pngs(pdf)
        except texdiff.TexDiffError as exc:
            raise ToolError(str(exc))
        except Exception as exc:  # noqa: BLE001
            raise _wrap(exc)
        from fastmcp.utilities.types import Image

        note = (f"Tracked changes {ref} -> current ({len(pngs)} page(s) shown, "
                "additions/deletions marked).")
        return [note, *[Image(data=p, format="png") for p in pngs]]

    @mcp.tool(annotations={"readOnlyHint": True})
    async def arxiv_export(project: str | None = None) -> str:
        """Prepare an arXiv-ready submission zip: flattens all \\input/\\include
        into one main.tex, strips comment lines, includes the precompiled
        bibliography (.bbl, which arXiv requires since it will not run bibtex),
        referenced graphics, and any custom .cls/.bst/.sty. Returns a download
        link (valid ~15 minutes)."""
        try:
            user = await app.user()
            await app.ensure_capacity(user)
            proj = await app.resolve_or_onboard(user, project)
            async with app.worker.open_repo(proj) as repo:
                main = texcompile.find_main_tex(repo)
                if not main:
                    raise ToolError("Could not find a root .tex to export.")
                bbl = await arxivprep.compile_bbl(repo, main)
                blob, manifest = arxivprep.build_zip(repo, main, bbl)
            app.dl_dir.mkdir(parents=True, exist_ok=True)
            fname = secrets.token_urlsafe(10) + ".zip"
            (app.dl_dir / fname).write_bytes(blob)
            code = app.cipher.encrypt(json.dumps({"k": "dl", "f": fname}))
            url = f"{app.base_url}/dl?code={quote(code, safe='')}"
        except Exception as exc:  # noqa: BLE001
            raise _wrap(exc)
        files = "\n".join(f"  - {m}" for m in manifest)
        bbl_note = "" if bbl else ("\nNOTE: no .bbl was produced (no bibliography or "
                                   "the compile failed); arXiv may need it.")
        return (f"arXiv submission bundle ready ({len(blob)} bytes):\n{files}{bbl_note}\n\n"
                f"Download (about 15 minutes): {url}")

    # -- version safety: checkpoints, diffs, restores -------------------------

    @mcp.tool
    async def checkpoint(name: str, project: str | None = None) -> str:
        """Save a named restore point of the project RIGHT NOW (before a big
        rewrite, before a deadline push). Cheap and instant; restore any file
        later with restore_file. Counts as one commit."""
        try:
            user = await app.user()
            await app.ensure_capacity(user)
            proj = await app.resolve_or_onboard(user, project)
            label = (name or "checkpoint").strip()[:60]
            result = await app.apply_and_push(
                user, proj, lambda repo: None, f"CHECKPOINT: {label}", allow_empty=True
            )
        except Exception as exc:  # noqa: BLE001
            raise _wrap(exc)
        return f"{result}\nCheckpoint {label!r} saved. See them with list_checkpoints."

    @mcp.tool(annotations={"readOnlyHint": True})
    async def list_checkpoints(project: str | None = None) -> str:
        """List saved checkpoints (newest first) with their commit ids, for
        project_diff / restore_file."""
        try:
            user = await app.user()
            await app.ensure_capacity(user)
            proj = await app.resolve_or_onboard(user, project)
            async with app.worker.lock_for(proj):
                out = await app.worker.log_matching(proj, "CHECKPOINT:")
        except Exception as exc:  # noqa: BLE001
            raise _wrap(exc)
        return out or "No checkpoints yet. Create one with checkpoint('before rewrite')."

    @mcp.tool(annotations={"readOnlyHint": True})
    async def project_diff(ref: str, project: str | None = None) -> str:
        """What changed since a commit/checkpoint id (from list_checkpoints or
        get_history): per-file change summary."""
        try:
            user = await app.user()
            await app.ensure_capacity(user)
            proj = await app.resolve_or_onboard(user, project)
            async with app.worker.lock_for(proj):
                out = await app.worker.diff_stat(proj, ref.strip())
        except Exception as exc:  # noqa: BLE001
            raise _wrap(exc)
        return out or f"No changes since {ref}."

    @mcp.tool
    async def restore_file(path: str, ref: str, project: str | None = None) -> str:
        """Restore one file to its content at a commit/checkpoint id and commit
        the restoration (undo for AI edits gone wrong). Counts as one commit."""
        try:
            user = await app.user()
            await app.ensure_capacity(user)
            proj = await app.resolve_or_onboard(user, project)
            async with app.worker.lock_for(proj):
                old = await app.worker.show_file(proj, ref.strip(), path)

            def mutate(repo: Path) -> None:
                write_text_exact(safe_join(repo, path), old)

            return await app.apply_and_push(
                user, proj, mutate, f"Restore {path} from {ref} (MiLatexAI)",
                guard_path=path, allow_shrink=True,
            )
        except Exception as exc:  # noqa: BLE001
            raise _wrap(exc)

    # -- citation toolkit -----------------------------------------------------

    @mcp.tool
    async def add_citation(
        reference: str, bib_file: str | None = None, project: str | None = None
    ) -> str:
        """Add a VERIFIED citation to the bibliography. Give a DOI (10.xxxx/...)
        or arXiv id (2301.01234); the server fetches the real BibTeX from
        doi.org / arXiv (never trusting model memory, so no hallucinated
        references), de-duplicates, appends to the .bib, and commits. Returns
        the entry key to use in \\cite{...}. Counts as one commit."""
        try:
            user = await app.user()
            await app.ensure_capacity(user)
            proj = await app.resolve_or_onboard(user, project)
            entry = await citations.fetch_bibtex(reference)
            key = citations.entry_key(entry)
            if not key:
                raise ToolError("The registry returned unusable BibTeX; try the DOI instead.")
            async with app.worker.open_repo(proj) as repo:
                bibs = [p.relative_to(repo).as_posix() for p in repo.rglob("*.bib")
                        if ".git" not in p.parts]
                target = bib_file or (bibs[0] if len(bibs) == 1 else None)
                if target is None:
                    raise ToolError(
                        f"Multiple .bib files ({', '.join(bibs)}); pass bib_file."
                        if bibs else "No .bib file in the project; create one with write_file first."
                    )
                existing = read_text(safe_join(repo, target)) if (repo / target).is_file() else ""
            if key in citations.bib_keys(existing):
                return f"'{key}' is already in {target}; cite it with \\cite{{{key}}}."

            def mutate(repo: Path) -> None:
                t = safe_join(repo, target)
                cur = read_text(t) if t.is_file() else ""
                sep = "" if (not cur or cur.endswith("\n\n")) else ("\n" if cur.endswith("\n") else "\n\n")
                write_text_exact(t, cur + sep + entry.rstrip("\n") + "\n")

            result = await app.apply_and_push(
                user, proj, mutate, f"Add citation {key} (MiLatexAI)"
            )
        except Exception as exc:  # noqa: BLE001
            raise _wrap(exc)
        return f"{result}\nAdded {key!r} to {target}. Cite it with \\cite{{{key}}}."

    @mcp.tool(annotations={"readOnlyHint": True})
    async def check_citations(project: str | None = None) -> str:
        """Bibliography integrity check: \\cite keys with no .bib entry
        (broken/hallucinated) and .bib entries never cited."""
        try:
            user = await app.user()
            await app.ensure_capacity(user)
            proj = await app.resolve_or_onboard(user, project)
            async with app.worker.open_repo(proj) as repo:
                cited: set[str] = set()
                have: set[str] = set()
                for p in repo.rglob("*.tex"):
                    if ".git" not in p.parts:
                        cited |= citations.cite_keys(p.read_text(encoding="utf-8", errors="replace"))
                for p in repo.rglob("*.bib"):
                    if ".git" not in p.parts:
                        have |= citations.bib_keys(p.read_text(encoding="utf-8", errors="replace"))
        except Exception as exc:  # noqa: BLE001
            raise _wrap(exc)
        undefined = sorted(cited - have)
        unused = sorted(have - cited)
        lines = [f"{len(cited)} cited key(s), {len(have)} bibliography entr(ies)."]
        if undefined:
            lines.append("UNDEFINED (cited but not in any .bib, fix or add_citation): "
                         + ", ".join(undefined))
        if unused:
            lines.append("Unused .bib entries (never cited): " + ", ".join(unused))
        if not undefined and not unused:
            lines.append("All citations resolve and every entry is used.")
        return "\n".join(lines)

    @mcp.tool(annotations={"readOnlyHint": True})
    async def project_stats(project: str | None = None) -> str:
        """Word counts per .tex file (approximate, comments/commands stripped),
        TODO/FIXME markers, and undefined or unused \\ref labels, for trimming
        to journal limits and pre-submission checks."""
        try:
            user = await app.user()
            await app.ensure_capacity(user)
            proj = await app.resolve_or_onboard(user, project)
            async with app.worker.open_repo(proj) as repo:
                a = paperstats.analyze(repo)
        except Exception as exc:  # noqa: BLE001
            raise _wrap(exc)
        lines = [f"~{a['total']} words across {len(a['counts'])} .tex file(s):"]
        lines += [f"  {f}: ~{n}" for f, n in sorted(a["counts"].items(), key=lambda x: -x[1])]
        if a["todos"]:
            lines.append("TODO markers:")
            lines += [f"  {t}" for t in a["todos"]]
        if a["undefined_refs"]:
            lines.append("Undefined \\ref targets: " + ", ".join(a["undefined_refs"]))
        if a["unused_labels"]:
            lines.append("Labels never referenced: " + ", ".join(a["unused_labels"]))
        return "\n".join(lines)

    # -- Figure Studio (Pro): matplotlib figures with their source kept -------

    _FIG_SHIM = (
        "import os as _os, contextlib as _ctx\n"
        "with _ctx.suppress(FileNotFoundError):\n"
        "    _os.remove('/mnt/data/figure.pdf')\n"
        "_os.chdir('/mnt/data')\n"
    )

    @mcp.tool
    async def commit_figure(
        code: str, name: str, format: str = "png", project: str | None = None
    ):
        """Save an APPROVED matplotlib figure into the user's Overleaf project (Pro).
        FLOW: write the Python and render it YOURSELF in your own code-execution
        environment first, show the user the image, and only call this after they
        approve. The server re-runs the code in an isolated sandbox (matplotlib 3.8,
        numpy 1.26; no network, no pip, no other libraries) and commits BOTH the
        source (figures/src/<name>.py, so the figure stays editable forever) and the
        rendered artifact (figures/<name>.pdf or .png) in one push. The code MUST
        save exactly one file named figure.pdf in the working directory, e.g.
        fig.savefig('figure.pdf'); seed any randomness. format: "png" (default,
        300 dpi, what most projects use) or "pdf" (vector) only when the user
        asks for it or the project's \\includegraphics files are PDFs.
        Overwrites an existing figure with the same name; counts as one commit
        toward the monthly limit."""
        try:
            user = await app.user()
            app.ensure_pro(user, "Figure Studio (creating and editing matplotlib figures)")
            await app.ensure_capacity(user)
            proj = await app.resolve_or_onboard(user, project)
            slug = figures.slugify(name)
            fmt = (format or "png").strip().lower().lstrip(".")
            if fmt not in ("pdf", "png"):
                raise ToolError("format must be 'pdf' or 'png'.")
            if not app.sessions.enabled:
                raise ToolError("Figure Studio isn't available on this server right now.")

            sess = app.sessions.session_for(user.user_id)
            res = await app.sessions.execute(sess, _FIG_SHIM + code)
            if not res.ok:
                tail = (res.stderr or res.detail or "unknown error")[-800:]
                raise ToolError(f"The figure code failed in the sandbox:\n{tail}")
            try:
                pdf = await app.sessions.download(sess, "figure.pdf")
            except SessionsError:
                have = await app.sessions.list_files(sess)
                stderr = (res.stderr or "")[-400:]
                raise ToolError(
                    "The code ran but produced no figure.pdf. It must call "
                    "savefig('figure.pdf'). Files it did produce: "
                    f"{have or 'none'}.{(' stderr: ' + stderr) if stderr else ''}"
                )
            if not pdf.startswith(b"%PDF"):
                raise ToolError("The produced figure.pdf is not a valid PDF; fix the savefig call.")

            # The committed artifact: the vector PDF itself, or a 300-dpi PNG
            # rasterized from that same PDF (identical drawing, user's format).
            artifact = pdf if fmt == "pdf" else figures.pdf_to_png(pdf, dpi=300)
            src_rel = figures.src_path(slug)
            out_rel = figures.out_path(slug, fmt)
            other_rel = figures.out_path(slug, "png" if fmt == "pdf" else "pdf")
            body = code.rstrip("\n") + "\n"
            file_body = figures.build_header(
                slug, code_body=body, pdf_bytes=artifact, ext=fmt) + body

            def mutate(repo: Path) -> None:
                src_t = safe_join(repo, src_rel)
                out_t = safe_join(repo, out_rel)
                src_t.parent.mkdir(parents=True, exist_ok=True)
                out_t.parent.mkdir(parents=True, exist_ok=True)
                write_text_exact(src_t, file_body)
                write_bytes_exact(out_t, artifact)
                # A format switch must not leave a stale sibling artifact behind.
                sibling = safe_join(repo, other_rel)
                if sibling.is_file():
                    sibling.unlink()

            result = await app.apply_and_push(
                user, proj, mutate, f"Figure {slug} (MiLatexAI Figure Studio)"
            )
        except Exception as exc:  # noqa: BLE001
            raise _wrap(exc)
        from fastmcp.utilities.types import Image

        note = (
            f"{result}\n\nCommitted {src_rel} (editable source) and {out_rel} "
            f"({len(artifact)} bytes). Include it with:\n"
            f"\\begin{{figure}}[t]\\centering\n"
            f"  \\includegraphics[width=\\linewidth]{{{out_rel}}}\n"
            f"  \\caption{{...}}\\label{{fig:{slug}}}\n\\end{{figure}}\n"
            "Below is the exact committed artifact."
        )
        try:
            png = artifact if fmt == "png" else figures.pdf_to_png(pdf)
        except Exception:  # noqa: BLE001  (preview is best-effort)
            return note
        return [note, Image(data=png, format="png")]

    @mcp.tool
    async def commit_tikz(
        code: str, name: str, format: str = "png", project: str | None = None
    ):
        """Save an APPROVED TikZ diagram into the user's Overleaf project (Pro).
        Give the tikzpicture code (a bare snippet is fine; it is wrapped in a
        standalone document with pgfplots available). The server compiles it,
        commits BOTH the source (figures/src/<name>.tex, editable forever) and
        the rendered artifact (figures/<name>.png at 300 dpi, or .pdf), and
        returns the rendered image. Show the user the result; on a compile error
        you get the exact LaTeX errors to fix. Counts as one commit."""
        try:
            user = await app.user()
            app.ensure_pro(user, "TikZ Studio (creating and editing TikZ diagrams)")
            await app.ensure_capacity(user)
            proj = await app.resolve_or_onboard(user, project)
            slug = figures.slugify(name)
            fmt = (format or "png").strip().lower().lstrip(".")
            if fmt not in ("pdf", "png"):
                raise ToolError("format must be 'png' or 'pdf'.")
            try:
                pdf = await tikz.render_pdf(code)
            except tikz.TikzError as exc:
                raise ToolError(str(exc))
            artifact = pdf if fmt == "pdf" else figures.pdf_to_png(pdf, dpi=300)
            src_rel = figures.src_path(slug, "tex")
            out_rel = figures.out_path(slug, fmt)
            other_rel = figures.out_path(slug, "png" if fmt == "pdf" else "pdf")
            body = code.rstrip("\n") + "\n"
            file_body = figures.build_header(
                slug, code_body=body, pdf_bytes=artifact, ext=fmt, comment="%") + body

            def mutate(repo: Path) -> None:
                src_t = safe_join(repo, src_rel)
                out_t = safe_join(repo, out_rel)
                src_t.parent.mkdir(parents=True, exist_ok=True)
                out_t.parent.mkdir(parents=True, exist_ok=True)
                write_text_exact(src_t, file_body)
                write_bytes_exact(out_t, artifact)
                sibling = safe_join(repo, other_rel)
                if sibling.is_file():
                    sibling.unlink()

            result = await app.apply_and_push(
                user, proj, mutate, f"TikZ {slug} (MiLatexAI TikZ Studio)"
            )
        except Exception as exc:  # noqa: BLE001
            raise _wrap(exc)
        from fastmcp.utilities.types import Image

        note = (
            f"{result}\n\nCommitted {src_rel} (editable TikZ source) and {out_rel}. "
            f"Include it with \\includegraphics[width=\\linewidth]{{{out_rel}}} "
            f"and \\label{{fig:{slug}}}. Below is the rendered diagram."
        )
        try:
            png = artifact if fmt == "png" else figures.pdf_to_png(pdf)
        except Exception:  # noqa: BLE001
            return note
        return [note, Image(data=png, format="png")]

    @mcp.tool(annotations={"readOnlyHint": True})
    async def list_figures(project: str | None = None) -> str:
        """List the project's Figure Studio figures (Pro): each managed figure's
        slug, source file, and whether its rendered output exists, plus recently
        DELETED figures that can still be recovered from git history. To edit one,
        read its figures/src/<slug>.py, change the code, re-render for the user,
        and commit_figure again with the same name."""
        try:
            user = await app.user()
            app.ensure_pro(user, "Figure Studio (creating and editing matplotlib figures)")
            await app.ensure_capacity(user)
            proj = await app.resolve_or_onboard(user, project)
            async with app.worker.open_repo(proj) as repo:
                found = figures.scan_figures(repo)
                states = {f.slug: figures.sync_state(repo, f) for f in found}
                raw_log = await app.worker.log_deleted(proj, figures.SRC_DIR + "/")
            live = {f.slug for f in found}
            deleted = figures.parse_deleted(raw_log, live)
        except Exception as exc:  # noqa: BLE001
            raise _wrap(exc)
        if not found and not deleted:
            return ("No Figure Studio figures in this project yet. Create one: write "
                    "matplotlib code, render it for the user, then commit_figure.")
        lines = []
        if found:
            lines.append(f"{len(found)} managed figure(s) in {proj.name!r}:")
            state_msgs = {
                figures.IN_SYNC: "ok, in sync (the source code is ground truth)",
                figures.CODE_EDITED: ("source was edited since the last render; the PDF is "
                                      "STALE. Re-render and commit_figure to update it"),
                figures.ARTIFACT_REPLACED: ("PDF was changed OUTSIDE Figure Studio; the stored "
                                            "code is NOT ground truth anymore. Ask the user "
                                            "which version wins before editing"),
                figures.DIVERGED: ("both the code and the PDF changed independently; ask the "
                                   "user which is authoritative before touching either"),
                figures.OUTPUT_MISSING: "output missing (re-run commit_figure)",
                figures.UNTRACKED: "no provenance record (hand-made or pre-tracking)",
            }
            for f in found:
                state = states.get(f.slug, figures.UNTRACKED)
                lines.append(f"- {f.slug}  (source {f.src}, output {f.out}: {state_msgs[state]})")
        if deleted:
            lines.append("Deleted figures still recoverable from git history:")
            for slug, commit in deleted.items():
                lines.append(
                    f"- {slug}  (deleted; recover the code with: git show {commit}^:{figures.src_path(slug)})"
                )
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
        """Create a BRAND-NEW file or overwrite an existing one, then commit+push.
        The file does not need to exist first and parent folders are created
        automatically, so use this freely for new .tex/.bib/.sty files (e.g. a new
        section file or a preamble you found for the user)."""
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
        user = await app.session_user(request)
        if user is None:
            return HTMLResponse(site.render_account(
                status=status, signed_in=False, billing_enabled=app.billing.enabled))
        projects = await app.service.store.list_projects(user.user_id)
        view = {
            "email": user.email,
            "plan": "pro" if (user.is_admin or user.plan == "pro") else "free",
            "is_admin": user.is_admin,
            "has_customer": bool(user.stripe_customer_id),
        }
        return HTMLResponse(site.render_account(
            status=status, signed_in=True, account=view,
            billing_enabled=app.billing.enabled, has_projects=bool(projects)))

    # -- website sign-in (WorkOS AuthKit) so a user can manage billing on the web
    # with the SAME account as the connector. The session cookie is signed,
    # HttpOnly, and SameSite=Lax — Lax also stops a cross-site POST from carrying
    # it, which is the CSRF guard for the billing actions below.

    @mcp.custom_route("/login", methods=["GET"])
    async def login(request: Request) -> Response:
        if not app.web_auth.enabled:
            return HTMLResponse(web.render_notice(
                "Sign-in unavailable",
                "Website sign-in isn't set up yet. You can manage billing inside "
                "Claude or ChatGPT.", icon="⚠️"), status_code=503)
        state = secrets.token_urlsafe(24)
        url = app.web_auth.authorization_url(
            redirect_uri=f"{app.base_url}/callback", state=state)
        resp = RedirectResponse(url, status_code=303)
        resp.set_cookie(STATE_COOKIE, state, max_age=600, httponly=True,
                        secure=app.cookie_secure, samesite="lax", path="/")
        return resp

    @mcp.custom_route("/callback", methods=["GET"])
    async def callback(request: Request) -> Response:
        params = request.query_params
        if params.get("error"):
            # Don't reflect the provider's error_description (attacker-controllable
            # query text on our own domain) — show a fixed message.
            return HTMLResponse(web.render_notice(
                "Sign-in failed",
                "Sign-in was cancelled or could not be completed. Please try again.",
                icon="⚠️"), status_code=400)
        code = params.get("code", "")
        state = params.get("state", "")
        cookie_state = request.cookies.get(STATE_COOKIE, "")
        if not (code and state and cookie_state
                and secrets.compare_digest(state, cookie_state)):
            return HTMLResponse(web.render_notice(
                "Sign-in failed",
                "This sign-in attempt is invalid or expired. Please try again.",
                icon="⚠️"), status_code=400)
        try:
            user_id, email = await app.web_auth.authenticate(code)
        except Exception:  # noqa: BLE001
            return HTMLResponse(web.render_notice(
                "Sign-in failed",
                "We couldn't complete sign-in. Please try again.", icon="⚠️"),
                status_code=502)
        await app.service.get_or_create_user(
            user_id, email, admin_emails=app.admin_emails)
        resp = RedirectResponse("/account", status_code=303)
        resp.set_cookie(SESSION_COOKIE, mint_session(app.cipher, user_id, email),
                        max_age=SESSION_TTL_SECONDS, httponly=True,
                        secure=app.cookie_secure, samesite="lax", path="/")
        resp.delete_cookie(STATE_COOKIE, path="/")
        return resp

    @mcp.custom_route("/logout", methods=["POST"])
    async def logout(request: Request) -> Response:
        # Only clear when a valid session is actually present. A cross-site POST
        # can't carry the SameSite=Lax session cookie, so this makes forced-logout
        # CSRF a no-op while a real (same-site) sign-out still works.
        user = await app.session_user(request)
        resp = RedirectResponse("/", status_code=303)
        if user is not None:
            resp.delete_cookie(SESSION_COOKIE, path="/")
        return resp

    @mcp.custom_route("/account/upgrade", methods=["POST"])
    async def account_upgrade(request: Request) -> Response:
        user = await app.session_user(request)
        if user is None:
            return RedirectResponse("/login", status_code=303)
        if user.is_admin or user.plan == "pro":
            return RedirectResponse("/account", status_code=303)
        if not app.billing.enabled:
            return HTMLResponse(web.render_notice(
                "Billing unavailable",
                "Upgrades are briefly unavailable. Please try again soon.",
                icon="⚠️"), status_code=503)
        try:
            url, cid = await app.billing.create_checkout(user, user.stripe_customer_id)
            await app.service.set_stripe_customer(user.user_id, cid)
        except Exception:  # noqa: BLE001
            return HTMLResponse(web.render_notice(
                "Something went wrong",
                "We couldn't start checkout. Please try again.", icon="⚠️"),
                status_code=502)
        return RedirectResponse(url, status_code=303)

    @mcp.custom_route("/account/manage", methods=["POST"])
    async def account_manage(request: Request) -> Response:
        user = await app.session_user(request)
        if user is None:
            return RedirectResponse("/login", status_code=303)
        if not user.stripe_customer_id or not app.billing.enabled:
            return RedirectResponse("/account", status_code=303)
        try:
            url = await app.billing.create_portal(user.stripe_customer_id)
        except Exception:  # noqa: BLE001
            return HTMLResponse(web.render_notice(
                "Something went wrong",
                "We couldn't open the billing portal. Please try again.",
                icon="⚠️"), status_code=502)
        return RedirectResponse(url, status_code=303)

    @mcp.custom_route("/og.svg", methods=["GET"])
    async def og_image(request: Request) -> Response:
        return Response(site.render_og_image(), media_type="image/svg+xml")

    @mcp.custom_route("/robots.txt", methods=["GET"])
    async def robots(request: Request) -> Response:
        return Response(site.robots_txt(), media_type="text/plain")

    @mcp.custom_route("/llms.txt", methods=["GET"])
    async def llms(request: Request) -> Response:
        return Response(site.llms_txt(), media_type="text/plain")

    @mcp.custom_route(f"/{site.INDEXNOW_KEY}.txt", methods=["GET"])
    async def indexnow_key(request: Request) -> Response:
        # IndexNow ownership proof: the key file simply contains the key.
        return Response(site.INDEXNOW_KEY, media_type="text/plain")

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
            "latex_available": bool(texcompile.tectonic_path()),
            "figure_studio": app.sessions.enabled,
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

    @mcp.custom_route("/dl", methods=["GET"])
    async def download(request: Request) -> Response:
        from starlette.responses import FileResponse

        code = request.query_params.get("code", "")
        try:
            data = json.loads(app.cipher.decrypt(code, ttl=900))
            assert data.get("k") == "dl"
            fname = os.path.basename(str(data["f"]))
        except Exception:  # noqa: BLE001
            return HTMLResponse(web.render_notice(
                "Link expired", "This download link is invalid or has expired. "
                "Run arxiv_export again for a fresh one.", icon="⏰"), status_code=400)
        target = app.dl_dir / fname
        if not target.is_file():
            return HTMLResponse(web.render_notice(
                "Bundle gone", "This bundle is no longer on the server (it restarted). "
                "Run arxiv_export again.", icon="⏰"), status_code=410)
        return FileResponse(target, filename="arxiv-submission.zip",
                            media_type="application/zip")

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
        user_id, email = ident
        # If the account already has a saved token, this is an "add another
        # project" case — render the form WITHOUT the token field.
        await app.service.get_or_create_user(user_id, email, admin_emails=app.admin_emails)
        has = await app.service.has_token(user_id)
        return HTMLResponse(
            web.render_connect_form(request.query_params["code"], email=email, has_token=has)
        )

    @mcp.custom_route("/connect", methods=["POST"])
    async def connect_submit(request: Request) -> Response:
        field = _form_fields(await request.body())
        code, overleaf_url, token = field("code"), field("overleaf_url"), field("token")
        name = field("name") or None
        ident = _verified(code)
        if ident is None:
            return _expired()
        user_id, email = ident
        await app.service.get_or_create_user(user_id, email, admin_emails=app.admin_emails)
        has = await app.service.has_token(user_id)

        def form_error(msg: str, status: int = 400) -> Response:
            return HTMLResponse(
                web.render_connect_form(code, overleaf_url=overleaf_url,
                                        name=name or "", email=email, error=msg, has_token=has),
                status_code=status,
            )

        if not overleaf_url:
            return form_error("Please provide your project link.")
        if not token and not has:
            return form_error("Please provide both your project link and Git token.")
        try:
            if token:
                # First connection, or the user is (re)setting the account token here.
                proj = await app.service.connect_project(user_id, overleaf_url, token, name)
            else:
                # Returning user: reuse the saved account token, no re-entry needed.
                proj = await app.service.add_project(user_id, overleaf_url, name)
        except AlreadyConnected as exc:
            return HTMLResponse(web.render_notice("Already connected", str(exc), icon="✅"))
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
                await app.service.add_project(
                    user_id, url, field("name") or None, token=field("token") or None
                )
            elif action == "remove":
                await app.service.store.delete_project(user_id, field("project_id"))
            else:
                return await show("Unknown action.", 400)
        except AlreadyConnected as exc:
            return await show(str(exc))
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
