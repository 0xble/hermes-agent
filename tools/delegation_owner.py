"""Authorize compression continuations without rewriting immutable delegation owners."""


def session_continues_owner(db, original, current):
    """Only the same session or its proven canonical compression tip may act.

    Common roots are not enough: forks, stale siblings and noncompression
    parent links must not inherit authority. Missing evidence fails closed.
    """
    if not isinstance(original, str) or not original or not isinstance(current, str) or not current:
        return False
    if original == current:
        return True
    lineage_reader = getattr(db, "get_compression_lineage", None)
    if not callable(lineage_reader):
        return False
    # Use the same canonical compression evidence as resume preflight, not
    # generic resume redirection (which may also follow noncompression links).
    lineage = lineage_reader(current)
    return (isinstance(lineage, list) and original in lineage and current in lineage
            and lineage.index(original) < lineage.index(current)
            and lineage[-1] == current and lineage_reader(original) == lineage)


def delegation_owner_matches(original, current, db=None):
    """All profile/session-key/chat/thread/topic fields remain exact."""
    if not isinstance(original, dict) or not isinstance(current, dict):
        return False
    return ({k: v for k, v in original.items() if k != "session_id"}
            == {k: v for k, v in current.items() if k != "session_id"}
            and session_continues_owner(db, original.get("session_id"), current.get("session_id")))
