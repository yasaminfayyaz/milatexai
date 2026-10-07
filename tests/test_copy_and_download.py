"""Binary files without flooding the chat.

download_file returns small files as base64 (unchanged) and larger ones as a short-lived
download link; copy_file copies a file between two of the user's projects on the server,
so nothing large ever passes through the conversation.
"""

from __future__ import annotations

import asyncio
import base64
import os
import re
import subprocess
import warnings
from urllib.parse import urlparse

from fastmcp import Client
from starlette.testclient import TestClient

from leafbridge.hosted import create_hosted_server
from leafbridge.service import AccountService
from leafbridge.store import InMemoryStore, TokenCipher, User

warnings.filterwarnings("ignore", category=DeprecationWarning)
A, B = "0123456789abcdef01234567", "fedcba9876543210fedcba98"
SMALL = os.urandom(10 * 1024)
LARGE = b"\x89PNG\r\n\x1a\n" + os.urandom(200 * 1024)


def _git(args, cwd):
    p = subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True)
    assert p.returncode == 0, p.stderr
    return p.stdout


def _remote(tmp_path, name, files):
    remote, seed = tmp_path / f"{name}.git", tmp_path / f"{name}-seed"
    remote.mkdir()
    _git(["init", "--bare", "-b", "main", "."], remote)
    seed.mkdir()
    _git(["init", "-b", "main", "."], seed)
    for rel, data in files.items():
        (seed / rel).parent.mkdir(parents=True, exist_ok=True)
        (seed / rel).write_bytes(data)
    _git(["add", "-A"], seed)
    _git(["-c", "user.name=S", "-c", "user.email=s@t", "commit", "-qm", "init"], seed)
    _git(["push", "-q", remote.as_uri(), "main"], seed)
    return remote


def _world(tmp_path):
    a = _remote(tmp_path, "a", {"main.tex": b"\\documentclass{article}\\begin{document}A\\end{document}\n",
                                "figs/small.bin": SMALL, "figs/plot.png": LARGE})
    b = _remote(tmp_path, "b", {"main.tex": b"\\documentclass{article}\\begin{document}B\\end{document}\n"})
    store, cipher = InMemoryStore(), TokenCipher(TokenCipher.generate_key())
    svc = AccountService(store, cipher)
    asyncio.run(store.upsert_user(User(user_id="u", email="u@x.com", plan="pro")))
    asyncio.run(svc.connect_project("u", f"https://www.overleaf.com/project/{A}", "olp_x", "paper", git_url=a.as_uri()))
    asyncio.run(svc.add_project("u", f"https://www.overleaf.com/project/{B}", "slides", git_url=b.as_uri()))
    mcp = create_hosted_server(store=store, cipher=cipher, auth=False, identity_provider=lambda: ("u", "u@x.com"),
                               base_url="https://milatexai.com", data_dir=tmp_path / "data")
    return mcp, store, b


def _call(mcp, tool, args):
    async def go():
        async with Client(mcp) as c:
            r = await c.call_tool(tool, args, raise_on_error=False)
            return r.is_error, " ".join(getattr(x, "text", "") for x in r.content)
    return asyncio.run(go())


def test_small_files_still_come_back_as_base64(tmp_path):
    mcp, _, _ = _world(tmp_path)
    err, out = _call(mcp, "download_file", {"path": "figs/small.bin", "project": "paper"})
    assert not err and base64.b64decode(out) == SMALL


def test_large_files_come_back_as_a_link_that_returns_the_exact_bytes(tmp_path):
    mcp, store, _ = _world(tmp_path)
    err, out = _call(mcp, "download_file", {"path": "figs/plot.png", "project": "paper"})
    assert not err and "too large to pass through the chat" in out and len(out) < 2000
    link = re.search(r"https://milatexai\.com/dl\?code=\S+", out).group(0)
    (stored,) = store._downloads.values()
    assert LARGE[:64] not in stored                                    # encrypted at rest
    with TestClient(mcp.http_app()) as client:
        r = client.get(urlparse(link).path + "?" + urlparse(link).query)
    assert r.status_code == 200 and r.content == LARGE
    assert r.headers["content-type"] == "image/png" and 'filename="plot.png"' in r.headers["content-disposition"]


def test_copy_file_puts_the_exact_file_in_the_other_project(tmp_path):
    mcp, _, b_remote = _world(tmp_path)
    err, out = _call(mcp, "copy_file", {"path": "figs/plot.png", "to_project": "slides", "project": "paper"})
    assert not err and "Copied figs/plot.png" in out and "'slides'" in out
    check = tmp_path / "check"
    _git(["clone", "-q", b_remote.as_uri(), str(check)], tmp_path)
    assert (check / "figs" / "plot.png").read_bytes() == LARGE
    assert "Copy figs/plot.png from paper" in _git(["log", "-1", "--format=%s"], check)


def test_copy_file_can_rename_and_refuses_copying_a_file_onto_itself(tmp_path):
    mcp, _, b_remote = _world(tmp_path)
    err, _ = _call(mcp, "copy_file", {"path": "figs/small.bin", "to_project": "slides", "to_path": "img/s.bin",
                                      "project": "paper"})
    assert not err
    err, out = _call(mcp, "copy_file", {"path": "main.tex", "to_project": "paper", "project": "paper"})
    assert err and "same file" in out
    err, out = _call(mcp, "copy_file", {"path": "nope.png", "to_project": "slides", "project": "paper"})
    assert err and "nope.png does not exist" in out
