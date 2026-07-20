"""Figure Studio tests: slug/header conventions, the sessions client, and the
commit_figure / list_figures tools driven end-to-end against a real local git
remote with a faked sandbox."""

from __future__ import annotations

import asyncio
import subprocess
import warnings
from pathlib import Path

import pytest

from leafbridge import figures
from leafbridge.figures import FigureError
from leafbridge.hosted import create_hosted_server
from leafbridge.sessions import ExecResult, SessionsClient, SessionsError
from leafbridge.store import InMemoryStore, TokenCipher, User

warnings.filterwarnings("ignore", category=DeprecationWarning)

HEX = "0123456789abcdef01234567"
OVERLEAF_URL = f"https://www.overleaf.com/project/{HEX}"


# --- figures.py: slugs + headers -------------------------------------------

def test_slugify_normalizes():
    assert figures.slugify("Energy vs Time") == "energy-vs-time"
    assert figures.slugify("speedup") == "speedup"
    assert figures.slugify("  A__B  ") == "a-b"
    assert len(figures.slugify("x" * 100)) <= 40


def test_slugify_rejects_garbage():
    for bad in ("", "###", "___", "  "):
        with pytest.raises(FigureError):
            figures.slugify(bad)


def test_header_round_trip_and_tolerance():
    head = figures.build_header("speedup")
    parsed = figures.parse_header(head + "import matplotlib\n")
    assert parsed["figure"] == "speedup"
    assert parsed["output"] == "figures/speedup.pdf"
    # Tolerant: reordered/missing lines still parse while 'figure:' survives.
    assert figures.parse_header("# tool: milatexai/1\n# figure: x\n")["figure"] == "x"
    assert figures.parse_header("import numpy\n") is None


def test_scan_figures(tmp_path: Path):
    src = tmp_path / "figures" / "src"
    src.mkdir(parents=True)
    (src / "alpha.py").write_text(figures.build_header("alpha") + "code\n")
    (src / "beta.py").write_text("print('no header')\n")  # counts by filename
    (tmp_path / "figures" / "alpha.pdf").write_bytes(b"%PDF-fake")
    found = {f.slug: f for f in figures.scan_figures(tmp_path)}
    assert found["alpha"].out_exists is True
    assert found["beta"].out_exists is False
    assert figures.scan_figures(tmp_path / "nowhere") == []


def _write_pair(repo: Path, slug: str, code: str, pdf: bytes):
    body = code.rstrip("\n") + "\n"
    src = repo / "figures" / "src"
    src.mkdir(parents=True, exist_ok=True)
    (src / f"{slug}.py").write_text(
        figures.build_header(slug, code_body=body, pdf_bytes=pdf) + body, encoding="utf-8")
    (repo / "figures" / f"{slug}.pdf").write_bytes(pdf)


def test_sync_state_detects_all_four_states(tmp_path: Path):
    _write_pair(tmp_path, "a", "plot()\n", b"%PDF-one")
    info = figures.scan_figures(tmp_path)[0]
    assert figures.sync_state(tmp_path, info) == figures.IN_SYNC

    # Hand-edit the code below the header -> PDF is stale.
    p = tmp_path / "figures" / "src" / "a.py"
    p.write_text(p.read_text() + "plt.title('tweak')\n", encoding="utf-8")
    assert figures.sync_state(tmp_path, info) == figures.CODE_EDITED

    # Restore code, replace the PDF externally -> code is NOT ground truth.
    _write_pair(tmp_path, "a", "plot()\n", b"%PDF-one")
    (tmp_path / "figures" / "a.pdf").write_bytes(b"%PDF-other")
    assert figures.sync_state(tmp_path, info) == figures.ARTIFACT_REPLACED

    # Change both -> diverged.
    p.write_text(p.read_text() + "extra\n", encoding="utf-8")
    assert figures.sync_state(tmp_path, info) == figures.DIVERGED


def test_sync_state_output_missing_and_untracked(tmp_path: Path):
    _write_pair(tmp_path, "b", "plot()\n", b"%PDF-x")
    info = figures.scan_figures(tmp_path)[0]
    (tmp_path / "figures" / "b.pdf").unlink()
    assert figures.sync_state(tmp_path, info) == figures.OUTPUT_MISSING
    # A hand-made script with no provenance header is untracked.
    (tmp_path / "figures" / "src" / "c.py").write_text("print('mine')\n")
    infos = {f.slug: f for f in figures.scan_figures(tmp_path)}
    assert figures.sync_state(tmp_path, infos["c"]) == figures.UNTRACKED


