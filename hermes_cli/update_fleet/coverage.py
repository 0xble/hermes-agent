"""Classify the gateway identities owed or covered by fleet restart evidence."""

from hermes_cli.update_cmd_fleet_gatewayless import runtime_outside_gateway_evidence

_SUPERVISED_SERVE_BACKENDS = frozenset(
    {"manual-serve", "desktop", "desktop-ssh", "systemd", "launchd", "windows-service", "service"}
)


def _receipt_owed_gateways(receipt: dict, pending_manual: list[dict]) -> set[tuple[str, str]] | None:
    """Pure coverage classification after manual retention of this receipt snapshot.

    Empty means this receipt owes no gateways, not that an independent marker owes none. Unknown identities, unclassified serve backends and failed manual transfers make coverage unverified.
    """
    plan = receipt.get("plan") or {}
    entries: list[tuple[object, str | None]] = [(entry, None) for entry in plan.get("runtimes") or []]
    entries.extend((entry, None) for entry in receipt.get("pending_manual_serves") or [])
    entries.extend((entry, None) for entry in receipt.get("carried_manual_serves") or [])
    entries.extend((entry, "gateway") for entry in receipt.get("fleet") or [])
    owed: set[tuple[str, str]] = set()
    unverified = False
    for entry, default_kind in entries:
        if not isinstance(entry, dict):
            unverified = True
            continue
        kind = entry.get("kind", default_kind)
        profile = entry.get("profile")
        # A serve/dashboard row is outside the gateway matrix's evidence, not evidence against the
        # gateways it does cover: a supervised backend (desktop, systemd, launchd) is its
        # supervisor's to restart, and a manual-serve row outside the retention list is the serve
        # obligation mechanism's — a host running a dashboard carries such a row in every receipt,
        # and a blanket veto made the gateway warning permanently undischargeable there (#115090).
        # Only an unclassified backend or a failed manual transfer still makes coverage unverified.
        if kind in ("serve", "dashboard") and entry.get("supervisor") in _SUPERVISED_SERVE_BACKENDS and entry not in pending_manual:
            continue
        if kind != "gateway" or not profile or profile == "unknown":
            unverified = True
            continue
        owed.add((kind, profile))
    return None if unverified else owed


def _fleet_covered_gateways(fleet: list) -> set[tuple[str, str]] | None:
    """``(kind, profile)`` identities the live rows vouch for; ``None`` when any row is unidentified.

    A multiplexer's row carries the ``served_profiles`` its runtime status records (``_fleet_row``
    keeps the field only when well-formed), so one live process covers every profile it serves.
    """
    covered: set[tuple[str, str]] = set()
    for row in fleet:
        profile = row.get("profile") if isinstance(row, dict) else None
        if not profile or profile == "unknown":
            return None  # unidentified runtime: the matrix cannot vouch for it
        covered.add(("gateway", profile))
        covered.update(("gateway", served) for served in row.get("served_profiles") or [])
    return covered


# A gateway the restart phase stopped without knowing its profile: no live row can be matched to it,
# so only a live, current gateway of this checkout settles it, never an empty host (""=no profile).
_UNMAPPED_GATEWAY = ("gateway", "")


def _marker_owed_gateways(inventory: object) -> set[tuple[str, str]] | None:
    """The ``("gateway", profile)`` set a marker's inventory owes; None when it recorded none.

    Raises ValueError for a malformed or unsupported inventory, which keeps the marker.
    """
    if inventory is None:
        return None
    if not isinstance(inventory, dict) or inventory.get("version") != 1:
        raise ValueError("unsupported fleet-restart inventory")
    runtimes = inventory.get("runtimes")
    if not isinstance(runtimes, list):
        raise ValueError("fleet-restart inventory has no runtime list")
    owed: set[tuple[str, str]] = set()
    for runtime in runtimes:
        if not isinstance(runtime, dict):
            raise ValueError("fleet-restart inventory row is not an object")
        if runtime_outside_gateway_evidence(runtime):
            continue
        if runtime.get("kind") == "gateway" and runtime.get("stopped_unmapped"):
            owed.add(_UNMAPPED_GATEWAY)
            continue
        profile = runtime.get("profile")
        if runtime.get("kind") != "gateway" or not isinstance(profile, str) or not profile.strip() or profile == "unknown":
            raise ValueError("fleet-restart inventory row is not an identified gateway")
        owed.add(("gateway", profile))
    return owed
