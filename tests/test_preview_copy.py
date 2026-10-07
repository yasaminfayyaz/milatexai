"""Previews of papers with huge images are built from a temporary copy with smaller images.

A 45 megapixel PNG is generated here (tiny on disk, 135 MB decoded). The promises:
- only images above the limit are replaced, in a temporary copy without git history, which is
  removed afterwards; the project itself is never changed (byte for byte);
- the smaller image keeps its printed size: its resolution drops by the same factor;
- the compile helpers build from that copy and still hand back their usual results (the PDF next
  to the main file), with a note saying a smaller copy was used.
"""

from __future__ import annotations

import hashlib
import struct
import subprocess
import sys
import zlib
from pathlib import Path

import pytest

from leafbridge import texcompile, texlocate, texmemory

fitz = pytest.importorskip("fitz")
W, H = 9000, 5000                      # 45 megapixels: above the 40 MP limit


def make_png(path: Path, w: int, h: int, dpi: float | None = None) -> None:
    """A real RGB PNG (all one colour, so tiny on disk), with a pHYs chunk only if dpi is given."""
    def chunk(kind, data):
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)
    raw = (b"\x00" + bytes([200]) * (w * 3)) * h
    phys = chunk(b"pHYs", struct.pack(">IIB", round(dpi / 0.0254), round(dpi / 0.0254), 1)) if dpi else b""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
                     + phys + chunk(b"IDAT", zlib.compress(raw, 1)) + chunk(b"IEND", b""))


@pytest.fixture(scope="module")
def big_png(tmp_path_factory):
    p = tmp_path_factory.mktemp("img") / "big.png"
    make_png(p, W, H)
    return p


def _project(tmp_path: Path, big_png: Path, dpi=None) -> Path:
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    (repo / ".git" / "HEAD").write_text("ref: refs/heads/main\n")
    (repo / "main.tex").write_text("\\documentclass{article}\\begin{document}x\\end{document}\n")
    if dpi:
        make_png(repo / "figs" / "big.png", W, H, dpi)
    else:
        (repo / "figs").mkdir()
        (repo / "figs" / "big.png").write_bytes(big_png.read_bytes())
    (repo / "figs" / "small.png").write_bytes(b"not really an image")
    return repo


def _digest(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def test_the_resolution_an_image_records_is_read():
    import tempfile
    d = Path(tempfile.mkdtemp())
    make_png(d / "a.png", 10, 10)
    make_png(d / "b.png", 10, 10, dpi=300)
    assert texmemory.image_dpi(d / "a.png") is None
    assert texmemory.image_dpi(d / "b.png") == pytest.approx((300, 300), rel=1e-3)
    jfif = b"\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01\x01\x01\x00\x96\x00\x96\x00\x00"
    (d / "c.jpg").write_bytes(jfif + b"\xff\xd9")
    assert texmemory.image_dpi(d / "c.jpg") == (150.0, 150.0)


def test_without_huge_images_the_project_itself_is_used(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    make_png(repo / "ok.png", 3000, 2000)
    with texmemory.preview_tree(repo) as (src, note):
        assert src == repo and note == ""


@pytest.mark.parametrize("dpi, expected_dpi", [(None, 72 / 4), (300, 300 / 4)])
def test_a_huge_image_is_shrunk_in_a_temporary_copy_at_the_same_printed_size(tmp_path, big_png, dpi, expected_dpi):
    repo = _project(tmp_path, big_png, dpi)
    before = {p.relative_to(repo): _digest(p) for p in repo.rglob("*") if p.is_file()}
    with texmemory.preview_tree(repo) as (src, note):
        assert src != repo and not (src / ".git").exists() and (src / "main.tex").is_file()
        w, h = texmemory.image_size(src / "figs" / "big.png")
        assert max(w, h) <= texmemory.PREVIEW_MAX_SIDE and (w, h) == (W // 4, H // 4)
        got_dpi = texmemory.image_dpi(src / "figs" / "big.png")
        assert got_dpi == pytest.approx((expected_dpi, expected_dpi), rel=2e-3)
        assert w / got_dpi[0] == pytest.approx(W / (dpi or 72), rel=2e-3)     # same printed width
        assert (src / "figs" / "small.png").read_bytes() == b"not really an image"
        assert "figs/big.png (9,000 x 5,000 pixels)" in note and "Your files are unchanged" in note
        copy_root = src.parent
    assert not copy_root.exists()                                            # removed afterwards
    assert {p.relative_to(repo): _digest(p) for p in repo.rglob("*") if p.is_file()} == before


def test_if_an_image_cannot_be_shrunk_it_is_left_as_it_is(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    # Only a header: large on paper, but not a decodable image.
    ihdr = struct.pack(">IIBBBBB", W, H, 8, 2, 0, 0, 0)
    (repo / "fake.png").write_bytes(b"\x89PNG\r\n\x1a\n" + struct.pack(">I", 13) + b"IHDR" + ihdr
                                    + struct.pack(">I", zlib.crc32(b"IHDR" + ihdr)))
    with texmemory.preview_tree(repo) as (src, note):
        assert (src / "fake.png").read_bytes() == (repo / "fake.png").read_bytes() and note == ""


def test_the_ordinary_compile_runs_in_the_copy_and_says_so(tmp_path, big_png, monkeypatch):
    repo = _project(tmp_path, big_png)
    seen = {}

    real_run = subprocess.run

    def fake_run(cmd, cwd=None, **kw):
        if cmd[0] == sys.executable:                    # the shrinking helper: really run it
            return real_run(cmd, cwd=cwd, **kw)
        seen["cwd"] = Path(cwd)
        seen["size"] = texmemory.image_size(Path(cwd) / "figs" / "big.png")
        return subprocess.CompletedProcess(cmd, 1, "", "error: something")
    monkeypatch.setattr(texcompile.subprocess, "run", fake_run)
    res = texcompile._compile_sync("/fake/tectonic", repo, "main.tex", 60)
    assert seen["cwd"] != repo and seen["size"] == (W // 4, H // 4)
    assert "smaller copy" in res.note
    assert texmemory.image_size(repo / "figs" / "big.png") == (W, H)


def test_the_page_locating_compile_returns_its_pdf_next_to_the_real_main_file(tmp_path, big_png, monkeypatch):
    repo = _project(tmp_path, big_png)

    real_run = subprocess.run

    def fake_run(cmd, cwd=None, **kw):
        if cmd[0] == sys.executable:                    # the shrinking helper: really run it
            return real_run(cmd, cwd=cwd, **kw)
        stem = Path(cmd[-1]).stem
        (Path(cwd) / f"{stem}.aux").write_text("\\milafloat{1}{table}{1}{}\n")
        doc = fitz.open()
        doc.new_page()
        doc.save(str(Path(cwd) / f"{stem}.pdf"))
        return subprocess.CompletedProcess(cmd, 0, "", "")
    monkeypatch.setattr(texlocate.subprocess, "run", fake_run)
    res = texlocate.compile_and_locate(str(repo), "main.tex", "/fake/tectonic")
    assert res.pdf_path and Path(res.pdf_path).parent == repo and Path(res.pdf_path).is_file()
    assert "smaller copy" in res.note and res.clean
    assert texmemory.image_size(repo / "figs" / "big.png") == (W, H)
