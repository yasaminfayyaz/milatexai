#!/usr/bin/env python3
"""Pull the AI's structured answer out of the action's execution file and write it to disk.

It is read from the file, not from a step output, because GitHub prints step inputs in the
run log and this repository is public. Writes an empty file when there is no usable answer;
the executor treats that as "no decision" and changes nothing.

Usage: python save_decision.py <execution_file> <out.json>
"""

from __future__ import annotations

import json
import sys


def find_structured(doc):
    """The structured answer, from either a list of messages or a single result object."""
    items = doc if isinstance(doc, list) else [doc]
    for item in reversed(items):
        if isinstance(item, dict):
            value = item.get("structured_output")
            if isinstance(value, str):
                try:
                    value = json.loads(value)
                except ValueError:
                    value = None
            if isinstance(value, dict):
                return value
    return None


def main() -> None:
    src, dst = sys.argv[1], sys.argv[2]
    found = None
    try:
        with open(src, encoding="utf-8") as fh:
            found = find_structured(json.load(fh))
    except (OSError, ValueError):
        pass
    with open(dst, "w", encoding="utf-8") as fh:
        if found is not None:
            json.dump(found, fh)
    print("decision saved" if found is not None else "no structured answer found")


if __name__ == "__main__":
    main()
