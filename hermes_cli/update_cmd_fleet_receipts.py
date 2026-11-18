"""Completion classification for fleet restart receipts."""

def _receipt_looks_unfinished(receipt: dict) -> bool:
    """True when *receipt* is from an update that did not finish cleanly.

    The command boundary stamps a ``stop_reason`` on every receipt, including clean
    ones (``completed at command boundary``, ``sys.exit(0)``); it must not make a
    successful receipt look unfinished, or the next ``hermes update`` retriggers
    ``fleet_restart_pending`` from pre-pull plan SHAs (#98022).
    """
    exit_code = receipt.get("exit_code")
    outcome = receipt.get("outcome")
    if exit_code not in (0, None) or outcome in ("failed", "partial", "running"):
        return True
    gateway_restart = receipt.get("gateway_restart")
    if isinstance(gateway_restart, dict) and gateway_restart.get("incomplete"):
        return True
    # A stop_reason alone (update_contract refusals: outcome="refused", no exit_code)
    # counts only when nothing else vouched for success.
    succeeded = exit_code == 0 or outcome == "success"
    return bool(receipt.get("stop_reason")) and not succeeded
