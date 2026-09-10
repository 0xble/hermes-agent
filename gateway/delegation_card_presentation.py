"""Canonical topic presentation, separate from immutable execution ownership.

All functions run under the manager's normalized conversation lock. Cleanup is
write-ahead: link original records, publish the union, then delete exact receipts.
"""


import json


def cleanup_allowed(manager, key, message_id):
    """Optional operator scope guard; malformed/unreadable policy denies cleanup.

    The policy only narrows exact IDs already present in the write-ahead ledger;
    it cannot authorize arbitrary deletion, retirement or send replacement.
    Read at the deletion boundary so removing an approval takes effect promptly.
    """
    path = manager.path.with_name("presentation-cleanup-policy.json")
    try:
        if path.is_symlink():
            return False
        policy = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return True  # Ordinary automatic lifecycle; no scoped rollout requested.
    except (OSError, ValueError):
        return False
    fields = ("profile", "platform", "chat_id", "thread_id", "message_id")
    if (not isinstance(policy, dict) or type(policy.get("version")) is not int
            or policy["version"] != 1
            or not isinstance(policy.get("allow"), list)
            or any(not isinstance(entry, dict) or set(entry) != set(fields)
                   or any(not isinstance(entry[f], str) for f in fields)
                   for entry in policy["allow"])):
        return False
    owner_profile, *route = manager._scope(manager.cards[key])
    target = (*route, str(message_id))
    return owner_profile == route[0] and any(
        tuple(entry[f] for f in fields) == target for entry in policy["allow"])


def scope(owner, source):
    # SessionSource.profile=None means the event's owning profile, not a second
    # presentation namespace. Keep explicit conflicting routes isolated.
    profile = owner.get("profile") or source.get("profile") or "default"
    return (profile, source.get("profile") or profile, source.get("platform"),
            str(source.get("chat_id")), str(source.get("thread_id") or ""))


def pending(manager, key):
    return [entry for entry in manager.cards[key].get("presentation_cleanup", [])
            if entry["state"] != "deleted"]


def fenced(manager, key):
    # A lost receipt remains a fence even after its task is linked to a survivor.
    return any((c.get("reanchor") or {}).get("state") == "attempting"
               or (not c.get("message_id") and c.get("send_attempts", 0)
                   and not c.get("consolidated_message_id") and not c.get("message_deleted"))
               for _, c in manager._members(key) if not c.get("retired"))


def bind(manager, key):
    card = manager.cards[key]
    if card.get("retired"):
        return
    active = [(k, c) for k, c in manager.cards.items()
              if not c.get("retired") and manager._scope(c) == manager._scope(card)]
    # Existing cleanup chooses a stable survivor until its exact ledger finishes.
    # Otherwise prefer a real transport receipt, then the oldest task. Never
    # compare message numbers as chronology or merge execution/receipt owners.
    roots = {manager._anchor(k) for k, _ in active}
    # A retired execution can still hold the transport for live siblings. Keep
    # that receipt, but never reuse a deleted/ambiguous historical anchor.
    candidates = active + [(k, manager.cards[k]) for k in roots
                           if k in manager.cards and manager.cards[k].get("retired")
                           and manager.cards[k].get("message_id")
                           and manager._scope(manager.cards[k]) == manager._scope(card)]
    anchor, target = min(candidates, key=lambda item: (
        not bool(item[1].get("presentation_cleanup")),
        not bool(item[1].get("message_id")), item[1]["started_at"], item[0]))
    members = [(k, c) for k, c in manager.cards.items()
               if manager._scope(c) == manager._scope(card)
               and (k in roots or manager._anchor(k) in roots or not c.get("retired"))]
    changed = False
    ledger = target.setdefault("presentation_cleanup", [])
    for old_key, old in members:
        if old_key != anchor:
            for message_id in (old.get("message_id"), old.get("obsolete_message_id")):
                if message_id and message_id != target.get("message_id"):
                    if not any(e["message_id"] == message_id for e in ledger):
                        ledger.append(dict(task_key=old_key, message_id=message_id,
                                           state="pending", attempts=0))
                        changed = True
            for entry in old.pop("presentation_cleanup", []):
                if not any(e["message_id"] == entry["message_id"] for e in ledger):
                    ledger.append({**entry, "state": "pending"} if entry["state"] != "deleted" else entry)
                    changed = True
            if old.get("message_id"):
                old["consolidated_message_id"] = old["message_id"]
                old["message_id"] = None
            old.pop("obsolete_message_id", None)
        if old.get("presentation_key") != anchor:
            old["presentation_key"] = anchor
            changed = True
    if changed:
        target["revision"] = target.get("revision", 0) + 1
        # Cached text is not a receipt for the newly unioned projection.
        target["rendered"] = ""
        for entry in ledger:
            if entry["state"] != "deleted":
                entry["state"] = "pending"
        manager._save()
    for member, _ in members:
        manager._assign_refs(member)


def published(manager, key):
    card = manager.cards[key]
    for entry in pending(manager, key):
        entry["state"] = "ready"
        entry["survivor_message_id"] = card.get("message_id")
    manager._save()  # durable successful union update precedes every extra delete
