"""Audited presentation-only retirement, independent of durable task outcomes."""
import copy
from datetime import datetime, timezone
import hashlib
import json
import re

_INACTIVE = {"completed", "failed", "error", "timeout", "cancelled", "interrupted", "budget_exhausted", "unknown"}
_SCHEMA = "delegation-card-dismissal-v1"


def _text(value):
    return isinstance(value, str) and bool(value.strip())


def prepare(raw: bytes, manifest: dict) -> dict:
    """Validate the whole batch before producing a candidate. Manifest is operator input.

    The hash pins *all* snapshot bytes, including per-card generation, owner, source,
    row state and transport identity. Explicit identities additionally make the
    intended scope reviewable. Evidence strings are audit pointers, not authority
    inferred by this program. Its invocation requires separate operator approval.
    """
    digest = hashlib.sha256(raw).hexdigest()
    if (not isinstance(manifest, dict) or manifest.get("schema") != _SCHEMA
            or manifest.get("snapshot_sha256") != digest
            or not all(_text(manifest.get(field)) for field in ("operator", "authorization"))):
        raise ValueError("invalid manifest, missing operator authorization, or snapshot hash mismatch")
    cards = json.loads(raw)
    targets = manifest.get("targets")
    if not isinstance(cards, dict) or not isinstance(targets, list) or not targets:
        raise ValueError("expected cards object and nonempty exact target list")
    seen = set()
    for target in targets:
        if not isinstance(target, dict):
            raise ValueError("target must be an object")
        key = target.get("parent_task_id")
        if not isinstance(key, str) or not re.fullmatch(r"[a-f0-9]{32}", key) or key in seen:
            raise ValueError("invalid or duplicate parent_task_id")
        seen.add(key)
        card = cards.get(key)
        if not isinstance(card, dict) or card.get("retired") or card.get("presentation_dismissal"):
            raise ValueError("target missing or already retired/dismissed")
        rows = card.get("rows")
        refs = target.get("refs")
        if (not isinstance(rows, dict) or not rows or not isinstance(refs, list)
                or not all(isinstance(ref, str) and re.fullmatch(r"[A-Z]+", ref) for ref in refs)
                or len(set(refs)) != len(refs) or set(refs) != set(rows)
                or any(not isinstance(row, dict) or row.get("thread_ref") != ref
                       or row.get("state") not in _INACTIVE for ref, row in rows.items())):
            raise ValueError("target must name every exact inactive row; active/partial cards cannot be dismissed")
        for field in ("owner", "source"):
            if not isinstance(target.get(field), dict) or not target[field] or target[field] != card.get(field):
                raise ValueError(f"{field} identity mismatch")
        if "message_id" not in target or target["message_id"] != card.get("message_id"):
            raise ValueError("message identity mismatch")
        if target.get("reason") not in {"superseded", "dismissed"} or not _text(target.get("evidence")):
            raise ValueError("explicit presentation reason and per-target evidence required")
    result = copy.deepcopy(cards)
    recorded_at = datetime.now(timezone.utc).isoformat()
    for target in targets:
        card = result[target["parent_task_id"]]
        card["presentation_dismissal"] = {
            "schema": _SCHEMA, "recorded_at": recorded_at,
            "snapshot_sha256": digest, "operator": manifest["operator"],
            "authorization": manifest["authorization"], "target": copy.deepcopy(target),
        }
        # Existing persisted retirement fence suppresses replay and late callbacks.
        # Do NOT change handled, row states, child execution or completion receipts.
        card["retired"] = True
    return result


def apply_pending(manager):
    """Consume an explicit operator request at startup, before recovery mutation.

    Unlike replacing cards.json offline, this preserves concurrently changed
    *unrelated* cards and the anchor's rendered/revision fields. All task identity,
    row state, handled proof and message identity must still match the audited
    snapshot. No age, prose or success inference is performed.
    """
    path = manager.path.with_name("dismissal-request.json")
    if not path.exists():
        return
    request_raw = path.read_bytes()
    request_id = hashlib.sha256(request_raw).hexdigest()
    request = json.loads(request_raw)
    raw = request["snapshot_json"].encode("utf-8")
    expected = json.loads(raw)
    prepared = prepare(raw, request["manifest"])
    keys = [t["parent_task_id"] for t in request["manifest"]["targets"]]
    for key in keys:
        current = manager.cards.get(key)
        if not current:
            raise ValueError("dismissal target no longer exists")
        if current.get("dismissal_request_sha256") == request_id:
            continue  # crash after persistence, before request archival
        for field in ("owner", "source", "rows", "message_id", "handled", "retired"):
            if current.get(field) != expected[key].get(field):
                raise ValueError(f"dismissal target changed: {key} {field}")
    previous = manager.cards
    manager.cards = copy.deepcopy(previous)
    try:
        for key in keys:
            current = manager.cards[key]
            if current.get("dismissal_request_sha256") == request_id:
                continue
            current.update(retired=True, presentation_dismissal=prepared[key]["presentation_dismissal"],
                           dismissal_request_sha256=request_id)
        manager._save()
    except Exception:
        manager.cards = previous
        raise
    path.replace(path.with_name(f"dismissal-applied-{request_id}.json"))
