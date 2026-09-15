from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

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


def _split_contract(tmp_path: Path) -> tuple[Path, Path, Path]:
    root = tmp_path / "MAINTENANCE.md"
    support = tmp_path / "maintenance"
    support.mkdir()
    first, second = support / "delivery.md", support / "state.md"
    root.write_text(
        "# Fork\n\n## Background\nAccepted upstream revision.\n\n"
        "- **Required every run:** [Delivery](maintenance/delivery.md).\n"
        "- **Required when state changes:** [State](maintenance/state.md).\n",
        encoding="utf-8",
    )
    first.write_text(_manifest("| HERMES-1 | Active |\n") + "### HERMES-1\n", encoding="utf-8")
    second.write_text(_manifest("| HERMES-2 | Retired |\n") + "### HERMES-2\n", encoding="utf-8")
    return root, first, second


def test_required_closure_assesses_every_module_and_cross_module_ids(tmp_path: Path) -> None:
    root, first, second = _split_contract(tmp_path)
    assert assess_manifest(root) == []
    # The first page passes; only reading the complete closure finds the omission.
    second.write_text(_manifest("| HERMES-2 | Retired |\n"), encoding="utf-8")
    assert "indexed patch has no record: HERMES-2" in assess_manifest(root)
    second.write_text(first.read_text(encoding="utf-8"), encoding="utf-8")
    assert assess_manifest(root) == [
        "duplicate patch index ID: HERMES-1", "duplicate patch record ID: HERMES-1",
    ]


@pytest.mark.parametrize("defect, expected", [
    ("missing", "error"),
    ("binary", "error"),
    ("symlink", "error"),
    ("directory-symlink", "error"),
    ("escape", "error"),
    ("unlinked", "findings"),
    ("no-trigger", "findings"),
    ("no-background", "findings"),
    ("duplicate-background", "findings"),
    ("nested", "findings"),
    ("enrolled-support", "findings"),
    ("missing-record-section", "findings"),
])
def test_incomplete_or_unsafe_closure_is_honest_but_never_a_gate(
    tmp_path: Path, defect: str, expected: str,
) -> None:
    root, first, second = _split_contract(tmp_path)
    if defect == "missing":
        second.unlink()
    elif defect == "binary":
        second.write_bytes(b"\xff")
    elif defect == "symlink":
        second.unlink()
        second.symlink_to(first)
    elif defect == "directory-symlink":
        moved = tmp_path / "elsewhere"
        first.parent.rename(moved)
        first.parent.symlink_to(moved, target_is_directory=True)
    elif defect == "escape":
        root.write_text(root.read_text().replace("maintenance/state.md", "maintenance/../outside.md"))
    elif defect == "unlinked":
        (first.parent / "unlinked.md").write_text(first.read_text())
    elif defect == "no-trigger":
        root.write_text(root.read_text().replace("Required when state changes", "Optional reference"))
    elif defect == "no-background":
        root.write_text(root.read_text().replace("## Background", "## Origins"))
    elif defect == "duplicate-background":
        root.write_text(root.read_text() + "\n## Background\n")
    elif defect == "nested":
        (first.parent / "nested").mkdir()
        (first.parent / "nested" / "part.md").write_text(first.read_text())
    elif defect == "enrolled-support":
        (first.parent / "MAINTENANCE.md").write_text(first.read_text())
    elif defect == "missing-record-section":
        second.write_text("# State\n")
    completed = subprocess.run(
        [sys.executable, str(SCRIPT), str(root), "--json"],
        capture_output=True, text=True, encoding="utf-8", check=False,
    )
    payload = json.loads(completed.stdout)
    assert completed.returncode == 0
    assert payload["status"] == expected
    assert payload["assessed"] is (expected != "error")
    assert payload["errors"] if expected == "error" else payload["findings"]
