"""Presentation helpers for Telegram session-control approvals."""

from typing import Any, Dict


def session_control_outcome(record: Dict[str, Any], who: str) -> tuple[str, str]:
    """Return resolution text that explains stale approvals instead of hiding them."""
    status = str(record.get("status") or "failed")
    kind, action = str(record.get("kind") or "goal"), str(record.get("action") or "")
    if status == "applied":
        verb = {
            ("goal", "clear"): "Goal cleared",
            ("goal", "pause"): "Goal paused",
            ("goal", "resume"): "Goal resumed",
            ("goal", "replace"): "Goal replaced",
            ("loop", "pause"): "Loop paused",
            ("loop", "resume"): "Loop resumed",
            ("loop", "stop"): "Loop stopped",
        }.get((kind, action), "Approved")
        return f"✓ {verb} by {who}", ""
    if status == "denied":
        return f"• Kept as-is, denied by {who}", ""
    if status == "expired":
        return "⌛ Not applied", "This approval expired before it was resolved."
    if record.get("error") == "target_changed":
        return f"◆ Not applied by {who}", "The target changed before approval, so nothing was changed."
    error = str(record.get("error") or "unknown error")
    return f"✗ Not applied by {who}", f"Reason: {error}"
