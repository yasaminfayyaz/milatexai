"""End-to-end: real login, at the protocol level.

Production login works like this: WorkOS (AuthKit) issues a signed token, and
FastMCP accepts it only if the signature matches WorkOS's published keys and
the issuer, audience, and expiry are right. No other test exercises that
path (the journey tests run with auth off), so a framework upgrade could break
real logins unnoticed. This test closes that gap without a WorkOS account:

- a fake AuthKit issuer on a local port publishes OUR test signing key;
- the real server is built with auth ON, pointed at that issuer;
- requests are raw MCP JSON-RPC over HTTP, exactly what Claude and ChatGPT
  send, so the test does not depend on any client library's API;
- a valid token must reach the right user's data, and forged, expired,
  wrong-audience, and wrong-issuer tokens must all be refused with 401.

Security-critical: ops/maint/guard.py forbids the maintenance agent from
editing this file, so it cannot be weakened to let an upgrade through.
"""

from __future__ import annotations

import asyncio
import base64
import json
import socket
import subprocess
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace

import pytest
import uvicorn
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from leafbridge.capacity import CapacityGate
from leafbridge.hosted import create_hosted_server
from leafbridge.service import AccountService
from leafbridge.store import InMemoryStore, TokenCipher, User

USER_ID = "user_e2e_auth"
EMAIL = "auth-e2e@example.com"
KID = "e2e-key-1"
HEX = "cccccccccccccccccccccccc"


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _int_b64url(n: int) -> str:
    return _b64url(n.to_bytes((n.bit_length() + 7) // 8, "big"))


def _mint(key, claims: dict, kid: str = KID) -> str:
    """A standard RS256 JWT, built by hand so this test depends on no JWT library."""
    header = _b64url(json.dumps({"alg": "RS256", "typ": "JWT", "kid": kid}).encode())
    payload = _b64url(json.dumps(claims).encode())
    signing_input = f"{header}.{payload}".encode()
    signature = key.sign(signing_input, padding.PKCS1v15(), hashes.SHA256())
    return f"{header}.{payload}.{_b64url(signature)}"


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _git(args, cwd) -> None:
    p = subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True)
    assert p.returncode == 0, p.stderr or p.stdout


def _fake_authkit(public_key, port: int) -> ThreadingHTTPServer:
    issuer = f"http://127.0.0.1:{port}"
    nums = public_key.public_numbers()
    jwks = {"keys": [{"kty": "RSA", "use": "sig", "alg": "RS256", "kid": KID,
                      "n": _int_b64url(nums.n), "e": _int_b64url(nums.e)}]}
    metadata = {
        "issuer": issuer,
        "authorization_endpoint": f"{issuer}/oauth2/authorize",
        "token_endpoint": f"{issuer}/oauth2/token",
        "registration_endpoint": f"{issuer}/oauth2/register",
        "jwks_uri": f"{issuer}/oauth2/jwks",
        "response_types_supported": ["code"],
        "code_challenge_methods_supported": ["S256"],
    }

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            body = {"/oauth2/jwks": jwks,
                    "/.well-known/oauth-authorization-server": metadata}.get(self.path)
            if body is None:
                self.send_response(404)
                self.end_headers()
                return
            data = json.dumps(body).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, *args):  # keep test output clean
            pass

    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def _post(world, method: str, params: dict, token: str | None, rid: int = 1,
          protocol: str | None = None):
    """One MCP JSON-RPC request over HTTP. Returns (status, headers, json|None)."""
    headers = {"Content-Type": "application/json",
               "Accept": "application/json, text/event-stream"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    if protocol:
        headers["MCP-Protocol-Version"] = protocol
    body = json.dumps({"jsonrpc": "2.0", "id": rid, "method": method, "params": params})
    req = urllib.request.Request(world.mcp_url, data=body.encode(), headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            raw = r.read().decode()
            if "text/event-stream" in (r.headers.get("Content-Type") or ""):
                data_lines = [l[5:].strip() for l in raw.splitlines() if l.startswith("data:")]
                raw = data_lines[-1] if data_lines else "{}"
            return r.status, dict(r.headers), json.loads(raw or "{}")
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers), None


@pytest.fixture(scope="module")
def world(tmp_path_factory):
    root = tmp_path_factory.mktemp("auth")
    signing_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    issuer_port, server_port = _free_port(), _free_port()
    issuer = f"http://127.0.0.1:{issuer_port}"
    fake = _fake_authkit(signing_key.public_key(), issuer_port)

    # A user with a real connected project, so a valid token must lead to THEIR data.
    remote, seed = root / "r.git", root / "seed"
    remote.mkdir()
    _git(["init", "--bare", "-b", "main", "."], remote)
    seed.mkdir()
    _git(["init", "-b", "main", "."], seed)
    (seed / "main.tex").write_bytes(b"\\documentclass{article}\\begin{document}x\\end{document}\n")
    _git(["add", "-A"], seed)
    _git(["-c", "user.name=S", "-c", "user.email=s@example.com", "commit", "-m", "init"], seed)
    _git(["remote", "add", "origin", remote.as_uri()], seed)
    _git(["push", "-u", "origin", "main"], seed)
    store = InMemoryStore()
    cipher = TokenCipher(TokenCipher.generate_key())
    asyncio.run(store.upsert_user(User(user_id=USER_ID, email=EMAIL, plan="pro")))
    asyncio.run(AccountService(store, cipher).connect_project(
        USER_ID, f"https://www.overleaf.com/project/{HEX}", "olp_x", "auth-paper",
        git_url=remote.as_uri()))

    base = f"http://127.0.0.1:{server_port}"
    with pytest.MonkeyPatch.context() as mp:
        mp.setenv("WORKOS_AUTHKIT_DOMAIN", issuer)
        # Never reach the real WorkOS from a test, even if a local .env is loaded.
        mp.delenv("WORKOS_API_KEY", raising=False)
        mp.delenv("WORKOS_CLIENT_ID", raising=False)
        mcp = create_hosted_server(
            store=store, cipher=cipher, auth=True, base_url=base,
            data_dir=root / "cache",
            capacity=CapacityGate(subscription_id="", resource_group="", stripe_api_key=""),
        )
        app = mcp.http_app(stateless_http=True)
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=server_port,
                                           log_level="warning", lifespan="on"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.time() + 30
    while not server.started:
        assert time.time() < deadline, "server did not start"
        time.sleep(0.05)

    # The audience a real AuthKit token carries is the resource URL this server
    # advertises (RFC 8707), so read it from the server instead of guessing.
    with urllib.request.urlopen(f"{base}/.well-known/oauth-protected-resource/mcp", timeout=30) as r:
        resource = json.load(r)["resource"]

    now = int(time.time())
    good_claims = {"iss": issuer, "sub": USER_ID, "email": EMAIL, "aud": resource,
                   "iat": now, "exp": now + 600}
    yield SimpleNamespace(mcp_url=f"{base}/mcp", key=signing_key, issuer=issuer,
                          resource=resource, good_claims=good_claims)
    server.should_exit = True
    thread.join(timeout=15)
    fake.shutdown()


INIT = {"protocolVersion": "2025-06-18", "capabilities": {},
        "clientInfo": {"name": "e2e-auth", "version": "1"}}


def test_valid_token_reaches_the_users_own_data(world):
    token = _mint(world.key, world.good_claims)
    status, _, init = _post(world, "initialize", INIT, token)
    assert status == 200, status
    assert init["result"]["serverInfo"]["name"] == "MiLatexAI"
    protocol = init["result"]["protocolVersion"]

    status, _, listed = _post(world, "tools/list", {}, token, rid=2, protocol=protocol)
    assert status == 200
    names = {t["name"] for t in listed["result"]["tools"]}
    assert {"list_projects", "read_file", "download_file"} <= names

    status, _, called = _post(world, "tools/call",
                              {"name": "list_projects", "arguments": {}},
                              token, rid=3, protocol=protocol)
    assert status == 200
    text = "".join(c.get("text", "") for c in called["result"]["content"])
    assert "auth-paper" in text, text  # identity came from the token's sub claim


def test_no_token_is_refused_with_an_oauth_challenge(world):
    status, headers, _ = _post(world, "initialize", INIT, None)
    assert status == 401
    challenge = {k.lower(): v for k, v in headers.items()}.get("www-authenticate", "")
    assert "resource_metadata" in challenge


@pytest.mark.parametrize("case", ["forged_signature", "expired", "wrong_audience", "wrong_issuer"])
def test_bad_tokens_are_refused(world, case):
    claims = dict(world.good_claims)
    key = world.key
    if case == "forged_signature":
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)  # attacker's key, same kid
    elif case == "expired":
        claims.update(iat=claims["iat"] - 7200, exp=claims["iat"] - 3600)
    elif case == "wrong_audience":
        claims["aud"] = "https://some-other-server.example/mcp"
    elif case == "wrong_issuer":
        claims["iss"] = "https://evil-issuer.example"
    status, _, _ = _post(world, "initialize", INIT, _mint(key, claims))
    assert status == 401, f"{case} token was accepted"