def test_parse_deleted_maps_slug_to_commit_and_skips_readded():
    log = "@abc123\nfigures/src/old-fig.py\n\n@def456\nfigures/src/readded.py\n"
    out = figures.parse_deleted(log, live_slugs={"readded"})
    assert out == {"old-fig": "abc123"}


def test_pdf_to_png_produces_png():
    import fitz

    doc = fitz.open()
    doc.new_page(width=200, height=120)
    pdf = doc.tobytes()
    png = figures.pdf_to_png(pdf)
    assert png[:8] == b"\x89PNG\r\n\x1a\n"


# --- sessions.py ------------------------------------------------------------

def test_session_ids_stable_unguessable_per_user():
    c = SessionsClient("https://x.example/pool", secret="s1")
    a1, a2 = c.session_for("user_a"), c.session_for("user_a")
    b = c.session_for("user_b")
    other_secret = SessionsClient("https://x.example/pool", secret="s2").session_for("user_a")
    assert a1 == a2 and a1 != b and a1 != other_secret
    assert a1.startswith("fig-") and len(a1) == 4 + 32


class FakeTransportClient(SessionsClient):
    """SessionsClient with a scripted _request."""

    def __init__(self, responses):
        super().__init__("https://fake.pool/x", secret="s")
        self._responses = list(responses)
        self.calls = []

    async def _request(self, method, path_and_query, json_body=None, *, timeout=150):
        self.calls.append((method, path_and_query))
        return self._responses.pop(0)


def _exec_body(status="Succeeded", stdout="", stderr="", image=None):
    import json

    result = {"stdout": stdout, "stderr": stderr}
    if image is not None:
        result["executionResult"] = {"type": "image", "format": "png", "base64_data": image}
    return json.dumps({"status": status, "result": result}).encode()


def test_execute_parses_success_and_image():
    c = FakeTransportClient([(200, _exec_body(stdout="hi", image="QUJD"))])
    res = asyncio.run(c.execute("fig-x", "print('hi')"))
    assert res.ok and res.stdout == "hi" and res.image_b64 == "QUJD"


def test_execute_surfaces_failure_and_http_errors():
    c = FakeTransportClient([(200, _exec_body(status="Failed", stderr="boom"))])
    res = asyncio.run(c.execute("fig-x", "x"))
    assert not res.ok and res.stderr == "boom"
    c2 = FakeTransportClient([(500, b"oops")])
    with pytest.raises(SessionsError):
        asyncio.run(c2.execute("fig-x", "x"))
    with pytest.raises(SessionsError):
        asyncio.run(SessionsClient("").execute("fig-x", "x"))  # disabled


def test_download_error_raises():
    c = FakeTransportClient([(404, b"nope")])
    with pytest.raises(SessionsError):
        asyncio.run(c.download("fig-x", "figure.pdf"))


# --- tool-level: real git remote + faked sandbox ----------------------------

def _git(args, cwd):
    proc = subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr or proc.stdout
    return proc.stdout


def _make_remote(tmp_path: Path) -> str:
    remote = tmp_path / "remote.git"
    seed = tmp_path / "seed"
    remote.mkdir()
    _git(["init", "--bare", "-b", "main", "."], remote)
    seed.mkdir()
    _git(["init", "-b", "main", "."], seed)
    (seed / "main.tex").write_text("\\documentclass{article}\\begin{document}x\\end{document}\n")
    _git(["add", "-A"], seed)
    _git(["-c", "user.name=S", "-c", "user.email=s@t", "commit", "-m", "init"], seed)
    _git(["remote", "add", "origin", remote.as_uri()], seed)
    _git(["push", "-u", "origin", "main"], seed)
    return remote.as_uri()


def _fake_pdf() -> bytes:
    import fitz

    doc = fitz.open()
    doc.new_page(width=300, height=200)
    return doc.tobytes()


