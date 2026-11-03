"""Export only the target fixture's retained bounded payload after the runner exits."""
from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / ".ci/composer-shard/artifacts"
FUNCTION = "test_classic_cli_matches_main_baseline_across_fifteen_steps"
# pytest's public tmp_path fixture uses the first 30 sanitized name characters,
# numbered from zero; the retained base is fresh and this file has one test.
REPORT = ROOT / ".ci/composer-shard/retained" / (FUNCTION[:30] + "0") / "composer-export.json"


def export():
    OUT.mkdir(parents=True, exist_ok=True)
    try:
        if REPORT.is_symlink() or REPORT.stat().st_size > 512000:
            raise ValueError("Refuse symlink or oversized fixture payload")
        data = json.loads(REPORT.read_text(encoding="utf-8"))
        if data.get("token") != "failure-boundary-10a52eee":
            raise ValueError("Wrong fixture token")
        if data.get("nodeid") != ("tests/e2e/core/cli_tmux/test_classic_cli_main_parity.py::" + FUNCTION):
            raise ValueError("Wrong fixture node")
        if data.get("export_error"):
            raise ValueError("Fixture reported export failure: " + data["export_error"])
        for name, record in data["files"].items():
            if Path(name).name != name or not name.endswith((".json", ".txt")):
                raise ValueError("Unexpected observation filename")
            (OUT / name).write_text(record["text"], encoding="utf-8")
        (OUT / "export.json").write_text(json.dumps(data, indent=2, ensure_ascii=True) + "\n")
        status = {"payload_available": True, "source": str(REPORT.relative_to(ROOT)),
                  "nodeid": data["nodeid"], "instruction": "Inspect this run; no automatic retry."}
    except Exception as error:
        status = {"payload_available": False, "error_type": type(error).__name__,
                  "instruction": "Inspect this run; no automatic retry."}
        (OUT / "export-status.json").write_text(json.dumps(status, indent=2) + "\n")
        raise
    (OUT / "export-status.json").write_text(json.dumps(status, indent=2) + "\n")


if __name__ == "__main__":
    export()
