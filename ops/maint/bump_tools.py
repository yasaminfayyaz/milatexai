"""Bump the pinned Tectonic and latexdiff versions in the Dockerfile to their
latest stable GitHub releases, but only when the exact download asset the
Dockerfile uses actually exists (a missing asset would break every build).
Prints one line per tool. Safe to run repeatedly."""

from __future__ import annotations

import json
import os
import re
import urllib.request
from pathlib import Path

DOCKERFILE = Path(__file__).resolve().parents[2] / "Dockerfile"
TOOLS = {
    # ARG name: (repo, tag regex -> version, asset URL template)
    "TECTONIC_VERSION": (
        "tectonic-typesetting/tectonic",
        r"^tectonic@(\d+\.\d+\.\d+)$",
        "https://github.com/tectonic-typesetting/tectonic/releases/download/"
        "tectonic%40{v}/tectonic-{v}-x86_64-unknown-linux-musl.tar.gz",
    ),
    "LATEXDIFF_VERSION": (
        "ftilmann/latexdiff",
        r"^v?(\d+\.\d+\.\d+)$",
        "https://github.com/ftilmann/latexdiff/releases/download/{v}/latexdiff-{v}.tar.gz",
    ),
}


def _get(url: str, method: str = "GET"):
    req = urllib.request.Request(url, method=method)
    token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")
    if token and "api.github.com" in url:
        req.add_header("Authorization", f"Bearer {token}")
    return urllib.request.urlopen(req, timeout=30)


def _vkey(v: str) -> tuple[int, ...]:
    return tuple(int(x) for x in v.split("."))


def latest(repo: str, tag_re: str) -> str | None:
    with _get(f"https://api.github.com/repos/{repo}/releases?per_page=50") as r:
        releases = json.load(r)
    versions = []
    for rel in releases:
        if rel.get("prerelease") or rel.get("draft"):
            continue
        m = re.match(tag_re, rel.get("tag_name", ""))
        if m:
            versions.append(m.group(1))
    return max(versions, key=_vkey) if versions else None


def asset_exists(url: str) -> bool:
    try:
        with _get(url, method="HEAD") as r:
            return r.status == 200
    except Exception:  # noqa: BLE001
        return False


def main() -> None:
    text = DOCKERFILE.read_text(encoding="utf-8")
    for arg, (repo, tag_re, asset) in TOOLS.items():
        m = re.search(rf"^ARG {arg}=(\S+)$", text, re.M)
        if not m:
            print(f"{arg}: not found in Dockerfile, skipped")
            continue
        current = m.group(1)
        try:
            newest = latest(repo, tag_re)
        except Exception as exc:  # noqa: BLE001
            print(f"{arg}: could not query releases ({exc}), kept {current}")
            continue
        if not newest or _vkey(newest) <= _vkey(current):
            print(f"{arg}: {current} is current")
            continue
        if not asset_exists(asset.format(v=newest)):
            print(f"{arg}: {newest} released but its build asset is missing, kept {current}")
            continue
        text = text.replace(m.group(0), f"ARG {arg}={newest}")
        print(f"{arg}: {current} -> {newest}")
    DOCKERFILE.write_bytes(text.encode("utf-8"))


if __name__ == "__main__":
    main()