class FakeSandbox(SessionsClient):
    def __init__(self, *, pdf: bytes | None = None, fail=False, stderr=""):
        super().__init__("https://fake.pool/x", secret="s")
        self.pdf = pdf
        self.fail = fail
        self.stderr = stderr
        self.executed: list[str] = []

    async def execute(self, session_id, code):
        self.executed.append(code)
        if self.fail:
            return ExecResult(ok=False, stderr=self.stderr or "Traceback: boom")
        return ExecResult(ok=True, stdout="ok")

    async def download(self, session_id, filename):
        if self.pdf is None:
            raise SessionsError("no file")
        return self.pdf

    async def list_files(self, session_id):
        return [] if self.pdf is None else ["figure.pdf"]


def _harness(tmp_path: Path, *, plan="pro", admin=False, sandbox=None):
    store = InMemoryStore()
    cipher = TokenCipher(TokenCipher.generate_key())
    uid, email = "user_fig", "fig@example.com"
    asyncio.run(store.upsert_user(User(user_id=uid, email=email, plan=plan, is_admin=admin)))
    mcp = create_hosted_server(
        store=store, cipher=cipher, auth=False,
        identity_provider=lambda: (uid, email),
        base_url="https://milatexai.com",
        data_dir=tmp_path / "cache",
        sessions=sandbox if sandbox is not None else FakeSandbox(pdf=_fake_pdf()),
    )
    remote = _make_remote(tmp_path)
    from leafbridge.service import AccountService

    svc = AccountService(store, cipher)
    asyncio.run(svc.connect_project(uid, OVERLEAF_URL, "olp_dummy", "paper", git_url=remote))
    return mcp, store, tmp_path


def _call(mcp, tool, args):
    from fastmcp import Client

    async def go():
        async with Client(mcp) as client:
            return await client.call_tool(tool, args)

    return asyncio.run(go())


def _text(result) -> str:
    blocks = getattr(result, "content", None) or []
    return "".join(getattr(b, "text", "") for b in blocks)


CODE = "import matplotlib.pyplot as plt\nplt.plot([1,2],[3,4])\nplt.savefig('figure.pdf')\n"


def test_commit_figure_free_user_is_gated(tmp_path):
    mcp, _s, _t = _harness(tmp_path, plan="free")
    from fastmcp.exceptions import ToolError

    with pytest.raises(ToolError, match="Pro feature"):
        _call(mcp, "commit_figure", {"code": CODE, "name": "speedup"})
    with pytest.raises(ToolError, match="Pro feature"):
        _call(mcp, "list_figures", {})


def test_commit_figure_admin_bypasses_gate(tmp_path):
    mcp, _s, _t = _harness(tmp_path, plan="free", admin=True)
    r = _call(mcp, "commit_figure", {"code": CODE, "name": "speedup"})
    assert "Committed figures/src/speedup.py" in _text(r)


def test_commit_figure_pro_commits_source_and_pdf(tmp_path):
    pdf = _fake_pdf()
    sandbox = FakeSandbox(pdf=pdf)
    mcp, _s, _t = _harness(tmp_path, sandbox=sandbox)
    r = _call(mcp, "commit_figure", {"code": CODE, "name": "Energy vs Time", "format": "pdf"})
    text = _text(r)
    assert "figures/src/energy-vs-time.py" in text
    assert "fig:energy-vs-time" in text  # include-snippet suggested
    # The shim was prepended (fresh figure.pdf + chdir into /mnt/data).
    assert sandbox.executed and "_os.chdir('/mnt/data')" in sandbox.executed[0]
    # Verify on the REMOTE: clone fresh and inspect.
    verify = tmp_path / "verify"
    # autocrlf=false: verify BYTES as committed (a tiny all-ASCII PDF would
    # otherwise be "helpfully" CRLF-converted by a default Windows checkout).
    _git(["-c", "core.autocrlf=false", "clone", (tmp_path / "remote.git").as_uri(), str(verify)], tmp_path)
    src = (verify / "figures" / "src" / "energy-vs-time.py").read_text()
    assert src.startswith("# === milatexai figure ===")
    assert "figure: energy-vs-time" in src
    assert CODE.strip() in src
    assert (verify / "figures" / "energy-vs-time.pdf").read_bytes() == pdf
    # An image block came back (committed-artifact preview).
    kinds = {type(b).__name__ for b in r.content}
    assert any("Image" in k for k in kinds)


