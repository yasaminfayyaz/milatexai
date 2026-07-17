"""Phase 2 hosted business logic: onboarding, per-user project resolution, and
usage metering — sitting on top of the encrypted :mod:`store`.

This is the multi-tenant brain: given an authenticated WorkOS user, connect
their Overleaf project (token encrypted), resolve it back to a
:class:`ProjectConfig` the git worker understands, and enforce the free-tier
limits. It is pure logic (no FastMCP, no git, no network), so it tests cleanly
against :class:`InMemoryStore`.
"""

from __future__ import annotations

from .config import ConfigError, ProjectConfig, extract_project_id
from .store import (
    Project,
    Store,
    TokenCipher,
    User,
    monthly_commit_limit,
    project_limit,
)

UPGRADE_URL = "https://milatexai.com/upgrade"


class ServiceError(Exception):
    """Base for user-facing hosted-mode errors."""


class LimitExceeded(ServiceError):
    pass


class ProjectNotConnected(ServiceError):
    pass


class AlreadyConnected(ServiceError):
    """The project the user tried to add is already one of their connected projects."""


class AccountService:
    def __init__(self, store: Store, cipher: TokenCipher):
        self.store = store
        self.cipher = cipher

    # -- users --------------------------------------------------------------

    async def get_or_create_user(
        self, user_id: str, email: str, *, admin_emails: tuple[str, ...] = ()
    ) -> User:
        is_admin = bool(email) and email.lower() in {e.lower() for e in admin_emails}
        user = await self.store.get_user(user_id)
        if user is None:
            user = User(user_id=user_id, email=email, is_admin=is_admin)
            await self.store.upsert_user(user)
            return user
        # Reconcile on every login: fill in a newly-available email, and promote
        # to admin if the email now matches (never auto-demote). Fixes records
        # created before the email/admin was known.
        changed = False
        if email and user.email != email:
            user.email = email
            changed = True
        if is_admin and not user.is_admin:
            user.is_admin = True
            changed = True
        if changed:
            await self.store.upsert_user(user)
        return user

    # -- onboarding: connect a project -------------------------------------

    async def connect_project(
        self,
        user_id: str,
        overleaf_url_or_id: str,
        token: str,
        name: str | None = None,
        git_url: str | None = None,
    ) -> Project:
        """Store a user's Overleaf project with its token ENCRYPTED. Enforces the
        per-plan project limit (updating an already-connected project is free)."""
        user = await self.store.get_user(user_id)
        if user is None:
            raise ServiceError("Unknown user; sign in first.")
        if not token or "PASTE" in token:
            raise ServiceError("A real Overleaf Git token is required.")
        try:
            pid = extract_project_id(overleaf_url_or_id)
        except ConfigError as exc:
            raise ServiceError(str(exc)) from exc

        # The Overleaf Git token is account-level — store it once on the user so
        # later projects can be added without pasting it again.
        user.overleaf_token_encrypted = self.cipher.encrypt(token)
        await self.store.upsert_user(user)

        await self._enforce_project_limit(user, pid)
        project = Project(
            user_id=user_id, project_id=pid, name=(name or pid[:8]),
            token_encrypted="", git_url=git_url,  # empty -> uses the account token
        )
        await self.store.put_project(project)
        return project

    async def add_project(
        self, user_id: str, overleaf_url_or_id: str, name: str | None = None,
        git_url: str | None = None,
    ) -> Project:
        """Add another project for an already-onboarded user, REUSING their stored
        account token (no token needed — only the URL)."""
        user = await self.store.get_user(user_id)
        if user is None:
            raise ServiceError("Unknown user; sign in first.")
        if not await self._account_token_enc(user):
            raise ServiceError(
                "Connect your first project (with your Overleaf token) before "
                "adding more."
            )
        try:
            pid = extract_project_id(overleaf_url_or_id)
        except ConfigError as exc:
            raise ServiceError(str(exc)) from exc
        dup = next(
            (p for p in await self.store.list_projects(user_id) if p.project_id == pid),
            None,
        )
        if dup is not None:
            raise AlreadyConnected(
                f"'{dup.name}' is already one of your connected projects, so you can "
                "edit it right now. No need to add it again."
            )
        await self._enforce_project_limit(user, pid)
        project = Project(
            user_id=user_id, project_id=pid, name=(name or pid[:8]),
            token_encrypted="", git_url=git_url,
        )
        await self.store.put_project(project)
        return project

    async def set_token(self, user_id: str, token: str) -> None:
        """Change the user's Overleaf token (applies to all their projects)."""
        if not token or "PASTE" in token:
            raise ServiceError("A real Overleaf Git token is required.")
        user = await self.store.get_user(user_id)
        if user is None:
            raise ServiceError("Unknown user; sign in first.")
        user.overleaf_token_encrypted = self.cipher.encrypt(token)
        await self.store.upsert_user(user)
        await self._clear_project_token_overrides(user_id)

    async def revoke_token(self, user_id: str) -> None:
        """Revoke the stored token — the AI can no longer reach any project until a
        new token is set. Projects stay in the list; re-add a token to restore."""
        user = await self.store.get_user(user_id)
        if user is not None and user.overleaf_token_encrypted:
            user.overleaf_token_encrypted = ""
            await self.store.upsert_user(user)
        await self._clear_project_token_overrides(user_id)

    async def disconnect_project(self, user_id: str, project_ref: str) -> bool:
        proj = self._select(await self.store.list_projects(user_id), project_ref)
        return await self.store.delete_project(user_id, proj.project_id)

    async def has_token(self, user_id: str) -> bool:
        user = await self.store.get_user(user_id)
        return bool(user and await self._account_token_enc(user))

    async def _enforce_project_limit(self, user: User, pid: str) -> None:
        existing = await self.store.list_projects(user.user_id)
        already = any(p.project_id == pid for p in existing)
        limit = project_limit(user)
        if limit is not None and not already and len(existing) >= limit:
            plural = "project" if limit == 1 else "projects"
            raise LimitExceeded(
                f"The free plan includes {limit} connected {plural} at a time. To "
                "work on a different project, remove the current one first (ask to "
                '"manage my projects"), or upgrade to Pro for unlimited projects at '
                f"once: {UPGRADE_URL}"
            )

    async def _account_token_enc(self, user: User) -> str:
        """The user's encrypted account token, backfilling once from a legacy
        per-project token (for projects connected before account tokens existed)."""
        if user.overleaf_token_encrypted:
            return user.overleaf_token_encrypted
        for p in await self.store.list_projects(user.user_id):
            if p.token_encrypted:
                user.overleaf_token_encrypted = p.token_encrypted
                await self.store.upsert_user(user)
                return p.token_encrypted
        return ""

    async def _clear_project_token_overrides(self, user_id: str) -> None:
        for p in await self.store.list_projects(user_id):
            if p.token_encrypted:
                p.token_encrypted = ""
                await self.store.put_project(p)

    # -- billing ------------------------------------------------------------

    async def set_stripe_customer(self, user_id: str, customer_id: str | None) -> None:
        """Remember a user's Stripe customer id (set when they first check out)."""
        user = await self.store.get_user(user_id)
        if user is None or not customer_id or user.stripe_customer_id == customer_id:
            return
        user.stripe_customer_id = customer_id
        await self.store.upsert_user(user)

    async def apply_subscription(
        self, user_id: str, plan: str, customer_id: str | None = None
    ) -> bool:
        """Flip a user's plan (free<->pro) from a Stripe webhook. Idempotent.
        Returns True if anything changed. Never downgrades an admin."""
        user = await self.store.get_user(user_id)
        if user is None:
            return False
        changed = False
        if customer_id and user.stripe_customer_id != customer_id:
            user.stripe_customer_id = customer_id
            changed = True
        if not user.is_admin and plan in ("free", "pro") and user.plan != plan:
            user.plan = plan
            changed = True
        if changed:
            await self.store.upsert_user(user)
        return changed

    # -- resolution: project -> git-ready config ---------------------------

    async def resolve_project(
        self, user_id: str, project_ref: str | None = None
    ) -> ProjectConfig:
        """Return a :class:`ProjectConfig` (token DECRYPTED, transiently) for the
        user's chosen project, for the git worker to use."""
        projects = await self.store.list_projects(user_id)
        if not projects:
            raise ProjectNotConnected(
                "No Overleaf project is connected to your account yet. "
                "Add one at https://milatexai.com."
            )
        chosen = self._select(projects, project_ref)
        enc = chosen.token_encrypted
        if not enc:
            user = await self.store.get_user(user_id)
            enc = await self._account_token_enc(user) if user else ""
        if not enc:
            raise ProjectNotConnected(
                "Your Overleaf token is missing or was revoked. Add it again to "
                "keep editing (ask me to change your token)."
            )
        token = self.cipher.decrypt(enc)
        return ProjectConfig(
            name=chosen.name,
            project_id=chosen.project_id,
            token=token,
            git_username=chosen.git_username,
            git_url=chosen.git_url,
        )

    # -- usage metering (writes only) --------------------------------------

    async def check_commit_allowed(self, user_id: str, month: str) -> None:
        """Raise LimitExceeded if the user has hit this month's commit cap.
        Call BEFORE performing a metered write."""
        user = await self.store.get_user(user_id)
        if user is None:
            raise ServiceError("Unknown user; sign in first.")
        limit = monthly_commit_limit(user)
        if limit is None:
            return
        used = await self.store.get_usage(user_id, month)
        if used >= limit:
            raise LimitExceeded(
                f"You've used all {limit} free commits this month. Reads stay free; "
                f"new edits resume next month, or upgrade to Pro for unlimited: {UPGRADE_URL}"
            )

    async def record_commit(self, user_id: str, month: str) -> int:
        """Count one successful metered write. Call AFTER a push succeeds."""
        return await self.store.increment_usage(user_id, month)

    # -- helpers ------------------------------------------------------------

    @staticmethod
    def _select(projects: list[Project], ref: str | None) -> Project:
        if ref is None:
            if len(projects) == 1:
                return projects[0]
            names = ", ".join(p.name for p in projects)
            raise ProjectNotConnected(
                f"You have multiple projects; specify one by name or id. Available: {names}."
            )
        r = ref.strip().lower()
        for p in projects:
            if p.name.lower() == r or p.project_id == r:
                return p
        try:
            pid = extract_project_id(ref)
        except ConfigError:
            pid = None
        if pid:
            for p in projects:
                if p.project_id == pid:
                    return p
        raise ProjectNotConnected(f"No connected project matches {ref!r}.")
