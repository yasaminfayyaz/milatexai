"""Azure Table Storage backend for the multi-tenant :class:`~leafbridge.store.Store`.

Three tables — users, projects, usage — holding only account metadata, the
ENCRYPTED Overleaf token, and a commit counter. Never any document content.

Auth is via the storage connection string (from Container Apps secret / Key
Vault in production). Cheap: Table Storage is pennies at this scale.
"""

from __future__ import annotations

import os

from azure.core.exceptions import ResourceNotFoundError
from azure.data.tables import UpdateMode
from azure.data.tables.aio import TableServiceClient

from .store import Project, Store, User


class AzureTableStore(Store):
    def __init__(self, connection_string: str, *, prefix: str = ""):
        self._svc = TableServiceClient.from_connection_string(connection_string)
        self._prefix = prefix
        self._ready = False

    @classmethod
    def from_env(cls, *, prefix: str = "") -> "AzureTableStore":
        cs = os.environ["AZURE_STORAGE_CONNECTION_STRING"]
        return cls(cs, prefix=prefix)

    def _name(self, base: str) -> str:
        return f"{self._prefix}{base}"

    async def _ensure(self) -> None:
        if not self._ready:
            for base in ("users", "projects", "usage"):
                await self._svc.create_table_if_not_exists(self._name(base))
            self._ready = True

    def _table(self, base: str):
        return self._svc.get_table_client(self._name(base))

    async def close(self) -> None:
        await self._svc.close()

    # -- users --------------------------------------------------------------

    async def get_user(self, user_id: str) -> User | None:
        await self._ensure()
        try:
            e = await self._table("users").get_entity("user", user_id)
        except ResourceNotFoundError:
            return None
        return User(
            user_id=e["RowKey"],
            email=e.get("email", ""),
            plan=e.get("plan", "free"),
            is_admin=bool(e.get("is_admin", False)),
            stripe_customer_id=(e.get("stripe_customer_id") or None),
            overleaf_token_encrypted=(e.get("overleaf_token_encrypted") or ""),
        )

    async def upsert_user(self, user: User) -> None:
        await self._ensure()
        await self._table("users").upsert_entity(
            {
                "PartitionKey": "user",
                "RowKey": user.user_id,
                "email": user.email,
                "plan": user.plan,
                "is_admin": user.is_admin,
                "stripe_customer_id": user.stripe_customer_id or "",
                "overleaf_token_encrypted": user.overleaf_token_encrypted or "",
            },
            mode=UpdateMode.REPLACE,
        )

    # -- projects -----------------------------------------------------------

    @staticmethod
    def _to_project(user_id: str, e) -> Project:
        return Project(
            user_id=user_id,
            project_id=e["RowKey"],
            name=e.get("name", ""),
            token_encrypted=e.get("token_encrypted", ""),
            git_username=e.get("git_username", "git"),
            git_url=(e.get("git_url") or None),
            # Older rows predate multi-provider — default them to Overleaf.
            provider=(e.get("provider") or "overleaf"),
        )

    async def list_projects(self, user_id: str) -> list[Project]:
        await self._ensure()
        out: list[Project] = []
        async for e in self._table("projects").query_entities(
            "PartitionKey eq @pk", parameters={"pk": user_id}
        ):
            out.append(self._to_project(user_id, e))
        return out

    async def get_project(self, user_id: str, project_id: str) -> Project | None:
        await self._ensure()
        try:
            e = await self._table("projects").get_entity(user_id, project_id)
        except ResourceNotFoundError:
            return None
        return self._to_project(user_id, e)

    async def put_project(self, project: Project) -> None:
        await self._ensure()
        await self._table("projects").upsert_entity(
            {
                "PartitionKey": project.user_id,
                "RowKey": project.project_id,
                "name": project.name,
                "token_encrypted": project.token_encrypted,
                "git_username": project.git_username,
                "git_url": project.git_url or "",
                "provider": project.provider or "overleaf",
            },
            mode=UpdateMode.REPLACE,
        )

    async def delete_project(self, user_id: str, project_id: str) -> bool:
        await self._ensure()
        table = self._table("projects")
        try:
            await table.get_entity(user_id, project_id)
        except ResourceNotFoundError:
            return False
        await table.delete_entity(user_id, project_id)
        return True

    # -- usage --------------------------------------------------------------

    async def get_usage(self, user_id: str, month: str) -> int:
        await self._ensure()
        try:
            e = await self._table("usage").get_entity(user_id, month)
        except ResourceNotFoundError:
            return 0
        return int(e.get("count", 0))

    async def increment_usage(self, user_id: str, month: str, by: int = 1) -> int:
        await self._ensure()
        table = self._table("usage")
        # Read-modify-write. Per-user writes are already serialized by the git
        # worker's per-project lock, so contention here is negligible.
        try:
            e = await table.get_entity(user_id, month)
            new = int(e.get("count", 0)) + by
        except ResourceNotFoundError:
            new = by
        await table.upsert_entity(
            {"PartitionKey": user_id, "RowKey": month, "count": new},
            mode=UpdateMode.REPLACE,
        )
        return new