def test_commit_figure_png_format_and_switch_cleans_sibling(tmp_path):
    mcp, _s, _t = _harness(tmp_path)
    # No format arg: PNG must be the DEFAULT.
    r = _call(mcp, "commit_figure", {"code": CODE, "name": "speedup"})
    text = _text(r)
    assert "figures/speedup.png" in text
    verify = tmp_path / "v1"
    _git(["-c", "core.autocrlf=false", "clone", (tmp_path / "remote.git").as_uri(), str(verify)], tmp_path)
    png = (verify / "figures" / "speedup.png").read_bytes()
    assert png[:8] == b"\x89PNG\r\n\x1a\n"
    src = (verify / "figures" / "src" / "speedup.py").read_text()
    assert "output: figures/speedup.png" in src
    # In-sync state holds for PNG artifacts too.
    listing = _text(_call(mcp, "list_figures", {}))
    assert "in sync" in listing
    # Switch back to pdf: the stale .png must be removed in the same commit.
    _call(mcp, "commit_figure", {"code": CODE, "name": "speedup", "format": "pdf"})
    verify2 = tmp_path / "v2"
    _git(["-c", "core.autocrlf=false", "clone", (tmp_path / "remote.git").as_uri(), str(verify2)], tmp_path)
    assert (verify2 / "figures" / "speedup.pdf").is_file()
    assert not (verify2 / "figures" / "speedup.png").exists()


def test_commit_figure_rejects_unknown_format(tmp_path):
    mcp, _s, _t = _harness(tmp_path)
    from fastmcp.exceptions import ToolError

    with pytest.raises(ToolError, match="pdf.*png|png.*pdf"):
        _call(mcp, "commit_figure", {"code": CODE, "name": "x9", "format": "svg"})


def test_commit_figure_no_pdf_produced_lists_files(tmp_path):
    mcp, _s, _t = _harness(tmp_path, sandbox=FakeSandbox(pdf=None))
    from fastmcp.exceptions import ToolError

    with pytest.raises(ToolError, match="produced no figure.pdf"):
        _call(mcp, "commit_figure", {"code": "print(1)", "name": "x1"})


def test_commit_figure_sandbox_failure_surfaces_stderr(tmp_path):
    mcp, _s, _t = _harness(tmp_path, sandbox=FakeSandbox(fail=True, stderr="NameError: nope"))
    from fastmcp.exceptions import ToolError

    with pytest.raises(ToolError, match="NameError"):
        _call(mcp, "commit_figure", {"code": "nope()", "name": "x2"})


def test_commit_figure_rejects_non_pdf(tmp_path):
    mcp, _s, _t = _harness(tmp_path, sandbox=FakeSandbox(pdf=b"not a pdf"))
    from fastmcp.exceptions import ToolError

    with pytest.raises(ToolError, match="not a valid PDF"):
        _call(mcp, "commit_figure", {"code": CODE, "name": "x3"})


def test_commit_figure_bad_name(tmp_path):
    mcp, _s, _t = _harness(tmp_path)
    from fastmcp.exceptions import ToolError

    with pytest.raises(ToolError, match="Figure names"):
        _call(mcp, "commit_figure", {"code": CODE, "name": "###"})


def test_sessions_disabled_message(tmp_path):
    mcp, _s, _t = _harness(tmp_path, sandbox=SessionsClient(""))
    from fastmcp.exceptions import ToolError

    with pytest.raises(ToolError, match="isn't available"):
        _call(mcp, "commit_figure", {"code": CODE, "name": "x4"})


def test_list_figures_lifecycle_including_deletion_memory(tmp_path):
    mcp, _s, _t = _harness(tmp_path)
    assert "No Figure Studio figures" in _text(_call(mcp, "list_figures", {}))
    _call(mcp, "commit_figure", {"code": CODE, "name": "speedup"})
    listing = _text(_call(mcp, "list_figures", {}))
    assert "speedup" in listing and "in sync" in listing
    # Replace the PDF "outside Figure Studio" (as if the user uploaded their own)
    # and push; the listing must flag that the code is no longer ground truth.
    _call(mcp, "upload_file", {
        "path": "figures/speedup.png",
        "content_base64": __import__("base64").b64encode(b"%PDF-external-edit").decode(),
    })
    listing = _text(_call(mcp, "list_figures", {}))
    assert "NOT ground truth" in listing
    # Delete the source through the normal tool, then the listing must remember it.
    _call(mcp, "delete_file", {"path": "figures/src/speedup.py"})
    listing = _text(_call(mcp, "list_figures", {}))
    assert "recoverable from git history" in listing
    assert "git show" in listing and "speedup" in listing
