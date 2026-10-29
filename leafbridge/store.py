"""Phase 2 multi-tenant storage: users, connected projects (with ENCRYPTED
Overleaf tokens), and monthly usage counters.

This replaces Phase 1's plaintext ``projects.json``. The interface is
backend-agnostic: :class:`InMemoryStore` for dev/tests, and (later) an Azure
Table Storage backend for production. Tokens are encrypted at rest via
:class:`TokenCipher`, in production the key comes from Azure Key Vault; for
local dev it is read from ``LEAFBRIDGE_ENC_KEY`` or generated ephemerally.

Nothing here stores document contents, only account metadata, an encrypted
token, and a commit counter, exactly as the design plan promises.
"""

from __future__ import annotations

import os
from abc import ABC, abstractmethod
from dataclasses import dataclass, field, replace

from cryptography.fernet import Fernet, InvalidToken

# Free-tier limits (admins and "pro" are unlimited).
FREE_PROJECT_LIMIT = 1
FREE_MONTHLY_COMMITS = 10


class StoreError(Exception):
    pass


class TokenDecryptError(StoreError):
    """A stored token could not be decrypted (wrong/rotated key)."""


class TokenCipher:
    """Symmetric authenticated encryption for stored tokens (Fernet)."""

    def __init__(self, key: bytes | str):
        self._fernet = Fernet(key.encode() if isinstance(key, str) else key)

    @classmethod
    def from_env(cls, var: str = "LEAFBRIDGE_ENC_KEY") -> tuple["TokenCipher", bool]:
        """Build a cipher from ``$var``; returns ``(cipher, ephemeral)``.

        ``ephemeral`` is True when no key was configured and a throwaway key was
        generated (fine for local dev; stored tokens won't survive a restart).
        """
        key = os.environ.get(var)
        if key:
            return cls(key), False
        return cls(Fernet.generate_key()), True

    @staticmethod
    def generate_key() -> str:
        """A fresh urlsafe-base64 key (put this in Key Vault / LEAFBRIDGE_ENC_KEY)."""
        return Fernet.generate_key().decode()

    def encrypt(self, plaintext: str) -> str:
        return self._fernet.encrypt(plaintext.encode()).decode()

    def decrypt(self, token: str, ttl: int | None = None) -> str:
        """Decrypt ciphertext. If ``ttl`` (seconds) is given, tokens older than
        that are rejected (Fernet embeds a timestamp), used for expiring
        capability codes, not for tokens at rest."""
        try:
            return self._fernet.decrypt(token.encode(), ttl=ttl).decode()
        except InvalidToken as exc:
            raise TokenDecryptError("Could not decrypt the stored token.") from exc


@dataclass
class User:
    user_id: str  # stable subject from the OAuth provider (e.g. WorkOS `sub`)
    email: str
    plan: str = "free"  # "free" | "pro"
    is_admin: bool = False
    stripe_customer_id: str | None = None
    # The Overleaf Git token is account-level (one token reaches all of a user's
    # projects), so it lives on the user. Projects added later reuse it, no need
    # to paste the token again. Encrypted with TokenCipher.
    overleaf_token_encrypted: str = ""
    # On the free plan only one project is active at a time; the others stay saved but
    # paused (for example after a Pro plan ends). "" until the user picks one.
    active_project_id: str = ""
    # A Pro payment that did not go through: when it first failed (epoch seconds, 0 = all paid up)
    # and how many reminder emails have gone out (0 none, 1 first notice, 2 final reminder).
    # Pro stays on for a grace period after the first failure; see dunning.py.
    past_due_since: float = 0.0
    dunning_stage: int = 0
    # Invite a friend (see referrals.py). ``referral_code`` is this person's own link code.
    # On the invited friend: who invited them, when their first payment went through, and whether the
    # inviter's reward has been settled. On the inviter: rewards earned so far, and how many of them
    # are still waiting to be applied to their Stripe balance (they are not Pro yet, or have no customer).
    referral_code: str = ""
    referred_by: str = ""
    referral_paid_at: float = 0.0
    referral_done: bool = False
    referral_rewards: int = 0
    referral_pending: int = 0


@dataclass
class Project:
    user_id: str
    project_id: str  # Overleaf project id (24-hex), or a provider project_key
    name: str
    token_encrypted: str = ""  # optional per-project override; "" -> use account token
    git_username: str = "git"
    git_url: str | None = None  # override for self-hosted Overleaf / testing
    # Which service this repo lives on. Defaults to "overleaf" so every existing
    # record (and everything that omits it) stays an Overleaf project. For
    # non-Overleaf repos the per-repo token lives in ``token_encrypted`` (there
    # is no shared account token), so ``provider`` also decides which projects
    # the account-token backfill/clear logic may touch.
    provider: str = "overleaf"


def monthly_commit_limit(user: User) -> int | None:
    """Commits allowed this month, or None for unlimited."""
    if user.is_admin or user.plan == "pro":
        return None
    return FREE_MONTHLY_COMMITS


def project_limit(user: User) -> int | None:
    """Projects a user may connect, or None for unlimited."""
    if user.is_admin or user.plan == "pro":
        return None
    return FREE_PROJECT_LIMIT


