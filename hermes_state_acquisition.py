"""Same-snapshot structural and occurrence eligibility for the evidence stream."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json

from agent.source_origin import ORIGIN_KEY, MAX_ORIGIN_BYTES, classify_origin
from hermes_state_evidence import EvidenceUnavailable, MAX_SOURCE_ROW_BYTES, _bounded_projection, inventory_rows

# Native session creation records these producer identities, independently of display titles.
CONTROL_ORIGINS = frozenset({"audit_control", "recovery_control"})


@dataclass(frozen=True)
class AcquisitionPolicy:
    roots: tuple[str, ...]
    inventory_sha256: str
    coverage_complete: bool = False


def graph_identity(rows):
    """Digest structural metadata only, including nodes a visible-profile census would miss."""
    digest = hashlib.sha256()
    for row in sorted(rows, key=lambda r: r["id"]):
        digest.update(json.dumps({key: row[key] for key in ("id", "parent_session_id", "source", "created_source")},
                                 sort_keys=True, separators=(",", ":"), allow_nan=False).encode())
        digest.update(b"\n")
    return digest.hexdigest()


def excluded_component(rows, policy):
    if policy.coverage_complete is not True or not policy.roots:
        raise EvidenceUnavailable("control seed coverage is unproven; acquisition held")
    graph = {}
    for row in rows:
        sid, parent = row["id"], row["parent_session_id"]
        if not isinstance(sid, str) or not sid or (parent is not None and (not isinstance(parent, str) or not parent)):
            raise EvidenceUnavailable("malformed structural inventory; acquisition held")
        if sid in graph and graph[sid].get("present"):
            raise EvidenceUnavailable("duplicate structural identity; acquisition held")
        node = graph.setdefault(sid, {"edges": set(), "present": False})
        node["present"] = True
        if parent:
            node["edges"].add(parent)
            graph.setdefault(parent, {"edges": set(), "present": False})["edges"].add(sid)
    if any(not isinstance(root, str) or root not in graph for root in policy.roots):
        raise EvidenceUnavailable("unresolved control seed; acquisition held")
    marked = [row["id"] for row in rows if row["created_source"] in CONTROL_ORIGINS or row["source"] in CONTROL_ORIGINS]
    excluded, pending = set(), [*policy.roots, *marked]
    while pending:
        sid = pending.pop()
        if sid not in excluded:
            excluded.add(sid)
            pending.extend(graph[sid]["edges"] - excluded)
    return excluded, graph


def acquisition_preflight(conn, session_id, policy):
    """Exhaust the unfiltered native inventory before preparing any message body expression."""
    if not isinstance(policy, AcquisitionPolicy):
        raise EvidenceUnavailable("missing owner control coverage; acquisition held")
    rows = list(inventory_rows(conn))
    excluded, graph = excluded_component(rows, policy)
    if graph_identity(rows) != policy.inventory_sha256:
        raise EvidenceUnavailable("inventory changed or incomplete; acquisition held")
    if session_id not in graph or not graph[session_id]["present"] or session_id in excluded:
        raise EvidenceUnavailable("control component or unresolved session; acquisition held")
    # A missing ancestor holds its whole affected component, not just the dangling child.
    pending, visited = [session_id], set()
    while pending:
        sid = pending.pop()
        if sid in visited:
            continue
        if not graph[sid]["present"]:
            raise EvidenceUnavailable("unresolved affected component; acquisition held")
        visited.add(sid)
        pending.extend(graph[sid]["edges"] - visited)
    parents = {row["id"]: row["parent_session_id"] for row in rows}
    chain, current = set(), session_id
    while current is not None:
        if current in chain:
            raise EvidenceUnavailable("cyclic affected component; acquisition held")
        chain.add(current)
        current = parents[current]


def _origin_rows(conn, session_id):
    # Only this reserved namespace is inspected, and only after structural admission.
    # Do not materialize unrelated display metadata or any content column for origin guessing.
    columns = ("id", "session_id", "message_uid", "role", "timestamp", "platform_message_id", "display_kind",
               "active", "compacted", "_compressed_summary", "absorbed_message_uids")
    origin = """, CASE WHEN octet_length(display_metadata) <= ? AND json_valid(display_metadata)
        THEN json_extract(display_metadata, '$._hermes_source_origin') END AS source_origin FROM """
    sql = _bounded_projection("messages", columns).replace(" FROM ", origin, 1)
    sql += " WHERE session_id = ? COLLATE BINARY AND role IN ('user', 'assistant') ORDER BY id"
    for row in conn.execute(sql, (MAX_ORIGIN_BYTES, session_id)):
        result = dict(row)
        if not result.pop("_fits"):
            raise EvidenceUnavailable("origin identity row exceeds source byte limit")
        raw = result.pop("source_origin")
        try:
            origin = json.loads(raw) if raw else None
        except (ValueError, TypeError):
            origin = None
        result["display_metadata"] = {ORIGIN_KEY: origin}
        yield result


def acquire_rows(conn, session_id, policy):
    from agent.authentication_material import exclude_authentication_material

    acquisition_preflight(conn, session_id, policy)
    for row in _origin_rows(conn, session_id):
        decision = classify_origin(row, session_id=session_id)
        result = {k: v for k, v in row.items() if k != "display_metadata"}
        result.update(origin=decision.status, disposition="unresolved")
        if decision.status == "generated":
            result["disposition"] = "ineligible"
        elif decision.status == "verified_human":
            pieces, size = [], 0
            for start, end in decision.spans:
                # Fetch only positively attributable spans, never generated quote/context spans.
                value = conn.execute("SELECT CASE WHEN octet_length(substr(content, ?, ?)) <= ? "
                    "THEN substr(content, ?, ?) END FROM messages WHERE id=? AND session_id=?",
                    (start + 1, end - start, MAX_SOURCE_ROW_BYTES, start + 1, end - start,
                     row["id"], session_id)).fetchone()
                if value is None or not isinstance(value[0], str) or len(value[0]) != end - start:
                    raise EvidenceUnavailable("authored span unavailable; acquisition held")
                size += len(value[0].encode("utf-8"))
                if size > MAX_SOURCE_ROW_BYTES:
                    raise EvidenceUnavailable("authored spans exceed source byte limit")
                pieces.append(exclude_authentication_material(value[0]))
            result.update(disposition="eligible", content="\n".join(pieces), principal=decision.principal,
                          platform=decision.platform, event=decision.event, authored_spans=decision.spans,
                          authored_references=decision.references)
            from agent.correction_evidence import correction_evidence
            correction = correction_evidence(result)
            if correction is not None:
                result["correction_evidence"] = correction
        yield result
