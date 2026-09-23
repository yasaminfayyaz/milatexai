#!/usr/bin/env bash
# Mechanical, deterministic upgrade step of the weekly maintenance run:
#   - re-resolve both lockfiles to the newest versions the ranges allow
#   - bump the pinned Tectonic / latexdiff versions in the Dockerfile
# Writes a human summary to .maint/changes.txt. No judgment calls happen here;
# the agent only steps in afterwards if this broke something.
set -euo pipefail
cd "$(dirname "$0")/../.."
mkdir -p .maint
UVFLAGS=(--python-version 3.12 --python-platform x86_64-manylinux_2_28 --generate-hashes --quiet)

cp requirements.txt .maint/requirements.before.txt
uv pip compile requirements.in "${UVFLAGS[@]}" --upgrade -o requirements.txt
uv pip compile requirements-dev.in "${UVFLAGS[@]}" --upgrade -o requirements-dev.txt

python ops/maint/bump_tools.py >> .maint/changes.txt

python - <<'PY' >> .maint/changes.txt
import re
def pins(path):
    out = {}
    for line in open(path, encoding="utf-8"):
        m = re.match(r"^([A-Za-z0-9_.\-]+)==([^\s\\;]+)", line)
        if m:
            out[m.group(1).lower()] = m.group(2)
    return out
before, after = pins(".maint/requirements.before.txt"), pins("requirements.txt")
changed = [(n, before.get(n), after.get(n)) for n in sorted(set(before) | set(after))
           if before.get(n) != after.get(n)]
print(f"Python packages changed: {len(changed)}")
for n, b, a in changed:
    print(f"  {n}: {b or '(new)'} -> {a or '(removed)'}")
PY
cat .maint/changes.txt
