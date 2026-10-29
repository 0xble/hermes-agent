"""Goal authority matching and recorded-user/evidence helpers.

The facade imports these names below its relay readers. Functions late-bind facade
state so monkeypatches on ``hermes_cli.goals`` remain the production seam.
"""
from __future__ import annotations

import logging
import re
import unicodedata
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger("hermes_cli.goals")


def _facade():
    from hermes_cli import goals
    return goals


def _is_user_typed(row: Dict[str, Any]) -> bool:
    """Whether a user-role row is input the person typed, judged by provenance first."""
    content = row.get("content")
    if not isinstance(content, str) or row.get("compressed_summary"):
        return False
    if str(row.get("display_kind") or "") not in _facade()._USER_TYPED_KINDS:
        return False
    if _facade()._is_agent_origin(content):
        return False
    if content.lstrip().startswith(_facade()._SYNTHETIC_USER_PREFIXES):
        return False
    # Relay and other gateway-authored text is persisted as an ordinary user row.  The
    # response-origin marker is the authoritative second provenance check; fail closed if
    # the filter cannot be imported so synthetic text can never authorize a revision.
    try:
        from gateway.response_filters import is_agent_origin_text
        if is_agent_origin_text(content):
            return False
    except Exception:
        return False
    try:
        from agent.context_compressor import ContextCompressor
        if ContextCompressor._is_context_summary_content(content):
            return False
    except Exception:
        # Same fail-closed rule: if a context summary cannot be ruled out, the row cannot
        # authorize a revision or session control.
        return False
    return True
# Gateway reply pointer (gateway/run_inbound.py ``_prepend_inbound_reply_context``), with or without
# " your previous message". Greedy to the last ``"]`` line end: the quoted text is the assistant's
# words and may itself contain ``"]``, so stripping too much fails closed for quote authority.
_REPLY_QUOTE_RE = re.compile(r'^\[Replying to(?: your previous message)?: ".*"\]\n\s*', re.DOTALL)


_REVISION_QUOTE_MIN_CHARS = 12
# Longest user message a revision may cite. The judge decides authority from the complete message,
# so a longer one is refused rather than excerpted: an excerpt can drop the context that negates it.
_REVISION_SOURCE_MAX_CHARS = 4000
# Replacement authority is deliberately narrower than ordinary revision authority. The requested
# replacement itself must be present in the recorded user message; a generic "change the scope" is
# not authorization for an agent-invented objective. Matching is deterministic and punctuation-safe.
_REPLACEMENT_ACTION_RE = re.compile(
    r"\b(?:replace|change)\s+(?:the\s+)?(?:goal|objective)\s+(?:with|to)\b",
    re.IGNORECASE,
)
# Match the restriction target itself, not work-domain words elsewhere in a directive.
_REPLACEMENT_GATE_TARGET_RE = re.compile(r"(?:(?:goal )?quality gates?|goal gates?|gates)")
_REPLACEMENT_BUDGET_TARGET_RE = re.compile(r"(?:(?:goal |remaining )?turn (?:budget|limit)|goal budget)")
_REMOVAL_VERBS = r"(?:remove|clear|drop|delete|disable|reset|skip|relax|lift|waive|stop requiring)"
_REPLACEMENT_NEGATION_RE = re.compile(r"\b(?:do\s+not|don['’]t|dont|never|not)\b", re.IGNORECASE)
_AUTHORITY_PRESERVE_RE = re.compile(
    r"\b(?:do\s+not|don't|dont|never|not|keep|preserve|retain|leave|without\s+removing|"
    r"without\s+dropping|without\s+clearing)\b",
    re.IGNORECASE,
)


def _normalize_authority_text(text: Any) -> str:
    """Normalize recorded text for safe authority matching, not for semantic inference."""
    normalized = unicodedata.normalize("NFKC", str(text or "")).casefold()
    # Expand negative contractions before punctuation becomes a space, so "don't" and "don’t"
    # keep their "not" instead of degrading to the unrecognized "don t".
    normalized = re.sub(r"(?<=\w)n['’]t\b", " not", normalized)
    chars = [" " if ch.isspace() or unicodedata.category(ch).startswith("P") else ch
             for ch in normalized]
    return " ".join("".join(chars).split())


