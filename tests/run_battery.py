"""Standalone runner: load the tool_bibtex.html engine into QuickJS and execute a
battery file against it. Prints the battery's JSON result.

Usage:
    python tests/run_battery.py path/to/battery.js

The battery file must be a self-contained IIFE that references window.BIB and
returns a JSON string {pass, fail, total, failures}. See bibtex_battery.js.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

import quickjs

ROOT = Path(__file__).resolve().parent.parent
HTML = (ROOT / "leafbridge" / "tool_bibtex.html").read_text(encoding="utf-8")


def engine_js() -> str:
    m = re.search(r"<script>(.*?)</script>", HTML, re.S)
    if not m:
        raise SystemExit("no <script> block in the tool")
    js = m.group(1)
    ui = js.find("/* ===================== UI")
    if ui <= 0:
        raise SystemExit("UI marker not found - engine/UI split changed")
    return "var window = {};\n" + js[:ui]


def run(battery_path: str) -> str:
    battery = Path(battery_path).read_text(encoding="utf-8")
    ctx = quickjs.Context()
    ctx.eval(engine_js())
    return ctx.eval(battery)


if __name__ == "__main__":
    if len(sys.argv) != 2:
        raise SystemExit("usage: python tests/run_battery.py <battery.js>")
    print(run(sys.argv[1]))
