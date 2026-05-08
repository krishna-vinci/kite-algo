#!/usr/bin/env python3
from __future__ import annotations

import pathlib
import re


ROOT = pathlib.Path(__file__).resolve().parents[1]
PYPROJECT = ROOT / "sdk" / "python" / "pyproject.toml"
TARGETS = [
    ROOT / "README.md",
    ROOT / "sdk" / "python" / "README.md",
    ROOT / "documents" / "algo-worker-sdk-guide.md",
    ROOT / "agent-context" / "algo-worker-developer" / "README.md",
    ROOT / "agent-context" / "algo-worker-developer" / "ALGO_WORKER_DEVELOPMENT_GUIDE.md",
]

VERSION_RE = re.compile(r'^version\s*=\s*"([^"]+)"', re.MULTILINE)
PIP_RE = re.compile(r'kite-algo-worker==([0-9]+\.[0-9]+\.[0-9]+)')
TAG_RE = re.compile(r'kite-algo-worker-v([0-9]+\.[0-9]+\.[0-9]+)')


def main() -> int:
    text = PYPROJECT.read_text(encoding="utf-8")
    match = VERSION_RE.search(text)
    if match is None:
        raise SystemExit(f"Could not determine SDK version from {PYPROJECT}")
    expected = match.group(1)

    errors: list[str] = []
    for path in TARGETS:
        target_text = path.read_text(encoding="utf-8")
        for actual in PIP_RE.findall(target_text):
            if actual != expected:
                errors.append(f"{path}: install version {actual} != {expected}")
        for actual in TAG_RE.findall(target_text):
            if actual != expected:
                errors.append(f"{path}: tag version {actual} != {expected}")
        for line_no, line in enumerate(target_text.splitlines(), start=1):
            normalized = line.strip()
            if "pip install" in normalized and "kite-algo-worker" in normalized and "==" not in normalized:
                errors.append(f"{path}:{line_no}: found unversioned or ranged install snippet: {normalized}")

    if errors:
        print("\n".join(errors))
        raise SystemExit(1)

    print(f"All worker SDK version references match {expected}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
