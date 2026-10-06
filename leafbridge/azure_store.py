"""Azure Table Storage backend for the multi-tenant :class:`~leafbridge.store.Store`.

Four tables (users, projects, usage, heads) holding only account metadata, the
ENCRYPTED Overleaf token, counters, and the last pushed commit id per project.
Never any document content. One blob container holds short-lived download
bundles (arXiv zips), so any running copy of the server can serve a link that
another copy created.

Auth is via the storage connection string (from Container Apps secret / Key
Vault in production). Cheap: Table Storage is pennies at this scale.
"""

from __future__ import annotations

import asyncio
import os
import random

from azure.core import MatchConditions
from azure.core.exceptions import ResourceExistsError, ResourceModifiedError, ResourceNotFoundError
from azure.data.tables import UpdateMode
from azure.data.tables.aio import TableServiceClient

from .store import Project, Store, User


class AzureTableStore(Store):
    def __init__(self, connection_string: str, *, prefix: str = ""):
        self._svc = TableServiceClient.from_connection_string(connection_string)
        self._cs = connection_string
        self._prefix = prefix
        self._ready = False
        self._blob = None
        self._container = None

    @classmethod
    def from_env(cls, *, prefix: str | None = None) -> "AzureTableStore":
        """``LEAFBRIDGE_TABLE_PREFIX`` gives a copy its own tables and download container in the
        same storage account (used by the throwaway load-test copy). Production sets none."""
        cs = os.environ["AZURE_STORAGE_CONNECTION_STRING"]
        if prefix is None:
            prefix = os.environ.get("LEAFBRIDGE_TABLE_PREFIX", "")
        if prefix and not (prefix.isalnum() and prefix[0].isalpha() and prefix.islower() and len(prefix) <= 20):
            raise ValueError("LEAFBRIDGE_TABLE_PREFIX must be 1 to 20 lowercase letters and digits, starting with a letter")
        return cls(cs, prefix=prefix)

    def _name(self, base: str) -> str:
        return f"{self._prefix}{base}"

    async def _ensure(self) -> None:
        if not self._ready:
            for base in ("users", "projects", "usage", "heads"):
                await self._svc.create_table_if_not_exists(self._name(base))
            self._ready = True

    def _table(self, base: str):
        return self._svc.get_table_client(self._name(base))

    async def close(self) -> None:
        await self._svc.close()
        if self._blob is not None:
            await self._blob.close()

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
            # Older rows predate multi-provider, default them to Overleaf.
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
        # Optimistic concurrency: two copies of the server may count for the same
        # user at the same moment. Each write only succeeds if the row has not
        # changed since it was read; the loser waits a random moment, reads again
        # and retries, so a crowd of simultaneous writers spreads out instead of colliding.
        for attempt in range(40):
            if attempt:
                await asyncio.sleep(random.uniform(0, min(0.5, 0.02 * attempt)))
            try:
                e = await table.get_entity(user_id, month)
            except ResourceNotFoundError:
                try:
                    await table.create_entity({"PartitionKey": user_id, "RowKey": month, "count": by})
                    return by
                except ResourceExistsError:
                    continue          # another copy created it first; count on top of theirs
            new = int(e.get("count", 0)) + by
            try:
                await table.update_entity(
                    {"PartitionKey": user_id, "RowKey": month, "count": new},
                    mode=UpdateMode.REPLACE, etag=e.metadata["etag"],
                    match_condition=MatchConditions.IfNotModified,
                )
                return new
            except ResourceModifiedError:
                continue              # someone else counted in between; read again
        raise RuntimeError("Could not update the usage counter after repeated conflicts.")

    # -- shared state for several running copies --------------------------------

    @staticmethod
    def _key(project_id: str) -> str:
        # Table keys may not contain / \ # ? or control characters.
        bad = {"/", chr(92), "#", "?"}
        return "".join("_" if (c in bad or ord(c) < 32 or ord(c) == 127) else c for c in project_id)[:500]

    async def get_head(self, project_id: str) -> str | None:
        await self._ensure()
        try:
            e = await self._table("heads").get_entity("head", self._key(project_id))
        except ResourceNotFoundError:
            return None
        return e.get("sha") or None

    async def put_head(self, project_id: str, sha: str) -> None:
        await self._ensure()
        await self._table("heads").upsert_entity(
            {"PartitionKey": "head", "RowKey": self._key(project_id), "sha": sha},
            mode=UpdateMode.REPLACE,
        )

    async def _downloads(self):
        if self._container is None:
            from azure.storage.blob.aio import BlobServiceClient

            self._blob = BlobServiceClient.from_connection_string(self._cs)
            container = self._blob.get_container_client(f"{self._prefix}downloads".lower())
            try:
                await container.create_container()
            except ResourceExistsError:
                pass
            self._container = container
        return self._container

    async def put_download(self, name: str, data: bytes) -> None:
        container = await self._downloads()
        await container.upload_blob(name, data, overwrite=True)

    async def get_download(self, name: str) -> bytes | None:
        container = await self._downloads()
        try:
            stream = await container.download_blob(name)
        except ResourceNotFoundError:
            return None
        return await stream.readall()
