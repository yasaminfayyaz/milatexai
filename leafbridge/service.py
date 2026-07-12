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


class AccountService:
    def __init__(self, store: Store, cipher: TokenCipher):
        self.store = store
        self.cipher = cipher

    # -- users --------------------------------------------------------------

    async def get_or_create_user(
        self, user_id: str, email: str, *, admin_emails: tuple[str, ...] = ()
    ) -> User:
        user = await self.store.get_user(user_id)
        if user is None:
            user = User(
                user_id=user_id,
                email=email,
                is_admin=email.lower() in {e.lower() for e in admin_emails},
            )
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

        existing = await self.store.list_projects(user_id)
        already = any(p.project_id == pid for p in existing)
        limit = project_limit(user)
        if limit is not None and not already and len(existing) >= limit:
            raise LimitExceeded(
                f"Your plan allows {limit} connected project(s). "
                f"Upgrade to Pro for unlimited: {UPGRADE_URL}"
            )

        project = Project(
            user_id=user_id,
            project_id=pid,
            name=(name or pid[:8]),
            token_encrypted=self.cipher.encrypt(token),
            git_url=git_url,
        )
        await self.store.put_project(project)
        return project

    async def disconnect_project(self, user_id: str, project_ref: str) -> bool:
        proj = self._select(await self.store.list_projects(user_id), project_ref)
        return await self.store.delete_project(user_id, proj.project_id)

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
        token = self.cipher.decrypt(chosen.token_encrypted)
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
