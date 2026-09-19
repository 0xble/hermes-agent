"""Auto-generate short session titles from the user's opening message.

Two stages, both off the critical path: an **instant** deterministic title (written before the model
is called, cannot fail), then an **upgrade** from one small-model call (cheap tier, thinking off,
JSON-constrained). Storage enforces provenance ``derived < llm < user``: stage 2 only replaces stage 1
and neither replaces a name the user typed."""

import inspect
import json
import logging
import re
import threading
from contextlib import suppress
from typing import Any, Callable, Optional

from agent.auxiliary_client import call_llm
from agent.context_compressor import LEGACY_SUMMARY_PREFIX
from agent.message_content import flatten_message_text

logger = logging.getLogger(__name__)

# (task_name, exception) -> None; surfaces auxiliary failures so silent drops don't pile up as NULL titles.
FailureCallback = Callable[[str, BaseException], None]
# (title, source[, display_title=...]) -> None. ``title`` is the persisted
# alias; ``display_title`` is the unsuffixed label for transports such as
# Telegram that keep visible topic names separate from session aliases.
TitleCallback = Callable[..., None]
# () -> bool, called right before the LLM request; False skips (e.g. the user switched models and
# the request would reload one the runtime already evicted).
# Validation callback: () -> bool. See #19027.
RuntimeValidator = Callable[[], bool]

# Text budget handed to the model (Claude Code / OpenClaw converged on 1000).
MAX_TITLE_INPUT_CHARS = 1000
# Cap on the instant derived title; a raw fragment reads worse the longer it runs.
MAX_DERIVED_TITLE_CHARS = 48
# Answer-shaped guard: a tiny model sometimes answers instead of titling; longer is rejected, not truncated.
# Upper bound on accepted title word count. Titling is a 3-7 word task; a small tiny-model sometimes ignores
# the task and answers the user's message instead — that answer must never become the session title (see the
# answer-shaped output guard in generate_title; port of can1357/oh-my-pi#7306). 12 leaves headroom for
# legitimate wordy titles while excluding full-sentence answers.
_MAX_TITLE_WORDS = 12

# The example titles shown to the model in the prompt, and the echo-guard
# set: when the opening message carries little topical signal, a small model
# sometimes takes the cheapest schema-valid answer and parrots one of these
# back verbatim — most visibly "Fix login button on mobile" naming sessions
# that have nothing to do with a login button. The prompt's example lines are
# rendered from these constants so the guard set and the prompt cannot drift
# apart. Port of QwenLM/qwen-code#9709.
_PROMPT_GOOD_EXAMPLES = (
    "Fix login button on mobile",
    "Postgres connection pool exhaustion",
    "Friendly greeting",
)
_PROMPT_VAGUE_EXAMPLE = "Code changes"

# "Friendly greeting" is deliberately NOT in the reject set: the prompt
# instructs the model to produce it for bare greetings, so it is a legitimate
# output, not an echo failure. The too-vague example is rejected too — a model
# repeating the counter-example says nothing about the session, and the
# derived title the guard falls back to is strictly more informative.
_EXAMPLE_ECHO_REJECT = frozenset(
    t.lower() for t in _PROMPT_GOOD_EXAMPLES if t != "Friendly greeting"
) | {_PROMPT_VAGUE_EXAMPLE.lower()}

_TITLE_PROMPT_TEMPLATE = (
    "You name chat sessions. Given the user's opening message, write a concise noun-phrase title "
    "that lets them find this conversation again in a list.\n\n"
    "Rules:\n"
    "- __WORD_RULE__\n"
    "- __CASE_RULE__\n"
    "- Name the subject, artifact, or decision, never an imperative or conditional action.\n"
    "- Keep technical terms, filenames, numbers, and error codes exact.\n"
    "- Drop filler words: the, this, my, a, an.\n"
    "- No trailing punctuation, no quotes, no tool names, no 'Title:' prefix.\n"
    "- Never answer the message. Name it.\n"
    "- Always produce something, even for a bare greeting.\n"
    "__LANGUAGE_RULE__\n"
    "__INSTRUCTIONS__\n"
    "__AVOID_TITLES__\n"
    + "".join(f'Good: {{"title": "{t}"}}\n' for t in _PROMPT_GOOD_EXAMPLES)
    + f'Too vague: {{"title": "{_PROMPT_VAGUE_EXAMPLE}"}}\n'
    'Contrastive example: use "Postgres connection pool exhaustion", not "Fix the Postgres connection pool".\n\n'
    'Reply with JSON only: {"title": "..."}'
)