def _split_text_units(text: str) -> List[str]:
    """Split sentence/clause-sized text without treating punctuation inside tokens as a boundary."""
    return [part.strip() for part in re.split(r"(?<=[.!?;])(?=\s|$)", text or "") if part.strip()]


def _authority_unit_records(text: str) -> List[Tuple[str, List[str], str]]:
    """Mask before splitting, retaining original offsets and quoted data (never quoted authority)."""
    masked = _mask_non_authoritative_spans(text or "")
    records: List[Tuple[str, List[str], str]] = []
    cursor = 0
    boundaries = [match.start() for match in re.finditer(r"(?<=[.!?;])(?=\s|$)", masked)]
    boundaries.append(len(masked))
    for boundary in boundaries:
        unit = masked[cursor:boundary]
        if unit.strip():
            quotes = [
                match.group(0)[1:-1]
                for match in _NON_AUTHORITATIVE_SPANS_RE.finditer(text or "")
                if cursor <= match.start() and match.end() <= boundary
                and _NON_AUTHORITATIVE_QUOTE_RE.fullmatch(match.group(0))
            ]
            records.append((unit, quotes, text[cursor:boundary]))
        cursor = boundary
    return records


def _authority_units(text: str) -> List[str]:
    """Return sentence/clause units after masking the complete source first."""
    return [unit for unit, _quotes, _original in _authority_unit_records(text)]


_NON_AUTHORITATIVE_QUOTE_RE = re.compile(
    r"“[^”]*”|‘[^’]*’|\"(?:\\.|[^\"])*\"|(?<!\w)'[^'\n]+?'(?!\w)",
    re.DOTALL,
)
_NON_AUTHORITATIVE_SPANS_RE = re.compile(
    r"```.*?```|`[^`]*`|“[^”]*”|‘[^’]*’|\"(?:\\.|[^\"])*\"|(?<!\w)'[^'\n]+?'(?!\w)",
    re.DOTALL,
)


def _removal_authority_text(unit: str) -> str:
    """Return only unquoted, uncoded text that can carry a removal instruction."""
    return _mask_non_authoritative_spans(unit)


def _mask_non_authoritative_spans(text: str) -> str:
    """Blank quoted and code spans while preserving offsets for sentence splitting."""
    return _NON_AUTHORITATIVE_SPANS_RE.sub(lambda match: " " * len(match.group(0)), text or "")


def _mask_replacement_objective_spans(text: str) -> str:
    """Blank replacement values before removal parsing can interpret their words as directives."""
    masked = text or ""
    action = _REPLACEMENT_ACTION_RE.search(masked)
    if action:
        masked = masked[:action.end()] + (" " * (len(masked) - action.end()))
    return masked


def _direct_authority_unit(unit: str) -> str:
    """Return only text that can carry a user's direct instruction."""
    return _mask_non_authoritative_spans(unit)


def _unit_is_non_directive(unit: str) -> bool:
    """Questions, conditionals, and other hypothetical clauses never authorize mutation."""
    stripped = (unit or "").strip()
    normalized = _normalize_authority_text(_direct_authority_unit(stripped))
    if not normalized:
        return True
    if stripped.endswith("?"):
        return True
    if re.match(r"^(?:what|why|how|when|where|who|can|could|would|should|shall|do|does|did|is|are|will|may|might)\b",
                normalized, re.IGNORECASE):
        return True
    if re.match(r"^(?:if|unless|provided|assuming|suppose|supposing)\b", normalized, re.IGNORECASE):
        return True
    if re.search(r"\b(?:if|unless|provided|assuming|suppose|supposing)\b", normalized, re.IGNORECASE):
        return True
    return False


def _replacement_goal_is_negated(unit: str, goal: str) -> bool:
    unit_norm = _normalize_authority_text(unit)
    target = _normalize_authority_text(goal)
    target_match = _bounded_authority_match(target, unit_norm)
    if target_match is None:
        return False
    target_at = target_match.start()
    for cue in _AUTHORITY_PRESERVE_RE.finditer(unit_norm):
        if cue.start() >= target_at and cue.end() <= target_at + len(target):
            continue
        if cue.end() <= target_at:
            return True
    return False


