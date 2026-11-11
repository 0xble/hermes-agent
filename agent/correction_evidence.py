"""Occurrence evidence for the existing correction owner, never a correction writer."""

import re

# Candidate detection does not resolve a proposition or adopt an assistant suggestion.
_CANDIDATE = re.compile(r"(?:^\s*no[, ]|\byou(?:'re| are) wrong\b|\bthat(?:'s| is) not what i "
                        r"(?:said|asked)\b|\bi did not say\b|\bstop (?:saying|claiming)\b)", re.IGNORECASE)


def correction_evidence(occurrence, target=None):
    """A reviewed exact target may be supplied by the existing quality/correction owner.

    Missing, ambiguous or cross-scope targets stay unresolved. The replacement is the exact
    authored proposition, not a neighboring outcome or an assistant's interpretation.
    """
    if (occurrence.get("origin") != "verified_human" or occurrence.get("role") != "user"
            or occurrence.get("disposition") != "eligible" or not occurrence.get("principal")):
        return None
    text = occurrence.get("content")
    if not isinstance(text, str) or not _CANDIDATE.search(text):
        return None
    pointer = {key: occurrence.get(key) for key in ("session_id", "id", "message_uid", "authored_spans")}
    result = {"session_id": occurrence.get("session_id"), "user_message_id": occurrence.get("id"),
              "correction_id": f"{occurrence.get('session_id')}:{occurrence.get('message_uid')}:{occurrence.get('id')}",
              "source": "Hermes state.db", "correcting_occurrence": pointer,
              "speaker": occurrence["principal"], "platform": occurrence.get("platform"),
              "at": occurrence.get("timestamp"), "right_claim": text, "status": "unresolved",
              "target_fact": None, "scope": None}
    if (isinstance(target, dict) and target.get("reviewed") is True and target.get("fact_id")
            and target.get("source_pointer") and target.get("scope")
            and target.get("correcting_occurrence") == pointer
            and target.get("speaker") == occurrence["principal"]
            and target.get("replacement") == text):
        result.update(status="attributed", target_fact={key: target[key] for key in ("fact_id", "source_pointer")},
                      scope=target["scope"])
    return result