_LANGUAGE_RULE_MATCH_USER = "- Write the title in the same language as the user's message."
_LANGUAGE_RULE_PINNED = "- Write the title in {language}."

# Constrains the response to a single title field ("model answered instead of titling" failure class).
_TITLE_RESPONSE_FORMAT = {
    "type": "json_schema",
    "json_schema": {"name": "session_title", "strict": True, "schema": {
        "type": "object", "properties": {"title": {"type": "string"}}, "required": ["title"], "additionalProperties": False}},
}

# Control-tag wrappers around machine-authored content inside a nominal "user" message (Codex CLI's
# RECOGNIZED_CONTROL_WRAPPERS): stripped, titling continues on what remains.
_CONTROL_WRAPPERS = tuple(
    (f"<{tag}>", f"</{tag}>")
    for tag in ("command-message", "command-name", "command-args", "local-command-caveat", "local-command-stderr",
                "local-command-stdout", "task-notification", "system-reminder", "ide_opened_file", "ide_selection")
)

# Hermes' own machine-authored openers: a compaction handoff or resumed session must not be titled after them.
_MACHINE_PREFIXES = (
    "[CONTEXT COMPACTION", LEGACY_SUMMARY_PREFIX, "[Runtime note:", "[System note:", "[SYSTEM]",
    # tui_gateway.server._MODEL_SWITCH_MARKER_PREFIX (keep in sync); persisted as role="user" because
    # strict providers reject a non-first system message.
    # Model-switch marker from tui_gateway.server._append_model_switch_marker. It is persisted with
    # role="user" (strict OpenAI-compatible providers reject a system message that is not first — #48338),
    # so without this entry it looks like a real opening turn: switching models before the first real
    # message titled the session "[System: The active model for this chat has…" instead of the user's actual
    # question.
    "[System: The active model for this chat has changed to ",
)


def _title_config() -> dict:
    """``auxiliary.title_generation`` (lazy read-only import: no hermes_cli cycle, no migration writes)."""
    from hermes_cli.config import load_config_readonly
    return ((load_config_readonly() or {}).get("auxiliary") or {}).get("title_generation") or {}


def _title_preferences() -> dict:
    """Return bounded title prompt preferences from the operator config."""
    cfg = _title_config()
    try:
        min_words = max(1, min(12, int(cfg.get("min_words", 3))))
    except (TypeError, ValueError):
        min_words = 3
    try:
        max_words = max(1, min(12, int(cfg.get("max_words", 7))))
    except (TypeError, ValueError):
        max_words = 7
    if min_words > max_words:
        min_words = max_words
    case_style = str(cfg.get("case_style", "sentence_case")).strip().lower()
    if case_style not in {"sentence_case", "title_case"}:
        case_style = "sentence_case"
    instructions = cfg.get("instructions", "")
    if isinstance(instructions, (list, tuple)):
        instructions = " ".join(str(item) for item in instructions)
    instructions = str(instructions or "").strip()[:1000]
    aliases = cfg.get("name_aliases", {})
    if not isinstance(aliases, dict):
        aliases = {}
    aliases = {str(k): str(v) for k, v in aliases.items() if str(k).strip() and str(v).strip()}
    return {"min_words": min_words, "max_words": max_words, "case_style": case_style,
            "instructions": instructions, "name_aliases": aliases}