def _replacement_action_is_authorized(source: str, goal: str) -> bool:
    """Require an explicit, unquoted, un-negated replacement instruction in one direct clause."""
    for direct, _quotes, original in _authority_unit_records(source):
        if _unit_is_non_directive(direct):
            continue
        action = _REPLACEMENT_ACTION_RE.search(direct)
        if not action:
            continue
        # The action is in masked imperative text; its value is the entire original span
        # through the sentence/clause end. Quotes can delimit a value, never supply the action.
        replacement = original[action.end():].lstrip().removeprefix(":").lstrip()
        if _normalize_authority_value(goal) != _normalize_authority_value(replacement):
            continue
        prefix = direct[:action.start()]
        if _REPLACEMENT_NEGATION_RE.search(prefix):
            continue
        if re.search(r"\b(?:keep|preserve|retain|leave)\b.{0,24}\b(?:goal|objective)\b", prefix,
                     re.IGNORECASE | re.DOTALL):
            continue
        if not _replacement_goal_is_negated(direct, goal):
            return True
    return False


def _removal_instruction_units(source: str, goal: str = "") -> List[Tuple[str, str, List[str]]]:
    """Return explicit removal verbs and their targets from independent imperative clauses.

    Mask replacement-objective text before examining directives. A work task may itself
    contain a removal command; it cannot grant permission to remove a standing-goal restriction.
    The same clause path binds ordinary revision removals to their actual targets.
    """
    instructions = []
    target = _normalize_authority_text(goal)
    for unit, quoted_targets, _original in _authority_unit_records(source):
        if _unit_is_non_directive(unit):
            continue
        normalized = _normalize_authority_text(_mask_replacement_objective_spans(unit))
        # Mask only the objective in its replacement clause, not the same text in an
        # independent removal directive. Whole-phrase matching cannot join words into a target.
        if target:
            for match in re.finditer(rf"(?<!\w){re.escape(target)}(?!\w)", normalized):
                if _REPLACEMENT_ACTION_RE.search(normalized[:match.start()]):
                    normalized = normalized[:match.start()] + " replacementobjective " + normalized[match.end():]
                    break
        # The quotes are data, never imperative text. A placeholder lets an otherwise bare
        # "Remove 'item'" directive parse without interpreting any words inside the quote.
        if quoted_targets:
            normalized += " quotedtarget"
        directives = re.split(
            rf"\b(?:and|but|then)\s+(?=(?:please\s+)?{_REMOVAL_VERBS}\b)"
            rf"|\s+(?=(?:please\s+)?{_REMOVAL_VERBS}\b)"
            r"|\s+(?=(?:and|but|then)\s+(?:ship|change|replace|set|update|widen|narrow)\b)",
            normalized,
        )
        for directive in directives:
            removal = re.fullmatch(rf"\s*(?:please\s+)?({_REMOVAL_VERBS})\s+(.+?)\s*", directive)
            if removal:
                targets = removal.group(2)
                instructions.append((removal.group(1), targets, quoted_targets))
    return instructions


def _explicit_restriction_removals(source: str, goal: str) -> Tuple[bool, bool]:
    """Require complete restriction targets in independent, un-negated removal directives."""
    remove_gates = remove_budget = False
    for verb, targets, _quoted_targets in _removal_instruction_units(source, goal):
        if _AUTHORITY_PRESERVE_RE.search(targets):
            continue
        for item in re.split(r"\b(?:and|or)\b", targets):
            item = re.sub(r"^(?:the|all|existing|current)\s+", "", item.strip())
            item = re.sub(r"\s+for (?:this|the) goal$", "", item)
            if verb in {"remove", "clear", "drop", "delete", "disable"}:
                remove_gates |= bool(_REPLACEMENT_GATE_TARGET_RE.fullmatch(item))
            if verb in {"remove", "clear", "drop", "delete", "disable", "reset"}:
                remove_budget |= bool(_REPLACEMENT_BUDGET_TARGET_RE.fullmatch(item))
    return remove_gates, remove_budget