class Store(ABC):
    """Backend-agnostic persistence. All methods are async so a real backend
    (Azure Table Storage) can do true async I/O."""

    @abstractmethod
    async def get_user(self, user_id: str) -> User | None: ...

    @abstractmethod
    async def upsert_user(self, user: User) -> None: ...

    @abstractmethod
    async def list_projects(self, user_id: str) -> list[Project]: ...

    @abstractmethod
    async def get_project(self, user_id: str, project_id: str) -> Project | None: ...

    @abstractmethod
    async def put_project(self, project: Project) -> None: ...

    @abstractmethod
    async def delete_project(self, user_id: str, project_id: str) -> bool: ...

    @abstractmethod
    async def get_usage(self, user_id: str, month: str) -> int: ...

    @abstractmethod
    async def increment_usage(self, user_id: str, month: str, by: int = 1) -> int: ...

    # -- accounts: listing, lookup by Stripe customer, and once-only job claims (see dunning.py).
    # Optional: a backend without them reports nothing and claims nothing.

    async def list_users(self) -> list[User]:
        return []

    async def find_user_by_customer(self, customer_id: str) -> User | None:
        for u in await self.list_users():
            if u.stripe_customer_id == customer_id:
                return u
        return None

    async def claim_job(self, name: str, key: str) -> bool:
        """True for exactly one caller per (name, key), however many copies of the server ask."""
        return False

    # -- tool-use counts (see activity.py). Optional: a backend without them just drops the counts.

    async def put_activity(self, user_id: str, copy_id: str, rows: list[tuple[str, str, int, int]]) -> None:
        """Save one copy's running totals as (day, tool, calls, errors) rows. Safe to repeat."""

    async def prune_activity(self, before_day: str) -> None:
        """Delete rows for days before ``before_day`` (YYYY-MM-DD)."""

    # -- state shared by every running copy of the server ---------------------
    # With more than one copy, anything one copy keeps in its own memory or disk is
    # invisible to the others. These two live in shared storage instead.

    async def get_head(self, project_id: str) -> str | None:
        """The commit the remote was at after the last push by ANY copy, so a copy
        whose working copy is older knows to refresh before a read."""
        return None

    async def put_head(self, project_id: str, sha: str) -> None:
        return None

    async def put_download(self, name: str, data: bytes) -> None:
        """A short-lived download (an arXiv bundle) that any copy can serve."""
        raise NotImplementedError

    # -- sign-in identities -------------------------------------------------------
    # MiLatexAI's account id is the WorkOS user id from the environment the account
    # was created in. A sign-in through another WorkOS environment (staging moved to
    # production) has a different id; a link maps it to the existing account, found
    # once by verified email, so projects, plan and Stripe subscription carry over.

    async def get_link(self, external_id: str) -> str | None:
        return None

    async def put_link(self, external_id: str, user_id: str) -> None:
        raise NotImplementedError

    async def find_user_by_email(self, email: str) -> list[User]:
        """Every account with this email (case-insensitive)."""
        raise NotImplementedError

    async def get_download(self, name: str) -> bytes | None:
        raise NotImplementedError


class InMemoryStore(Store):
    """Non-persistent store for development and tests."""

    def __init__(self) -> None:
        self._users: dict[str, User] = {}
        self._projects: dict[tuple[str, str], Project] = {}
        self._usage: dict[tuple[str, str], int] = {}
        self._jobs: set[tuple[str, str]] = set()
        self._activity: dict[tuple[str, str], tuple[int, int]] = {}   # (user_id, "day|tool|copy") -> (calls, errors)
        self._heads: dict[str, str] = {}
        self._downloads: dict[str, bytes] = {}
        self._links: dict[str, str] = {}

    async def get_user(self, user_id: str) -> User | None:
        return self._users.get(user_id)

    async def upsert_user(self, user: User) -> None:
        self._users[user.user_id] = user

    async def list_projects(self, user_id: str) -> list[Project]:
        return [p for (uid, _), p in self._projects.items() if uid == user_id]

    async def get_project(self, user_id: str, project_id: str) -> Project | None:
        return self._projects.get((user_id, project_id))

    async def put_project(self, project: Project) -> None:
        self._projects[(project.user_id, project.project_id)] = project

    async def delete_project(self, user_id: str, project_id: str) -> bool:
        return self._projects.pop((user_id, project_id), None) is not None

    async def get_usage(self, user_id: str, month: str) -> int:
        return self._usage.get((user_id, month), 0)

    async def increment_usage(self, user_id: str, month: str, by: int = 1) -> int:
        new = self._usage.get((user_id, month), 0) + by
        self._usage[(user_id, month)] = new
        return new

    async def list_users(self) -> list[User]:
        return [replace(u) for u in self._users.values()]

    async def claim_job(self, name: str, key: str) -> bool:
        if (name, key) in self._jobs:
            return False
        self._jobs.add((name, key))
        return True

    async def put_activity(self, user_id: str, copy_id: str, rows: list[tuple[str, str, int, int]]) -> None:
        for day, tool, calls, errors in rows:
            self._activity[(user_id, f"{day}|{tool}|{copy_id}")] = (calls, errors)

    async def prune_activity(self, before_day: str) -> None:
        for key in [k for k in self._activity if k[1] < before_day]:
            del self._activity[key]

    async def get_head(self, project_id: str) -> str | None:
        return self._heads.get(project_id)

    async def put_head(self, project_id: str, sha: str) -> None:
        self._heads[project_id] = sha

    async def put_download(self, name: str, data: bytes) -> None:
        self._downloads[name] = data

    async def get_download(self, name: str) -> bytes | None:
        return self._downloads.get(name)

    async def get_link(self, external_id: str) -> str | None:
        return self._links.get(external_id)

    async def put_link(self, external_id: str, user_id: str) -> None:
        self._links[external_id] = user_id

    async def find_user_by_email(self, email: str) -> list[User]:
        e = email.strip().lower()
        return [u for u in self._users.values() if e and (u.email or "").strip().lower() == e]
