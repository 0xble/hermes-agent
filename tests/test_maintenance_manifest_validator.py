from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from scripts.validate_maintenance_manifest import assess_manifest


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "validate_maintenance_manifest.py"


def _manifest(body: str) -> str:
    return "# Maintenance\n\n## Maintained patch index\n" + body + "\n## Patch records\n"


def test_inventory_accepts_retired_records_without_commit_subjects(tmp_path: Path) -> None:
    path = tmp_path / "MAINTENANCE.md"
    path.write_text(
        _manifest("| HERMES-1 | Retired | historical note |\n") + "\n### HERMES-1\n",
        encoding="utf-8",
    )
    assert assess_manifest(path) == []


def test_inventory_reports_structure_as_findings_not_a_gate(tmp_path: Path) -> None:
    path = tmp_path / "MAINTENANCE.md"
    path.write_text(_manifest("| HERMES-1 | Active | note |\n"), encoding="utf-8")
    completed = subprocess.run(
        [sys.executable, str(SCRIPT), str(path), "--json"],
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    payload = json.loads(completed.stdout)
    assert completed.returncode == 0
    assert payload == {
        "assessed": True,
        "errors": [],
        "findings": ["indexed patch has no record: HERMES-1"],
        "status": "findings",
    }


def test_unassessable_inventory_is_honest_and_nonblocking(tmp_path: Path) -> None:
    missing = tmp_path / 'missing.md'
    binary = tmp_path / 'binary.md'
    binary.write_bytes(b'\xff')
    link = tmp_path / 'link.md'
    link.symlink_to(binary)
    for path in (missing, binary, link):
        completed = subprocess.run([sys.executable, str(SCRIPT), str(path), '--json'],
                                   capture_output=True, text=True)
        assert completed.returncode == 0
        payload = json.loads(completed.stdout)
        assert payload['status'] == 'error'
        assert payload['assessed'] is False
        assert payload['errors']


def test_inventory_never_reads_or_enforces_git_history(tmp_path: Path) -> None:
    path = tmp_path / "MAINTENANCE.md"
    path.write_text(_manifest("| HERMES-1 | Retired | squash or merge subject |\n") + "\n### HERMES-1\n", encoding="utf-8")
    assert assess_manifest(path) == []
