"""Canonical topic presentation, separate from immutable execution ownership.

All functions run under the manager's normalized conversation lock. Cleanup is
write-ahead: link original records, publish the union, then delete exact receipts.
"""


import json
import math


_TERMINAL = {"completed", "failed", "error", "timeout", "cancelled", "interrupted", "budget_exhausted"}


def row_order(row):
    """Immutable admission order; legacy/invalid metadata sorts before new rows."""
    value = row.get("presentation_order")
    return value if type(value) is int and value > 0 else 0


def next_row_order(manager, key):
    # Called under the conversation lock, only when admitting a new identity.
    # Include retired records so later groups never reuse their chronology.
    target_scope = manager._scope(manager.cards[key])
    return 1 + max((row_order(row) for card in manager.cards.values()
                    if manager._scope(card) == target_scope
                    for row in card["rows"].values()), default=0)


def render(manager, key, projection):
    """Render the same cap-first eligible view used by transport and expiry."""
    from gateway.delegation_cards import render_card

    cap = max_visible_roots(manager, key)
    selected = select_rows(projection, now=manager._now(), max_visible_roots=cap)
    return render_card(projection, max_visible_roots=cap, selected_identities=selected)


def _display_setting(manager, key, name, default):
    from gateway.display_config import resolve_display_setting
    from gateway.run import _load_gateway_config

    card = manager.cards[key]
    source = manager._source(card)
    if source is None:
        return default
    source.profile = source.profile or card["owner"].get("profile")
    home = manager.path.parents[2]
    resolver = getattr(manager.runner, "_resolve_profile_home_for_source", None)
    try:
        if resolver is not None:
            home = resolver(source)
        config = _load_gateway_config(home / "config.yaml")
        return resolve_display_setting(config, source.platform.value, name, default)
    except Exception:
        return default  # never borrow another profile after resolution failure


def terminal_ttl_seconds(manager, key):
    return _display_setting(manager, key, "delegation_terminal_ttl_seconds", 300)


def max_visible_roots(manager, key):
    return _display_setting(manager, key, "delegation_max_visible_roots", 5)


def _valid_number(value):
    return type(value) in (int, float) and not isinstance(value, bool) and math.isfinite(value)


def select_rows(projection, *, now, max_visible_roots=5):
    """Return the display rows after cap-first selection and terminal TTL pruning.

    The immutable projection remains untouched. Root selection happens before expiry pruning so an
    expired older root can never cause a previously hidden root to be backfilled into the window.
    Active descendants retain their terminal ancestors as context.
    """
    rows = dict(sorted((projection.get("rows") or {}).items(), key=lambda item: row_order(item[1])))
    identities = set(rows)
    children = {}
    for identity, row in rows.items():
        parent = row.get("card_parent_identity")
        if parent in identities and parent != identity:
            children.setdefault(parent, []).append(identity)
    roots = []
    root_for = {}
    visited = set()

    def walk(identity, root):
        if identity in visited:
            return
        visited.add(identity)
        root_for[identity] = root
        for child in children.get(identity, ()):
            walk(child, root)

    for identity, row in rows.items():
        parent = row.get("card_parent_identity")
        if parent not in identities:
            roots.append(identity)
            walk(identity, identity)
    for identity in rows:
        if identity not in visited:
            roots.append(identity)
            walk(identity, identity)

    cap = max_visible_roots if type(max_visible_roots) is int and max_visible_roots > 0 else 5
    selected_roots = set(roots[-cap:])
    selected = {identity for identity in rows if root_for.get(identity) in selected_roots}
    for identity in tuple(selected):
        row = rows[identity]
        if row.get("state") not in _TERMINAL:
            continue
        if row.get("batch_display") and row.get("display_expires_at") is None:
            continue  # complete birth roster has not reached all-terminal
        terminal_at = row.get("terminal_at")
        expires_at = row.get("display_expires_at")
        # Legacy terminal rows without a trustworthy timestamp are immediately hidden. Do not
        # invent a historical time, and do not mutate the durable projection from presentation.
        if not _valid_number(terminal_at) or not _valid_number(expires_at) or now >= expires_at:
            selected.discard(identity)

    # Preserve every ancestor needed to explain an eligible descendant, including an expired
    # terminal ancestor. This is display context only; it does not revive the ancestor itself.
    for identity in tuple(selected):
        parent = rows[identity].get("card_parent_identity")
        seen = {identity}
        while parent in rows and parent not in seen:
            selected.add(parent)
            seen.add(parent)
            parent = rows[parent].get("card_parent_identity")
    return selected


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
    return any((c.get("reanchor") or {}).get("state") in {"attempting", "sending", "deleting", "delete_pending", "deleted"}
               or (not c.get("retired") and not c.get("message_id") and c.get("send_attempts", 0)
                   and not c.get("consolidated_message_id") and not c.get("message_deleted"))
               for _, c in manager._members(key))


def bind(manager, key):
    card = manager.cards[key]
    if card.get("retired"):
        return
    # Handled executions can still own a live batch display window. Join their
    # transport without undoing the independent lifecycle retirement.
    active = [(k, c) for k, c in manager.cards.items()
              if (not c.get("retired") or (c.get("original_calls") and not c.get("message_deleted")))
              and manager._scope(c) == manager._scope(card)]
    # Existing cleanup chooses a stable survivor until its exact ledger finishes.
    # Otherwise prefer a real transport receipt, then the oldest task. Never
    # compare message numbers as chronology or merge execution/receipt owners.
    roots = {manager._anchor(k) for k, _ in active}
    # A gap/lost receipt is still owned even if its execution retired. A later
    # task in the topic must not create a second transport around that fence.
    roots.update(k for k, c in manager.cards.items()
                 if manager._scope(c) == manager._scope(card)
                 and ((c.get("reanchor") or {}).get("order") == "delete_first"
                      or (c.get("reanchor") or {}).get("state") == "attempting"))
    # A retired execution can still hold the transport for live siblings. Keep
    # that receipt, but never reuse a deleted/ambiguous historical anchor.
    candidates = active + [(k, manager.cards[k]) for k in roots
                           if k in manager.cards and manager.cards[k].get("retired")
                           and (manager.cards[k].get("message_id") or manager.cards[k].get("reanchor"))
                           and manager._scope(manager.cards[k]) == manager._scope(card)]
    inflight = manager._inflight.get(manager._scope(card))
    if inflight and all(k != inflight for k, _ in candidates):
        candidates.append((inflight, manager.cards[inflight]))
    anchor, target = min(candidates, key=lambda item: (
        item[0] != inflight if inflight else False,
        (item[1].get("reanchor") or {}).get("order") != "delete_first",
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
