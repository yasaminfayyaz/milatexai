"""Why a compile was stopped, in words the user can act on.

A server copy has a fixed amount of memory. The LaTeX engine is killed if a paper needs more,
and then prints no LaTeX error at all, so "Compile FAILED." alone would wrongly suggest a
mistake in the paper. The usual cause is one very large image: a 14000 x 9000 pixel PNG is
1 MB on disk but about 500 MB once decoded, and writing it into the PDF needs about twice that.
These failures are about the server, not the paper version, so they are never remembered
(see buildcache.py): the next try runs again.

To avoid them, compiles for checking and previewing run from a temporary copy of the project
in which only such images are replaced by smaller versions (``preview_tree``). The image keeps
its printed size: its resolution is lowered by the same factor, so the layout is identical.
The project itself is never changed, and arXiv bundles keep the original images.
"""

from __future__ import annotations

import shutil
import struct
import subprocess
import sys
import tempfile
from contextlib import contextmanager
from pathlib import Path

STOPPED = "Compile stopped"
BIG_PIXELS = 40_000_000          # above this an image is worth pointing out
_TRANSIENT = (STOPPED, "Compile timed out", "Could not run", "compile failed to run")


def image_size(path: Path) -> tuple[int, int] | None:
    """(width, height) of a PNG or JPEG from its header, without decoding it."""
    try:
        with open(path, "rb") as fh:
            head = fh.read(64 * 1024)
    except OSError:
        return None
    if head[:8] == b"\x89PNG\r\n\x1a\n" and len(head) >= 24:
        w, h = struct.unpack(">II", head[16:24])
        return w, h
    if head[:2] == b"\xff\xd8":
        i = 2
        while i + 9 < len(head):
            if head[i] != 0xFF:
                i += 1
                continue
            marker = head[i + 1]
            if marker in (0xC0, 0xC1, 0xC2):
                h, w = struct.unpack(">HH", head[i + 5:i + 9])
                return w, h
            i += 2 + struct.unpack(">H", head[i + 2:i + 4])[0]
    return None


PREVIEW_MAX_SIDE = 4000          # pixels on the long side of the smaller copy: plenty for a page
_INCH = 0.0254


def image_dpi(path: Path) -> tuple[float, float] | None:
    """The resolution an image file records (PNG pHYs in metres, JPEG JFIF density), or None
    when it records none; LaTeX then assumes 72 dpi."""
    try:
        with open(path, "rb") as fh:
            head = fh.read(256 * 1024)
    except OSError:
        return None
    if head[:8] == b"\x89PNG\r\n\x1a\n":
        i = 8
        while i + 8 <= len(head):
            length, kind = struct.unpack(">I4s", head[i:i + 8])
            if kind == b"pHYs" and length == 9:
                x, y, unit = struct.unpack(">IIB", head[i + 8:i + 17])
                return (x * _INCH, y * _INCH) if unit == 1 and x and y else None
            if kind in (b"IDAT", b"IEND"):
                return None
            i += 12 + length
        return None
    if head[:2] == b"\xff\xd8" and head[6:11] == b"JFIF\x00":
        units, x, y = head[13], *struct.unpack(">HH", head[14:18])
        if units == 1 and x and y:
            return float(x), float(y)
        if units == 2 and x and y:
            return x * 2.54, y * 2.54
    return None


# Runs in a separate process, so a failure only costs that process, never the server. Pillow
# decodes a PNG row by row into one buffer (about width x height x channels bytes, once), and a
# JPEG straight at the reduced size. A damaged or truncated image raises: the original is kept.
# Arguments: path, k (shrink by 2**k), the resolution to record in pixels per metre (x, y).
_SHRINK = r"""
import sys
from PIL import Image
path, k, px, py = sys.argv[1], int(sys.argv[2]), int(sys.argv[3]), int(sys.argv[4])
Image.MAX_IMAGE_PIXELS = None            # the size was checked by the caller
with Image.open(path) as im:
    fmt, (w, h) = im.format, im.size
    tw, th = max(1, w >> k), max(1, h >> k)
    if fmt == "JPEG":
        im.draft(im.mode, (tw, th))      # decode at (close to) the target size
    im.load()
    if im.mode not in ("1", "L", "LA", "RGB", "RGBA", "I", "F"):
        im = im.convert("RGBA" if im.mode in ("P", "PA") and "transparency" in im.info else "RGB")
    factor = max(1, round(im.size[0] / tw))
    small = im.reduce(factor) if factor > 1 else im
    dpi = (px * 0.0254, py * 0.0254)
    if fmt == "JPEG":
        small.save(path, format="JPEG", quality=92, dpi=(round(dpi[0]), round(dpi[1])))
    else:
        small.save(path, format="PNG", dpi=dpi)
"""
# Never try to decode more than this (bytes): the server and the engine share the same memory.
SHRINK_BUDGET = 700 * 1024 * 1024