def _normalize_authority_value(text: Any) -> str:
    """Normalize exact values without losing substantive punctuation or objective words."""
    value = unicodedata.normalize("NFKC", str(text or "")).casefold()
    value = value.translate(str.maketrans({"“": '"', "”": '"', "‘": "'", "’": "'"}))
    value = " ".join(value.split()).rstrip(".!?;").strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
        value = value[1:-1].strip().rstrip(".!?;").strip()
    return value


def _bounded_authority_match(needle: str, source: str) -> Optional[re.Match[str]]:
    """Match a normalized token sequence, never part of a word."""
    return re.search(rf"(?<!\w){re.escape(needle)}(?!\w)", source) if needle else None


def _requirement_diff(field_name: str, before: Any, after: Any) -> Tuple[List[str], List[str], List[str]]:
    """Return retained, removed, and added canonical items."""
    before_items = {_normalize_authority_value(item): item
                    for item in _facade().contract_items(field_name, before)}
    after_items = {_normalize_authority_value(item): item
                   for item in _facade().contract_items(field_name, after)}
    retained = [item for key, item in after_items.items() if key in before_items]
    removed = [item for key, item in before_items.items() if key not in after_items]
    added = [item for key, item in after_items.items() if key not in before_items]
    return retained, removed, added


def _bounded_evidence_match(needle: str, content: str) -> bool:
    """Match a recorded evidence excerpt without accepting partial tokens."""
    raw = unicodedata.normalize("NFKC", str(needle or "")).casefold()
    text = unicodedata.normalize("NFKC", str(content or "")).casefold()
    raw = " ".join(raw.split())
    text = " ".join(text.split())
    return bool(raw and re.search(rf"(?<!\w){re.escape(raw)}(?!\w)", text))


def _evidence_is_negated(content: str, evidence: str, *, include_no: bool = False) -> bool:
    """Reject a cited excerpt when the recorded result explicitly preserves it."""
    target = " ".join(unicodedata.normalize("NFKC", str(evidence or "")).casefold().split())
    negators = r"\b(?:do not|don['’]t|dont|never|not|keep|preserve|retain|without)\b"
    if include_no:
        negators += r"|\bno\b"
    for unit in re.split(r"(?<=[.!?;])\s+", unicodedata.normalize("NFKC", str(content or ""))):
        normalized = " ".join(unit.casefold().split())
        match = re.search(rf"(?<!\w){re.escape(target)}(?!\w)", normalized)
        if match and re.search(negators, normalized[:match.start()]):
            return True
    return False

def _typed_user_message_records_since(session_id: Optional[str], since: float = 0.0,
                                      limit: int = 500) -> List[Dict[str, Any]]:
    """Newest-first active typed user rows; withdrawn and summarized-away rows cannot authorize."""
    db = _facade()._get_session_db()
    reader = getattr(db, "get_messages", None) if db is not None else None
    if not session_id or reader is None:
        return []
    try:
        # Replay's active-only accessor preserves carried compaction copies, but never revives
        # rewind/Undo rows or soft-archived originals. Select typed input before applying its limit.
        rows = reader(session_id, include_inactive=False, include_compacted=False)
    except Exception as exc:  # health: allow BLE001 -- malformed or unavailable session rows must fail closed for authority
        logger.debug("goal revise: user message read failed: %s", exc)
        return []
    return [row for row in reversed(rows)
            if row.get("role") == "user" and _is_user_typed(row)
            and float(row.get("timestamp") or 0.0) >= float(since or 0.0)][:limit]


def _typed_user_message_text(row: Dict[str, Any]) -> str:
    """Return only the user's text, excluding a client-provided assistant quote header."""
    content = row.get("content")
    return _REPLY_QUOTE_RE.sub("", content, count=1) if isinstance(content, str) else ""


def user_messages_since(session_id: Optional[str], since: float = 0.0, limit: int = 500) -> List[str]:
    """Text the user actually wrote since ``since``: synthetic runtime prompts are dropped and a
    reply's quoted header (which repeats the assistant's words) is stripped. Fail-safe: ``[]``."""
    return [_typed_user_message_text(row)
            for row in _typed_user_message_records_since(session_id, since=since, limit=limit)]