def _title_prompt(*, language: str, recent_titles=None) -> str:
    prefs = _title_preferences()
    case_rule = ("Title case: capitalize the principal words; this is the only capitalization rule."
                 if prefs["case_style"] == "title_case" else
                 "Sentence case: capitalize only the first word and proper nouns; this is the only capitalization rule.")
    avoid = [str(t).strip() for t in (recent_titles or []) if str(t).strip()][:20]
    avoid_block = "Avoid these recent session titles; do not copy them:\n" + "\n".join(f"- {t}" for t in avoid) if avoid else ""
    instruction_block = f"Trusted operator guidance (follow literally): {prefs['instructions']}" if prefs["instructions"] else ""
    return _TITLE_PROMPT_TEMPLATE.replace("__WORD_RULE__", f"{prefs['min_words']} to {prefs['max_words']} words.") \
        .replace("__CASE_RULE__", case_rule) \
        .replace("__LANGUAGE_RULE__", _LANGUAGE_RULE_PINNED.format(language=language) if language else _LANGUAGE_RULE_MATCH_USER) \
        .replace("__INSTRUCTIONS__", instruction_block) \
        .replace("__AVOID_TITLES__", avoid_block)


def _restore_name_aliases(title: str, aliases: dict) -> str:
    """Restore configured display names without treating operator text as regex."""
    result = title
    for alias, canonical in sorted(aliases.items(), key=lambda item: len(item[0]), reverse=True):
        parts = [re.escape(part) for part in re.findall(r"[\w]+", alias, flags=re.UNICODE)]
        if not parts:
            continue
        pattern = r"(?<!\w)" + r"[\W_]+".join(parts) + r"(?!\w)"
        result = re.sub(pattern, lambda _match, value=canonical: value, result, flags=re.IGNORECASE | re.UNICODE)
    return result

def _title_language() -> str:
    """Configured title language, or "" to match the user."""
    try:
        return str(_title_config().get("language", "")).strip()
    except Exception:
        return ""

def _auto_title_enabled() -> bool:
    try:
        from utils import is_truthy_value
        return is_truthy_value(_title_config().get("enabled"), default=True)
    except Exception:
        logger.debug("Failed to read title_generation.enabled", exc_info=True)
        return True


def strip_control_wrappers(text: str) -> str:
    """Remove leading control wrappers (nested too) so a slash-command turn reduces to the prose the user typed."""
    current = (text or "").strip()
    for _ in range(len(_CONTROL_WRAPPERS) * 2):  # bounded: each pass must remove a wrapper or we stop
        stripped = _strip_one_wrapper(current)
        if stripped == current:
            break
        current = stripped
    return current


def _strip_one_wrapper(text: str) -> str:
    lowered = text.lower()
    for open_tag, close_tag in _CONTROL_WRAPPERS:
        if not lowered.startswith(open_tag):
            continue
        end = lowered.find(close_tag)
        if end == -1:  # unterminated wrapper: drop the opening tag and keep the body
            return text[len(open_tag):].strip()
        # Prefer trailing prose; otherwise the wrapper body is all we have.
        return (text[end + len(close_tag):].strip() or text[len(open_tag):end].strip()).strip()
    return text


def _summarize_user_message(user_message: str) -> str:
    """Text worth titling: describe a ``/skill`` invocation (it embeds the whole skill body), then strip wrappers."""
    if not user_message:
        return ""
    described = None
    try:
        from agent.skill_commands import describe_skill_invocation
        described = describe_skill_invocation(user_message)
    except Exception:
        logger.debug("Skill-scaffolding summary failed; titling raw", exc_info=True)
    return strip_control_wrappers(user_message if described is None else described)


def is_titleable_user_message(user_message: str) -> bool:
    """False for machine-authored openers and turns that reduce to nothing once scaffolding is stripped."""
    return (isinstance(user_message, str) and bool(user_message.strip()) and not user_message.lstrip().startswith(_MACHINE_PREFIXES)
            and bool(_summarize_user_message(user_message).strip()))


