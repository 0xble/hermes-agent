"""Persisted original-call display membership, never result or delivery membership.

Each dispatch stamps its complete birth roster before any child runs. Missing
members remain unresolved; a shared parent ID or transport is not a birth call.
"""
import copy
import re

from gateway import delegation_card_presentation as presentation
from gateway.delegation_card_presentation import _TERMINAL, _valid_number


def valid_manifest(manifest, key, ref):
    if not isinstance(manifest, dict):
        return False
    members = manifest.get("member_refs")
    return (isinstance(manifest.get("id"), str)
            and re.fullmatch(r"[a-f0-9]{32}", manifest["id"]) is not None
            and manifest.get("parent_task_id") == key
            and isinstance(members, list) and bool(members)
            and all(isinstance(r, str) and re.fullmatch(r"[A-Z]+", r) for r in members)
            and len(set(members)) == len(members) and ref in members)


def late_member(card, key, ref, manifest):
    """A first observation of an already declared child is not resurrection."""
    if ref in card["rows"] or not valid_manifest(manifest, key, ref):
        return False
    return card.get("original_calls", {}).get(manifest["id"], {}).get("manifest") == manifest


def register(manager, key, ref, manifest):
    card = manager.cards[key]
    row = card["rows"][ref]
    if manifest is None:
        return
    if not valid_manifest(manifest, key, ref):
        manager._warn_once(card, "Invalid delegation original-call display metadata")
        return
    calls = card.setdefault("original_calls", {})
    prior = calls.get(manifest["id"])
    if ((prior is not None and prior["manifest"] != manifest)
            or row.get("original_call_id") not in (None, manifest["id"])
            or any(other["manifest"]["id"] != manifest["id"]
                   and set(other["manifest"]["member_refs"]) & set(manifest["member_refs"])
                   for other in calls.values())):
        manager._warn_once(card, "Conflicting delegation original-call display metadata")
        return
    calls.setdefault(manifest["id"], {"manifest": copy.deepcopy(manifest)})
    row["original_call_id"] = manifest["id"]
    row.pop("display_expires_at", None)


def refresh(manager, key):
    """Derive only from retained current attempts, including handled members.

    Save callers own persistence. An unchanged closed window never re-reads the
    TTL; a validated new attempt opens it before another terminal transition.
    """
    card = manager.cards[key]
    for call in card.get("original_calls", {}).values():
        rows = [card["rows"].get(ref, {}) for ref in call["manifest"]["member_refs"]]
        complete = all(row.get("original_call_id") == call["manifest"]["id"]
                       and row.get("state") in _TERMINAL
                       and _valid_number(row.get("terminal_at")) for row in rows)
        if not complete:
            call.pop("all_terminal_at", None)
            call.pop("display_expires_at", None)
        elif not _valid_number(call.get("display_expires_at")):
            call["all_terminal_at"] = max(row["terminal_at"] for row in rows)
            call["display_expires_at"] = call["all_terminal_at"] + presentation.terminal_ttl_seconds(manager, key)


def display_fields(card, row):
    call = card.get("original_calls", {}).get(row.get("original_call_id"))
    if call is None:
        return {}
    return {"batch_display": True, "display_expires_at": call.get("display_expires_at")}
