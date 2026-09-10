"""Native updater receipt fixtures built through its real persistence boundary."""
from unittest.mock import patch


def finalize_update(home, *, outcome="success", fleet_state="current"):
    from hermes_cli import update_receipt
    sha = "a" * 40
    with patch.object(update_receipt, "_receipt_dir", return_value=home / "logs" / "update_receipts"), \
         patch.object(update_receipt, "_code_identity", return_value={"sha": sha}):
        update_receipt.begin_update_receipt()
        update_receipt.record_gateway_restart(restarted_services=["gateway"])
        path = update_receipt.finalize_update_receipt(outcome, fleet=[{
            "profile": "default", "pid": 1234, "state": fleet_state, "code_sha": sha,
        }])
    (home / ".update_process_exit_code").write_text("0")
    return path