def derive_title(user_message: str) -> Optional[str]:
    """Instant title: first meaningful line trimmed to a word boundary. No model, never fails."""
    line = " ".join(_first_line(_summarize_user_message(user_message)).split())
    if len(line) > MAX_DERIVED_TITLE_CHARS:
        cut = line[:MAX_DERIVED_TITLE_CHARS]
        space = cut.rfind(" ")
        line = (cut[:space] if space > MAX_DERIVED_TITLE_CHARS // 2 else cut).rstrip(" ,.;:—-") + "…"
    return line or None


def _strip_title_prefix(text: str) -> str:
    return text[6:].strip() if text.lower().startswith("title:") else text


def _first_line(text: str) -> str:
    return next((ln.strip() for ln in text.splitlines() if ln.strip()), "")


def _extract_title_text(content: str) -> str:
    """Strict JSON, then a loose JSON scan, then first-line prose (a provider ignoring ``response_format`` still titles)."""
    if not content:
        return ""
    raw = content.strip()
    fenced = re.match(r"^```(?:json)?\s*(.*?)\s*```$", raw, re.DOTALL)
    if fenced:
        raw = fenced.group(1).strip()
    try:
        parsed = json.loads(raw)
        if isinstance(parsed, dict) and isinstance(parsed.get("title"), str):
            return parsed["title"].strip()
    except (ValueError, TypeError):
        pass
    match = re.search(r'"title\"\s*:\s*"((?:[^"\\]|\\.)*)"', raw)
    if match:
        with suppress(ValueError):
            return json.loads(f'"{match.group(1)}"').strip()
        return match.group(1).strip()
    # Prose fallback: scrub <think> blocks so reasoning can't leak into a title.
    try:
        from agent.agent_runtime_helpers import strip_think_blocks
        raw = strip_think_blocks(None, raw).strip()
    except Exception:
        logger.debug("strip_think_blocks unavailable for title output", exc_info=True)
    return _strip_title_prefix(_first_line(raw)).strip("\"'").strip()


def _clean_title(text: str) -> Optional[str]:
    """Normalize a model-produced title, or None when nothing usable remains."""
    title = _strip_title_prefix(" ".join((text or "").split()).strip("\"'").strip()).rstrip(".!,;:")
    if len(title) > 80:
        title = title[:77].rstrip() + "..."
    return title or None


def _safe_callback(callback: Optional[Callable], args: tuple, log_fmt: str, label: str) -> None:
    """Invoke an optional consumer callback, never raising."""
    try:
        if callback is not None:
            callback(*args)
    except Exception:
        logger.debug(log_fmt, label, exc_info=True)


def _report_failure(failure_callback: Optional[FailureCallback], exc: BaseException, label: str) -> None:
    _safe_callback(failure_callback, ("title generation", exc), "%s failure_callback raised", label)


def _notify_title(
    title_callback: Optional[TitleCallback],
    title: str,
    source: str,
    label: str,
    *,
    display_title: Optional[str] = None,
) -> None:
    """Notify title consumers, preserving compatibility with two-argument callbacks."""
    if title_callback is None:
        return
    try:
        if display_title is None:
            title_callback(title, source)
            return
        try:
            signature = inspect.signature(title_callback)
            signature.bind(title, source, display_title=display_title)
        except (TypeError, ValueError):
            title_callback(title, source)
        else:
            title_callback(title, source, display_title=display_title)
    except Exception:
        logger.debug("%s callback failed", label, exc_info=True)


def _is_prompt_example_echo(title: str) -> bool:
    """Return True when *title* is one of the prompt's own example titles.

    Comparison is case-insensitive after stripping any leading/trailing run of
    non-letter/non-digit characters, so bracket/quote wrappers cannot bypass
    the guard — while ``_clean_title`` keeps brackets for real titles like
    "(WIP) Fix build". Unicode-aware so full-width wrappers are covered too.
    """
    normalized = re.sub(r"^[\W_]+|[\W_]+$", "", title.strip(), flags=re.UNICODE).lower()
    return normalized in _EXAMPLE_ECHO_REJECT


def generate_title(
    user_message: str,
    timeout: Optional[float] = None,
    failure_callback: Optional[FailureCallback] = None,
    main_runtime: dict = None,
    runtime_validator: Optional[RuntimeValidator] = None,
    avoid_titles=None,
    recent_titles=None,
    icon_options=None,
    recent_icons=None,
    icon_instructions: str = "",
    icon_callback: Optional[Callable[[Optional[str], str], None]] = None,
) -> Optional[str]:
    """Title from the opening message alone (waiting for the assistant made this slow and bought
    nothing). ``runtime_validator`` runs right before the request; False skips silently.

    If it returns False (e.g. the user's model was switched since the background thread captured its runtime
    snapshot), the call is skipped silently — no request is sent, so a stale title request can't reload a
    model the runtime already unloaded (#19027).
    """
    if not _auto_title_enabled():
        logger.debug("Auto-title skipped: auxiliary.title_generation.enabled=false")
        return None
    try:
        if runtime_validator is not None and not runtime_validator():
            logger.debug("Title generation skipped: runtime validator returned False")
            return None
    except Exception:  # fail open: a broken validator must not disable titling
        logger.debug("Title runtime validator raised; proceeding", exc_info=True)
    user_snippet = _summarize_user_message(user_message)[:MAX_TITLE_INPUT_CHARS]
    if not user_snippet.strip():
        return None
    language = _title_language()
    prompt = _title_prompt(language=language, recent_titles=recent_titles or avoid_titles)
    icon_allowed = list(icon_options or [])
    if icon_allowed:
        from agent.topic_icons import normalize_emoji
        recent = {normalize_emoji(item) for item in (recent_icons or [])}
        fresh = [item for item in icon_allowed if normalize_emoji(item) not in recent]
        candidates = fresh if len(fresh) >= 8 else icon_allowed
        prompt += "\n\nAlso select one icon. Reply with JSON only: {\"title\": \"...\", \"icon\": \"...\"}. " \
            f"Allowed icons: {[str(item.get('emoji', item)) if isinstance(item, dict) else str(item) for item in candidates]}."
        if icon_instructions:
            prompt += f" Icon guidance: {str(icon_instructions).strip()[:1000]}"
    try:
        response = call_llm(
            task="title_generation",
            messages=[{"role": "system", "content": prompt}, {"role": "user", "content": user_snippet}],
            # A title is a handful of tokens; a larger ceiling let chatty models burn seconds.
            max_tokens=64, temperature=0.3, timeout=timeout, main_runtime=main_runtime,
            extra_body={"response_format": _TITLE_RESPONSE_FORMAT},
            # The module contract above promises thinking-disabled operation,
            # but nothing enforced it: with the aux default reasoning_effort
            # "" (provider default), Gemini enables internal thinking and
            # bills thought tokens against max_tokens=64 — the JSON payload
            # never lands, and the prose fallback stores the opening fence
            # ("```json") as the session title (#91927).
            reasoning_config={"enabled": False},
        )
        choice = response.choices[0]
        finish_reason = getattr(choice, "finish_reason", None)
        if finish_reason in {"length", "max_tokens"}:
            logger.debug("Rejecting truncated title output (finish_reason=%s)", finish_reason)
            return None
        raw_content = getattr(getattr(choice, "message", None), "content", "") or ""
        payload = None
        if icon_allowed:
            with suppress(Exception):
                candidate = str(raw_content).strip()
                fence = re.fullmatch(r"```(?:json)?\s*(.*?)\s*```", candidate, flags=re.IGNORECASE | re.DOTALL)
                payload = json.loads(fence.group(1) if fence else candidate)

        if not any(char.isalnum() for char in str(raw_content)):
            logger.debug("Rejecting title output with no Unicode letters or digits")
            return None
        title_value = payload.get("title") if isinstance(payload, dict) else None
        title = _clean_title(_extract_title_text(title_value or raw_content))
        if icon_allowed and icon_callback is not None:
            from agent.topic_icons import choose_topic_icon_deterministic, validate_model_icon
            icon = validate_model_icon(payload.get("icon") if isinstance(payload, dict) else None, icon_allowed)
            if icon is None:
                icon = choose_topic_icon_deterministic(title or "", user_snippet, icon_allowed, recent_icons)
            with suppress(Exception):
                icon_callback(icon, "llm" if title else "fallback")
        if title is None or not any(char.isalnum() for char in title):
            return None
        title = _restore_name_aliases(title, _title_preferences()["name_aliases"])
        # Answer-shaped output guard: titling is a short noun-phrase task.
        if len(title.split()) > _MAX_TITLE_WORDS:
            logger.debug("Rejecting answer-shaped title output (%d words > %d)", len(title.split()), _MAX_TITLE_WORDS)
            return None
        if _is_prompt_example_echo(title):
            logger.debug("Rejecting prompt-example echo title: %r", title)
            return None
        return title
    except Exception as e:
        # WARNING so it shows in agent.log without debug mode; stack at debug.
        logger.warning("Title generation failed: %s", e)
        logger.debug("Title generation traceback", exc_info=True)
        _report_failure(failure_callback, e, "Title generation")
        return None


def _has_upgraded_title(session_db, session_id: str) -> bool:
    """True when the session already carries an ``llm``/``user`` title (or the check fails)."""
    try:
        source_fn = getattr(session_db, "get_session_title_source", None)
        if source_fn is not None:
            return source_fn(session_id) not in (None, "derived")
        return bool(session_db.get_session_title(session_id))
    except Exception:
        return True


def _persist_session_title(session_db, session_id, title, *, source, dedupe=True):
    """Persist at *source* authority via ``set_auto_title`` (precedence check + write in one
    transaction, so a manual ``/title`` is never overwritten); None when a higher authority held the row.
    ``ValueError`` = unique-title index collision → append ``#N`` via ``get_next_title_in_lineage``;
    ``dedupe=False`` re-raises instead (the derived title is on the critical path, collides constantly
    on "hi", and the model replaces it a second later anyway).

    ``ValueError`` means the name is taken by an unrelated session (the unique-title index); rather than
    leave the session untitled (#50537), append a ``#N`` suffix via ``get_next_title_in_lineage``.
    """
    auto_fn = getattr(session_db, "set_auto_title", None)

    def _set(candidate):
        if auto_fn is not None:
            if auto_fn(session_id, candidate, source=source):
                return candidate
            logger.debug("Skipping %s title: a higher-authority title already holds session %s", source, session_id)
            return None
        legacy_fn = getattr(session_db, "set_auto_title_if_empty", None)  # older store without provenance
        if legacy_fn is not None:
            return candidate if legacy_fn(session_id, candidate) else None
        if session_db.set_session_title(session_id, candidate) is False:
            raise RuntimeError(f"session {session_id} not found when storing title")
        return candidate

    try:
        return _set(title)
    except ValueError:
        next_title_fn = getattr(session_db, "get_next_title_in_lineage", None)
        deduped = next_title_fn(title) if dedupe and next_title_fn is not None else None
        if not deduped or deduped == title:
            raise
        return _set(deduped)


def apply_instant_title(session_db, session_id: str, user_message: str, title_callback: Optional[TitleCallback] = None) -> Optional[str]:
    """Write the derived title inline. Returns it, or None (no usable text, or a ``derived``+ title exists). Never raises."""
    if not session_db or not session_id:
        return None
    try:
        title = derive_title(user_message) if is_titleable_user_message(user_message) else None
        persisted = _persist_session_title(session_db, session_id, title, source="derived", dedupe=False) if title else None
        if persisted:
            _notify_title(
                title_callback, persisted, "derived", "Instant-title", display_title=title
            )
        return persisted
    except Exception:
        logger.debug("Instant title failed", exc_info=True)
        return None


def auto_title_session(
    session_db,
    session_id: str,
    user_message: str,
    failure_callback: Optional[FailureCallback] = None,
    main_runtime: dict = None,
    title_callback: Optional[TitleCallback] = None,
    runtime_validator: Optional[RuntimeValidator] = None,
) -> None:
    """Generate and store the model title (daemon-thread target); skips sessions already carrying an
    ``llm``/``user`` title (a ``derived`` one is expected — upgrading it is the point). Never lets an
    exception escape (the threading excepthook would spray a traceback into the terminal); the canonical
    trigger is the post-``hermes update`` window where lazy imports read NEW source against OLD modules."""
    try:
        if not session_db or not session_id or _has_upgraded_title(session_db, session_id):
            return
        # This thread starts AFTER the turn's ambient context was reset; republish it so the call carries
        # the same Portal ``conversation=`` tag (root-of-lineage) and bills usage to this session.
        from agent.aux_accounting import set_accounting_context
        from agent.portal_tags import set_conversation_context
        conversation_id = session_id
        with suppress(Exception):
            conversation_id = session_db.get_conversation_root(session_id) or session_id
        set_conversation_context(conversation_id)
        # Same for the accounting context, so the title call's token usage is recorded against this session
        # (task='title_generation', #23270).
        set_accounting_context(session_db, session_id)
        recent_titles = []
        with suppress(Exception):
            list_recent = getattr(session_db, "list_recent_session_titles", None)
            if callable(list_recent):
                recent_titles = list_recent(exclude_session_id=session_id, limit=20)
        title, source = generate_title(
            user_message, failure_callback=failure_callback, main_runtime=main_runtime,
            runtime_validator=runtime_validator, recent_titles=recent_titles,
        ), "llm"
        if not title:  # the inline attempt declined collisions; off the critical path the lineage scan is affordable
            title, source = derive_title(user_message), "derived"
        if not title:
            return
        try:
            persisted = _persist_session_title(session_db, session_id, title, source=source)
        except Exception as e:
            logger.debug("Failed to set auto-generated title: %s", e)
            return
        if persisted is not None:
            logger.debug("Auto-generated session title: %s", persisted)
            _notify_title(
                title_callback, persisted, source, "Auto-title", display_title=title
            )
    except Exception as e:
        # WARNING so operators see it in agent.log; names the likely cause.
        logger.warning("Auto-title failed (harmless; if this started after an update, restart the running Hermes process): %s", e)
        logger.debug("Auto-title traceback", exc_info=True)
        _report_failure(failure_callback, e, "Auto-title")


def _is_real_user_turn(message: Any) -> bool:
    """A question a person actually asked (Hermes persists machinery under ``role="user"``)."""
    if not isinstance(message, dict) or message.get("role") != "user":
        return False
    content = message.get("content")
    return is_titleable_user_message(content if isinstance(content, str) else flatten_message_text(content))


def _session_is_untitled(session_db, session_id: str) -> bool:
    """No title of any provenance; False when it can't tell (no model call per turn for an unreadable title)."""
    getter = getattr(session_db, "get_session_title", None)
    try:
        return callable(getter) and not str(getter(session_id) or "").strip()
    except Exception:
        logger.debug("Untitled check failed for %s", session_id, exc_info=True)
        return False


def maybe_auto_title(
    session_db,
    session_id: str,
    user_message: str,
    conversation_history: Optional[list] = None,
    failure_callback: Optional[FailureCallback] = None,
    main_runtime: dict = None,
    title_callback: Optional[TitleCallback] = None,
    runtime_validator: Optional[RuntimeValidator] = None,
) -> None:
    """Instant inline title, then a daemon-thread upgrade. Call at the START of a turn, before the model."""
    if not session_db or not session_id or not user_message:
        return
    # History may be pre- or post-message. Skip only when BOTH past the opening turn AND named: count alone
    # left a machinery-opened session nameless; title alone never titles on an old store.
    user_msg_count = sum(1 for m in (conversation_history or []) if _is_real_user_turn(m))
    if (user_msg_count > 1 and not _session_is_untitled(session_db, session_id)) or not is_titleable_user_message(user_message):
        return
    if not _auto_title_enabled():  # config read after the cheap guards so the file isn't touched every turn
        logger.debug("Auto-title skipped: auxiliary.title_generation.enabled=false")
        return
    apply_instant_title(session_db, session_id, user_message, title_callback)
    threading.Thread(
        target=auto_title_session,
        args=(session_db, session_id, user_message),
        kwargs=dict(failure_callback=failure_callback, main_runtime=main_runtime, title_callback=title_callback, runtime_validator=runtime_validator),
        daemon=True,
        name="auto-title",
    ).start()
