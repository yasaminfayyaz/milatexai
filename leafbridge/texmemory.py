"""Why a compile was stopped, in words the user can act on.

A server copy has a fixed amount of memory. The LaTeX engine is killed if a paper needs more,
and then prints no LaTeX error at all, so "Compile FAILED." alone would wrongly suggest a
mistake in the paper. The usual cause is one very large image: a 14000 x 9000 pixel PNG is
1 MB on disk but about 500 MB once decoded, and writing it into the PDF needs about twice that.
These failures are about the server, not the paper version, so they are never remembered
(see buildcache.py): the next try runs again.
"""

from __future__ import annotations

import struct
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