def _decode_bytes(path: Path, width: int, height: int) -> int:
    """Memory Pillow needs to hold the decoded image (JPEGs decode small, so they are cheap)."""
    try:
        with open(path, "rb") as fh:
            head = fh.read(32)
    except OSError:
        return 0
    if head[:8] == b"\x89PNG\r\n\x1a\n":
        depth, ctype = head[24], head[25]
        channels = {0: 1, 2: 3, 3: 1, 4: 2, 6: 4}.get(ctype, 4)
        return width * height * channels * (2 if depth == 16 else 1)
    return width * height * 3 // 16


def _shrink(path: Path, width: int, height: int) -> bool:
    """Replace ``path`` with a copy at most PREVIEW_MAX_SIDE on its long side and the same
    printed size. False (file untouched) if it could not be done."""
    if _decode_bytes(path, width, height) > SHRINK_BUDGET:
        return False
    k = 0
    while max(width, height) / 2 ** k > PREVIEW_MAX_SIDE:
        k += 1
    dpi_x, dpi_y = image_dpi(path) or (72.0, 72.0)
    ppm_x, ppm_y = (max(1, round(d / _INCH / 2 ** k)) for d in (dpi_x, dpi_y))
    try:
        done = subprocess.run([sys.executable, "-c", _SHRINK, str(path), str(k), str(ppm_x), str(ppm_y)],
                              capture_output=True, timeout=180)
    except (OSError, subprocess.TimeoutExpired):
        return False
    return done.returncode == 0


@contextmanager
def preview_tree(repo: Path):
    """Yield (directory to compile in, note for the user). Without oversized images that is the
    project itself and no note. Otherwise a temporary copy (no git history) where only those
    images are smaller, removed afterwards; the project is never touched."""
    repo = Path(repo)
    big = oversized_images(repo)
    if not big:
        yield repo, ""
        return
    tmp = Path(tempfile.mkdtemp(prefix="mila_preview_"))
    try:
        src = tmp / "src"
        shutil.copytree(repo, src, ignore=shutil.ignore_patterns(".git"), symlinks=True)
        shrunk = [(rel, w, h) for rel, w, h in big if _shrink(src / rel, w, h)]
        note = ""
        if shrunk:
            names = ", ".join(f"{rel} ({w:,} x {h:,} pixels)" for rel, w, h in shrunk)
            note = (f"Note: {names} {'is' if len(shrunk) == 1 else 'are'} too large for the compile "
                    "server, so this was built with a smaller copy at the same printed size. "
                    "Your files are unchanged.")
        yield src, note
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def oversized_images(repo: Path) -> list[tuple[str, int, int]]:
    """Images above BIG_PIXELS in the project, largest first, as (path, width, height)."""
    repo = Path(repo)
    found = []
    for f in repo.rglob("*"):
        if f.suffix.lower() in (".png", ".jpg", ".jpeg") and ".git" not in f.parts:
            size = image_size(f)
            if size and size[0] * size[1] > BIG_PIXELS:
                found.append((f.relative_to(repo).as_posix(), *size))
    return sorted(found, key=lambda x: x[1] * x[2], reverse=True)


def stopped_message(repo: Path, returncode: int) -> str:
    """What to tell the user when the engine was killed (negative return code)."""
    if returncode == -9:
        msg = (f"{STOPPED}: the LaTeX engine ran out of memory on our server. "
               "This is not an error in your paper.")
    else:
        msg = (f"{STOPPED} unexpectedly (signal {-returncode}). "
               "This is not an error in your paper; please try again.")
    big = oversized_images(repo)
    if big:
        path, w, h = big[0]
        msg += (f" The likely cause is {path}, which is {w:,} x {h:,} pixels (about "
                f"{w * h // 1_000_000} megapixels) and needs several hundred MB of memory to place "
                "in the PDF. Scaling it down to about 3000 pixels wide looks the same in print "
                "and fixes this.")
    return msg


def is_transient(message: str | None) -> bool:
    """True for failures caused by the server (memory, time, engine start), not the paper."""
    return bool(message) and message.startswith(_TRANSIENT)
