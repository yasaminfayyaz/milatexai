"""tracked_changes_pdf sees edits inside \\input / \\include files, not only the main file.

A thesis is usually thesis.tex plus chapter files. An edit in chapters/ch6.tex used to be
invisible: only thesis.tex was diffed. Both versions are now expanded into one document
first (the old one from git at that commit), then compared.
"""

from __future__ import annotations

import asyncio
import subprocess
import warnings
from pathlib import Path

from fastmcp import Client

from leafbridge import texdiff
from leafbridge.hosted import create_hosted_server
from leafbridge.service import AccountService
from leafbridge.store import InMemoryStore, TokenCipher, User

warnings.filterwarnings("ignore", category=DeprecationWarning)
BS = chr(92)
MAIN = f"{BS}documentclass{{report}}\n{BS}begin{{document}}\nAbstract text.\n{BS}input{{chapters/ch6}}\n{BS}include{{chapters/ch7}}\n{BS}end{{document}}\n"


def _git(args, cwd):
    p = subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True)
    assert p.returncode == 0, p.stderr
    return p.stdout


def test_an_edit_inside_an_input_chapter_reaches_the_diff(tmp_path, monkeypatch):
    remote, seed = tmp_path / "r.git", tmp_path / "seed"
    remote.mkdir()
    _git(["init", "-q", "--bare", "-b", "main", "."], remote)
    seed.mkdir()
    _git(["init", "-q", "-b", "main", "."], seed)
    (seed / "chapters").mkdir()
    (seed / "main.tex").write_text(MAIN)
    (seed / "chapters" / "ch6.tex").write_text("Future work is planned carefully.\n")
    (seed / "chapters" / "ch7.tex").write_text("Appendix stays the same.\n")
    _git(["add", "-A"], seed)
    _git(["-c", "user.name=S", "-c", "user.email=s@t", "commit", "-qm", "v1"], seed)
    (seed / "chapters" / "ch6.tex").write_text("Future work is planned boldly.\n")    # only the chapter changes
    _git(["-c", "user.name=S", "-c", "user.email=s@t", "commit", "-qam", "v2"], seed)
    _git(["push", "-q", remote.as_uri(), "main"], seed)

    store, cipher = InMemoryStore(), TokenCipher(TokenCipher.generate_key())
    asyncio.run(store.upsert_user(User(user_id="u", email="u@x.com", plan="pro")))
    asyncio.run(AccountService(store, cipher).connect_project(
        "u", "https://www.overleaf.com/project/0123456789abcdef01234567", "olp_x", "thesis", git_url=remote.as_uri()))

    seen = {}
    real_run = subprocess.run

    def fake_run(cmd, cwd=None, **kw):
        if cmd[0] == "latexdiff":
            seen["old"] = (Path(cwd) / cmd[-2]).read_text()
            seen["new"] = (Path(cwd) / cmd[-1]).read_text()
            return subprocess.CompletedProcess(cmd, 0, "\\documentclass{article}\\begin{document}x\\end{document}", "")
        if "compile" in cmd:                        # the engine: hand back a one-page PDF
            import fitz
            out = Path(cmd[cmd.index("--outdir") + 1])
            doc = fitz.open()
            doc.new_page()
            doc.save(str(out / "__mila_diff.pdf"))
            return subprocess.CompletedProcess(cmd, 0, "", "")
        return real_run(cmd, cwd=cwd, **kw)
    monkeypatch.setattr(texdiff, "latexdiff_cmd", lambda: ["latexdiff"])
    monkeypatch.setattr(texdiff.subprocess, "run", fake_run)
    mcp = create_hosted_server(store=store, cipher=cipher, auth=False, identity_provider=lambda: ("u", "u@x.com"),
                               base_url="https://milatexai.com", data_dir=tmp_path / "data")

    async def go():
        async with Client(mcp) as c:
            return await c.call_tool("tracked_changes_pdf", {"ref": "HEAD~1"}, raise_on_error=False)
    r = asyncio.run(go())
    assert not r.is_error, r.content
    assert "planned carefully" in seen["old"] and "planned boldly" not in seen["old"]
    assert "planned boldly" in seen["new"] and "planned carefully" not in seen["new"]
    assert "Appendix stays the same." in seen["old"] and "Appendix stays the same." in seen["new"]
    assert "\\input{" not in seen["new"] and "\\include{" not in seen["new"]
