"""Guardrails for an automated maintenance change, checked against the staged
diff (``git diff --cached``) relative to a base ref. Run from a PRISTINE copy
of this file (the change under review must not be able to edit its own judge).

Refuses the change if it:
  - touches the pipeline, deploy tooling, or protected login tests
  - deletes any test file
  - reduces the number of test functions
  - adds any skip / xfail
  - introduces an em dash anywhere

Usage: python guard.py <base-ref>      (exit 1 on violation)
"""

from __future__ import annotations

import re
import subprocess
import sys

# Security-critical tests the agent must never edit (they gate login).
PROTECTED = (".github/", "ops/", "tests/e2e/test_auth.py", "tests/conftest.py")
EM_DASH = chr(0x2014)  # written as a code point so this file never contains one


def git(*args: str) -> str:
    return subprocess.run(["git", *args], capture_output=True, text=True,
                          encoding="utf-8", check=True).stdout


def count(pattern: str, ref: str | None) -> int:
    """Occurrences of a regex across tests/ at ``ref`` (None = staged index)."""
    files = git("ls-files", "tests") if ref is None else git("ls-tree", "-r", "--name-only", ref, "tests")
    total = 0
    for f in files.split():
        if not f.endswith(".py"):
            continue
        src = git("show", f":{f}") if ref is None else git("show", f"{ref}:{f}")
        total += len(re.findall(pattern, src, re.M))
    return total


def main() -> int:
    base = sys.argv[1]
    problems: list[str] = []
    for line in git("diff", "--cached", "--name-status", base).splitlines():
        status, *paths = line.split("\t")
        for p in paths:
            if p.startswith(PROTECTED):
                problems.append(f"touches protected path {p}")
        if status.startswith("D") and paths[0].startswith("tests/"):
            problems.append(f"deletes test file {paths[0]}")

    before, after = count(r"^\s*def test_", base), count(r"^\s*def test_", None)
    if after < before:
        problems.append(f"test functions dropped from {before} to {after}")
    skip_re = r"pytest\.mark\.(skip|xfail)|pytest\.(skip|xfail)\("
    sb, sa = count(skip_re, base), count(skip_re, None)
    if sa > sb:
        problems.append(f"skip/xfail markers increased from {sb} to {sa}")

    added = [l for l in git("diff", "--cached", base).splitlines()
             if l.startswith("+") and not l.startswith("+++")]
    if any(EM_DASH in l for l in added):
        problems.append("introduces an em dash")

    if problems:
        print("GUARD REJECTED the maintenance change:")
        for p in problems:
            print(f"  - {p}")
        return 1
    print(f"guard ok: {after} test functions (was {before}), no protected paths touched")
    return 0


if __name__ == "__main__":
    sys.exit(main())
