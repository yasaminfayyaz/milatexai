"""Phase 2 multi-tenant storage: users, connected projects (with ENCRYPTED
Overleaf tokens), and monthly usage counters.

This replaces Phase 1's plaintext ``projects.json``. The interface is
backend-agnostic: :class:`InMemoryStore` for dev/tests, and (later) an Azure
Table Storage backend for production. Tokens are encrypted at rest via
:class:`TokenCipher` — in production the key comes from Azure Key Vault; for
local dev it is read from ``LEAFBRIDGE_ENC_KEY`` or generated ephemerally.

Nothing here stores document contents — only account metadata, an encrypted
token, and a commit counter, exactly as the design plan promises.
"""

from __future__ import annotations

import os
from abc import ABC, abstractmethod
from dataclasses import dataclass, field

from cryptography.fernet import Fernet, InvalidToken

# Free-tier limits (admins and "pro" are unlimited).
FREE_PROJECT_LIMIT = 1
FREE_MONTHLY_COMMITS = 25


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
        that are rejected (Fernet embeds a timestamp) — used for expiring
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
    # projects), so it lives on the user. Projects added later reuse it — no need
    # to paste the token again. Encrypted with TokenCipher.
    overleaf_token_encrypted: str = ""


@dataclass
class Project:
    user_id: str
    project_id: str  # Overleaf project id (24-hex)
    name: str
    token_encrypted: str = ""  # optional per-project override; "" -> use account token
    git_username: str = "git"
    git_url: str | None = None  # override for self-hosted Overleaf / testing


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


class InMemoryStore(Store):
    """Non-persistent store for development and tests."""

    def __init__(self) -> None:
        self._users: dict[str, User] = {}
        self._projects: dict[tuple[str, str], Project] = {}
        self._usage: dict[tuple[str, str], int] = {}

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
