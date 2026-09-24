"""List dependencies held back by an upper version cap (e.g. ``fastmcp<4``)
while a newer major release exists on PyPI. The weekly maintenance agent
attempts each one; routine upgrades within the caps happen mechanically in
refresh.sh. Prints one line per held-back package (nothing if none)."""

from __future__ import annotations

import json
import re
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SPEC = re.compile(r"^\s*([A-Za-z0-9_.\-]+)\s*([^#]*?)\s*(#.*)?$")


def _vkey(v: str) -> tuple[int, ...]:
    return tuple(int(x) for x in re.findall(r"\d+", v)[:3])


def latest_stable(name: str) -> str | None:
    with urllib.request.urlopen(f"https://pypi.org/pypi/{name}/json", timeout=30) as r:
        releases = json.load(r)["releases"]
    stable = [v for v, files in releases.items()
              if files and re.fullmatch(r"\d+(\.\d+)*", v)]
    return max(stable, key=_vkey) if stable else None


def main() -> None:
    for fname in ("requirements.in", "requirements-dev.in"):
        for line in (ROOT / fname).read_text(encoding="utf-8").splitlines():
            if not line.strip() or line.lstrip().startswith(("#", "-")):
                continue
            m = SPEC.match(line)
            if not m:
                continue
            name, spec = m.group(1), m.group(2)
            cap = re.search(r"<\s*([0-9][0-9.]*)", spec)
            if not cap:
                continue
            try:
                newest = latest_stable(name)
            except Exception as exc:  # noqa: BLE001
                print(f"{name}: could not query PyPI ({exc}), skipped")
                continue
            if newest and _vkey(newest) >= _vkey(cap.group(1)):
                print(f"{name}: capped at <{cap.group(1)} in {fname}, latest is {newest}")


if __name__ == "__main__":
    main()
