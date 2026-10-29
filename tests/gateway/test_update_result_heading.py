import json
from pathlib import Path

from gateway.run_notifications import GatewayNotificationsMixin


def test_update_result_heading_distinguishes_noop_and_change(tmp_path: Path) -> None:
    receipt_dir = tmp_path / "logs" / "update_receipts"
    receipt_dir.mkdir(parents=True)
    receipt = {
        "pre_update": {"sha": "a" * 40},
        "post_update": {"sha": "a" * 40},
    }
    (receipt_dir / "latest.json").write_text(json.dumps(receipt))

    heading, detail = GatewayNotificationsMixin._update_result_heading(tmp_path, {}, 0)

    assert heading == "ℹ️ Already Latest"
    assert "No changes were applied" in detail
    assert "gateway was not restarted" in detail


def test_update_result_heading_reports_changed_revision(tmp_path: Path) -> None:
    receipt_dir = tmp_path / "logs" / "update_receipts"
    receipt_dir.mkdir(parents=True)
    (receipt_dir / "latest.json").write_text(json.dumps({
        "pre_update": {"sha": "a" * 40},
        "post_update": {"sha": "b" * 40},
    }))

    heading, detail = GatewayNotificationsMixin._update_result_heading(tmp_path, {}, 0)

    assert heading == "✅ Update Complete"
    assert "b" * 12 in detail


def test_update_result_heading_reports_process_failure(tmp_path: Path) -> None:
    heading, detail = GatewayNotificationsMixin._update_result_heading(tmp_path, {}, 7)

    assert heading == "❌ Update Failed"
    assert "code 7" in detail


def test_same_revision_with_verified_restart_is_not_reported_as_noop(tmp_path: Path) -> None:
    """Checkout repair and fleet catch-up keep the SHA but restart the gateway."""
    receipt_dir = tmp_path / "logs" / "update_receipts"
    receipt_dir.mkdir(parents=True)
    (receipt_dir / "latest.json").write_text(json.dumps({
        "pre_update": {"sha": "a" * 40},
        "post_update": {"sha": "a" * 40},
        "gateway_restart": {"restarted_services": ["gateway"]},
        "fleet": [{"profile": "default", "state": "current", "code_sha": "a" * 40}],
    }))

    heading, detail = GatewayNotificationsMixin._update_result_heading(tmp_path, {}, 0)

    assert heading == "✅ Update Complete"
    assert "not restarted" not in detail
    assert "restarted" in detail and "a" * 12 in detail
