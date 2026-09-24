"""Native updater receipt fixtures built through its real persistence boundary."""
import json
from unittest.mock import patch


def finalize_update(home, *, outcome="success", fleet_state="current", noop=False):
    """``noop`` mirrors "Already up to date": same revision, no restart, empty fleet."""
    from hermes_cli import update_receipt
    sha = "a" * 40
    with patch.object(update_receipt, "_receipt_dir", return_value=home / "logs" / "update_receipts"), \
         patch.object(update_receipt, "_code_identity", return_value={"sha": sha}):
        update_receipt.begin_update_receipt()
        if not noop:
            update_receipt.record_gateway_restart(restarted_services=["gateway"])
        path = update_receipt.finalize_update_receipt(outcome, fleet=[] if noop else [{
            "profile": "default", "pid": 1234, "state": fleet_state, "code_sha": sha,
        }])
    (home / ".update_process_exit_code").write_text("0")
    assert path is not None
    for receipt_path in (path, home / "logs" / "update_receipts" / "latest.json"):
        if receipt_path.exists():
            receipt_data = json.loads(receipt_path.read_text(encoding="utf-8"))
            receipt_data.setdefault("pre_update", {})["sha"] = sha if noop else "b" * 40
            receipt_path.write_text(json.dumps(receipt_data), encoding="utf-8")
    return path
