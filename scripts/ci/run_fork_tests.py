#!/usr/bin/env python3
"""Bounded fork proof surfaces, plus changed tests. Unknown code runs the full suite.

The manifest covers each maintenance unit. This is a hosted smoke gate, not a
claim of complete-suite coverage. Use scripts/run_tests.sh tests candidate-extensions for the complete gate.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess

ROOT = Path(__file__).resolve().parents[2]
MANIFEST = Path(__file__).with_name("fork_test_surfaces.json")


def select_tests(changed: list[str], surfaces: dict[str, list[str]]) -> list[str] | None:
    """None means full discovery, including missing/unknown change context."""
    if not changed:
        return None
    selected = {test for tests in surfaces.values() for test in tests}
    for path in changed:
        if path.startswith("tests/") and path.endswith(".py"):
            # Shared fixtures and deleted tests require full discovery.
            if Path(path).name == "conftest.py" or not (ROOT / path).is_file():
                return None
            selected.add(path)
        elif path.startswith(("maintenance/", "website/", "docs/")) or path.endswith(".md"):
            continue
        elif path.startswith("tests/conformance/vectors/"):
            selected.add("tests/conformance/test_vector_generator.py")
        else:
            # Do not guess the dependency fan-out of production, packaging,
            # test harness, workflow, or other unclassified changes.
            return None
    return sorted(selected)


def main() -> int:
    event = json.loads(Path(os.environ["GITHUB_EVENT_PATH"]).read_text(encoding="utf-8"))
    base = event.get("pull_request", {}).get("base", {}).get("sha") or event.get("before")
    head = event.get("pull_request", {}).get("head", {}).get("sha") or os.environ["GITHUB_SHA"]
    changed = []
    if base and set(base) != {"0"}:
        revision = f"{base}...{head}" if event.get("pull_request") else f"{base}..{head}"
        diff = subprocess.run(["git", "diff", "--name-only", revision], cwd=ROOT, capture_output=True, text=True, encoding="utf-8", errors="replace")
        if diff.returncode == 0:
            changed = diff.stdout.splitlines()
    surfaces = json.loads(MANIFEST.read_text(encoding="utf-8"))
    for unit, tests in surfaces.items():
        if not tests or any(not (ROOT / test).is_file() for test in tests):
            raise ValueError(f"Missing fork test surface for {unit}")
    manual_smoke = (
        os.environ.get("GITHUB_EVENT_NAME") == "workflow_dispatch"
        and str(event.get("inputs", {}).get("full_python", True)).lower() == "false"
    )
    selected = (
        sorted({test for tests in surfaces.values() for test in tests})
        if manual_smoke else select_tests(changed, surfaces)
    )
    print(f"Fork selection: {len(selected)} files" if selected else "Unclassified change: running full Python suite", flush=True)
    return subprocess.call(["bash", "scripts/run_tests.sh", *(selected if selected is not None else ["tests", "candidate-extensions"])], cwd=ROOT)


if __name__ == "__main__":
    raise SystemExit(main())
