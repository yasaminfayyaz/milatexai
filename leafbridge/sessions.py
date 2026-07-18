"""Client for Azure Container Apps Dynamic Sessions (code-interpreter pool).

Figure Studio executes LLM-authored matplotlib code in a Hyper-V isolated
session (egress disabled, no access to this container's secrets or disk), then
downloads the produced PDF. Auth is the container app's managed identity via
``DefaultAzureCredential`` (role: Azure ContainerApps Session Executor); for
local development a raw bearer token can be injected via ``SESSIONS_TOKEN``.

Session identifiers are the *authorization* for a session's files, so they are
derived per user with an HMAC over a server secret: unguessable, stable per
user (so a user's session is reused within the pool cooldown window), and never
shared across tenants.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import time
from dataclasses import dataclass

API_VERSION = "2025-02-02-preview"
TOKEN_SCOPE = "https://dynamicsessions.io/.default"


class SessionsError(Exception):
    """The sessions pool rejected or failed an operation."""


@dataclass
class ExecResult:
    ok: bool
    stdout: str = ""
    stderr: str = ""
    image_b64: str | None = None  # the pool auto-captures the current figure
    detail: str = ""


class SessionsClient:
    def __init__(self, pool_endpoint: str, *, secret: str = ""):
        self.endpoint = (pool_endpoint or "").rstrip("/")
        self.enabled = bool(self.endpoint)
        self._secret = secret or "leafbridge"
        self._cred = None
        self._token: str | None = None
        self._token_exp: float = 0.0

    @classmethod
    def from_env(cls) -> "SessionsClient":
        return cls(
            os.environ.get("SESSIONS_POOL_ENDPOINT", ""),
            secret=os.environ.get("LEAFBRIDGE_ENC_KEY", ""),
        )

    def session_for(self, user_id: str) -> str:
        """A stable, unguessable per-user session identifier."""
        mac = hmac.new(self._secret.encode(), b"figstudio:" + user_id.encode(), hashlib.sha256)
        return "fig-" + mac.hexdigest()[:32]

    # -- auth ----------------------------------------------------------------

    async def _get_token(self) -> str:
        override = os.environ.get("SESSIONS_TOKEN")
        if override:
            return override
        now = time.time()
        if self._token and now < self._token_exp - 120:
            return self._token
        from azure.identity.aio import DefaultAzureCredential

        if self._cred is None:
            self._cred = DefaultAzureCredential()
        tok = await self._cred.get_token(TOKEN_SCOPE)
        self._token, self._token_exp = tok.token, float(tok.expires_on)
        return self._token

    # -- transport (overridden in tests) ------------------------------------

    async def _request(
        self, method: str, path_and_query: str, json_body: dict | None = None,
        *, timeout: int = 150,
    ) -> tuple[int, bytes]:
        import aiohttp

        token = await self._get_token()
        headers = {"Authorization": f"Bearer {token}"}
        url = f"{self.endpoint}/{path_and_query}"
        client_timeout = aiohttp.ClientTimeout(total=timeout)
        async with aiohttp.ClientSession(timeout=client_timeout) as sess:
            async with sess.request(method, url, json=json_body, headers=headers) as resp:
                return resp.status, await resp.read()

    # -- operations ----------------------------------------------------------

    async def execute(self, session_id: str, code: str) -> ExecResult:
        """Run ``code`` synchronously in the user's session."""
        if not self.enabled:
            raise SessionsError("Figure Studio is not configured on this server.")
        status, raw = await self._request(
            "POST",
            f"executions?api-version={API_VERSION}&identifier={session_id}",
            {"codeInputType": "Inline", "executionType": "Synchronous", "code": code},
        )
        if status >= 300:
            raise SessionsError(f"Sandbox execution failed (HTTP {status}).")
        try:
            body = json.loads(raw)
        except ValueError as exc:
            raise SessionsError("Sandbox returned an unreadable response.") from exc
        result = body.get("result") or {}
        exec_res = result.get("executionResult") or {}
        image = exec_res.get("base64_data") if isinstance(exec_res, dict) else None
        ok = body.get("status") == "Succeeded"
        return ExecResult(
            ok=ok,
            stdout=str(result.get("stdout") or ""),
            stderr=str(result.get("stderr") or ""),
            image_b64=image if isinstance(image, str) else None,
            detail="" if ok else f"status={body.get('status')}",
        )

    async def download(self, session_id: str, filename: str) -> bytes:
        """Fetch a file produced by the code (e.g. figure.pdf)."""
        status, raw = await self._request(
            "GET", f"files/{filename}/content?api-version={API_VERSION}&identifier={session_id}"
        )
        if status >= 300:
            raise SessionsError(f"Could not fetch {filename!r} from the sandbox (HTTP {status}).")
        return raw

    async def list_files(self, session_id: str) -> list[str]:
        status, raw = await self._request(
            "GET", f"files?api-version={API_VERSION}&identifier={session_id}"
        )
        if status >= 300:
            return []
        try:
            body = json.loads(raw)
        except ValueError:
            return []
        return [f.get("name", "") for f in body.get("value", []) if isinstance(f, dict)]
