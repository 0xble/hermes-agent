"""Inbound message event types shared by every gateway platform adapter.

A leaf module: adapters, helpers and the runner import it, so it must not import from
gateway.platforms.*.
"""

import re
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, Dict, List, Optional

from gateway.session import SessionSource

# Desktop attachment reference tags prepended by buildContextText before the
# user's visible text (e.g. "@image:/tmp/foo.png\n\n/moa ask something").
# Strip these when detecting slash commands so a media-ref prefix does not hide
# a slash token from MessageEvent.is_command() / get_command().
# The pattern matches to end-of-line (not just whitespace-bounded) to handle
# Windows paths that may contain spaces (e.g. "C:\Users\John Doe\image.png").
_ATTACHMENT_REF_RE = re.compile(r"^(?:@(?:image|file|url):[^\n]+\n?)+", re.IGNORECASE)

GOAL_CONTINUATION_METADATA_KEY = "goal_continuation"
TURN_ORIGINS = frozenset({"user", "relay", "process", "goal_loop", "cron", "unknown"})


def _event_chain(event: Any) -> list[Any]:
    """Return an event and any events merged into its turn, newest event first."""
    if event is None:
        return []
    return [event, *(getattr(event, "_hermes_absorbed_events", None) or ())]


# Machine ingress: deliveries from third-party systems, never a person typing.
_MACHINE_PLATFORMS = frozenset({"webhook", "msgraph_webhook", "wecom_callback"})


def _is_machine_ingress(source: Any) -> bool:
    platform = getattr(getattr(source, "platform", None), "value", getattr(source, "platform", None))
    return (str(platform or "").lower() in _MACHINE_PLATFORMS
            or str(getattr(source, "chat_type", "") or "").lower() == "webhook"
            or str(getattr(source, "user_id", "") or "").startswith("webhook:"))


# Gateway-stamped notification provenance; an event carrying one is never human ingress.
_NOTIFICATION_ORIGINS = frozenset({
    "relay_delivery", "cron_delivery", "process_registry_synthetic", "delegation_result",
})
# Boolean metadata stamps the gateway sets on synthetic turns.
_SYNTHETIC_MARKERS = ("cron_delivery", "loop_wakeup", "hermes_loop_wakeup")
# Relay deliveries that arrive as ordinary chat messages (for example through the owner's own
# Telegram account) carry only this footer, on their last non-empty line.
_RELAY_FOOTER_RE = re.compile(r"\[relay from=[^\s\]]+ receipt=[^\s\]]+(?: task=[^\s\]]+)?\]")


def _has_relay_footer(event: Any) -> bool:
    lines = [line.strip() for line in str(getattr(event, "text", "") or "").splitlines() if line.strip()]
    return bool(lines) and _RELAY_FOOTER_RE.fullmatch(lines[-1]) is not None


def _is_relay_event(event: Any) -> bool:
    return (bool(getattr(getattr(event, "source", None), "delivered_via_upstream_relay", False))
            or (getattr(event, "metadata", None) or {}).get("notification_origin") == "relay_delivery"
            or _has_relay_footer(event))


def _is_human_event(event: Any) -> bool:
    """Recognize positively identified human ingress, default-deny."""
    if event is None or getattr(event, "internal", False):
        return False
    source = getattr(event, "source", None)
    if source is None:
        return False
    if _is_relay_event(event):
        return False
    if _is_machine_ingress(source):
        return False
    if getattr(source, "is_bot", False):
        return False
    if not getattr(event, "allow_gateway_control", True):
        return False
    if getattr(event, "_heartbeat_session_id", None):
        return False
    metadata = getattr(event, "metadata", None) or {}
    if metadata.get("notification_origin") in _NOTIFICATION_ORIGINS or any(
        metadata.get(marker) for marker in _SYNTHETIC_MARKERS
    ):
        return False
    return not is_goal_continuation_event(event)


def turn_origin_for_event(event: Any) -> str:
    """Classify a gateway turn, preserving human input when events are merged."""
    events = _event_chain(event)
    if any(_is_human_event(candidate) for candidate in events):
        return "user"
    if any(_is_relay_event(candidate) for candidate in events):
        return "relay"
    if any(is_goal_continuation_event(candidate)
           or bool((getattr(candidate, "metadata", None) or {}).get("loop_wakeup"))
           or bool((getattr(candidate, "metadata", None) or {}).get("hermes_loop_wakeup"))
           or bool(getattr(candidate, "_heartbeat_session_id", None))
           for candidate in events):
        return "goal_loop"
    if any((getattr(candidate, "metadata", None) or {}).get("notification_origin") == "cron_delivery"
           or bool((getattr(candidate, "metadata", None) or {}).get("cron_delivery"))
           for candidate in events):
        return "cron"
    if any((getattr(candidate, "metadata", None) or {}).get("notification_origin") in {
        "process_registry_synthetic", "delegation_result",
    } for candidate in events):
        return "process"
    return "unknown"


def is_goal_continuation_event(event_or_text: Any) -> bool:
    """Recognize stamped continuations and legacy text (which must fail the identity fence)."""
    metadata = getattr(event_or_text, "metadata", None)
    if isinstance(metadata, dict):
        return bool(metadata.get(GOAL_CONTINUATION_METADATA_KEY))
    # A live MessageEvent without the provenance stamp is ordinary ingress, even when its
    # text quotes a legacy continuation prompt. Raw strings remain supported for persisted
    # rows and text-only compatibility callers.
    if hasattr(event_or_text, "metadata"):
        return False
    from hermes_cli.goals import is_goal_continuation_text
    return is_goal_continuation_text(str(getattr(event_or_text, "text", event_or_text) or ""))


class MessageType(Enum):
    """Types of incoming messages."""
    TEXT = "text"
    LOCATION = "location"
    PHOTO = "photo"
    VIDEO = "video"
    AUDIO = "audio"
    VOICE = "voice"
    DOCUMENT = "document"
    STICKER = "sticker"
    COMMAND = "command"  # /command style


class ProcessingOutcome(Enum):
    """Result classification for message-processing lifecycle hooks."""
    SUCCESS = "success"
    FAILURE = "failure"
    CANCELLED = "cancelled"


@dataclass
class MessageEvent:
    """Incoming message from a platform — the normalized shape all adapters produce."""
    text: str
    message_type: MessageType = MessageType.TEXT
    # Author, mirrored from ``source`` for per-message prompt builders; None for non-IM sources.
    user_id: Optional[str] = None
    user_name: Optional[str] = None
    # None only in isolated unit tests; production always sets it. Typing it Optional
    # exposes ~60 unguarded ``.source.<attr>`` reads, so that is a separate change.
    source: SessionSource = None
    raw_message: Any = None
    message_id: Optional[str] = None
    # Delivery-ledger identity for the final send, when it differs from ``message_id``. A queued
    # (/queue) chain answers the LAST message of the chain, so its final send has to be ledgered
    # under that message's id. Keyed on the opening event's id instead, two chained turns carrying
    # the same text collide on one obligation id and the earlier turn's row is overwritten (a
    # refused first reply then reads as delivered). Reply routing is unaffected: the reply anchor
    # still comes from this event.
    ledger_message_id: Optional[str] = None
    # Reply anchor for the final send when the answer is to a DIFFERENT message than the one that
    # opened the turn: a successful busy redirect turns the running turn onto the redirecting
    # message, so its reply must quote that message (#115001). ``_reply_anchor_for_event``
    # honours this over ``message_id``; None = derive from the event as usual.
    reply_anchor_override: Optional[str] = None
    # Platform update id (Telegram ``update_id``): ``/restart`` records it so the new gateway
    # advances past it even if PTB's shutdown ACK times out.
    platform_update_id: Optional[int] = None
    # Media attachments: local file paths (for vision tool access)
    media_urls: list[str] = field(default_factory=list)
    media_types: list[str] = field(default_factory=list)
    # Per-attachment text-inlining contract; None = legacy "text/* already inlined into ``text``".
    media_text_inlined: list[Optional[bool]] = field(default_factory=list)
    reply_to_message_id: Optional[str] = None
    reply_to_text: Optional[str] = None  # Text of the replied-to message (for context injection)
    reply_to_author_id: Optional[str] = None
    reply_to_author_name: Optional[str] = None
    reply_to_is_own_message: bool = False  # True when the user replied to this bot/assistant's message
    # Structured interactive-prompt reply (relay only): {prompt_id, option_id, label?,
    # prompt_message_id?}; routed to the approval/slash-confirm/clarify resolvers BEFORE dispatch.
    prompt_response: Optional[dict[str, Any]] = None
    # Auto-loaded skill(s) for topic/channel bindings; a single name or ordered list.
    auto_skill: Optional[str | list[str]] = None
    # Per-channel ephemeral system prompt; applied at API call time, never persisted to transcript.
    channel_prompt: Optional[str] = None
    # History-backfilled channel context (missed under require_mention); kept out of ``text`` so
    # run.py's sender-prefix logic sees only the trigger message.
    channel_context: Optional[str] = None
    # Set for synthetic events (e.g. background-process notifications) that must bypass user authorization.
    internal: bool = False
    # Free-form per-event metadata (e.g. ``whatsapp_from_owner=True``); plugins must ``.get()``.
    metadata: dict[str, Any] = field(default_factory=dict)
    timestamp: datetime = field(default_factory=datetime.now)
    # May this event resolve gateway commands / control prompts? Proactive plugin events set False
    # so untrusted payload text stays conversational. New fields append after it (positional compat).
    allow_gateway_control: bool = True
    # Was this message addressed to this bot? False lets a bare silence marker stand (the adapter
    # knows the message was meant for someone else); None means unknown and keeps the visible
    # fallback, like True.
    reply_expected: Optional[bool] = None
    # Set for synthetic turns that carry no channel inputs of their own (goal continuation,
    # heartbeat due-prompts, /goal resume) so the turn reuses the session's pinned channel
    # prompt/parent instead of overwriting it with this event's own None (#126109). Independent
    # of ``internal``: these events still go through user authorization. Appended after
    # ``reply_expected`` for positional compat (see the field-order rule above).
    inherit_channel_pin: bool = False
    # Identity of one edit of a message (Telegram: its ``update_id``). An edit re-sends its message's own
    # ``message_id`` with new text, so native admission keys it apart from the original send.
    # Appended for positional compat (see the field-order rule above).
    edit_revision: Optional[str] = None

    # Process-local admission receipt, never routing metadata or execution acknowledgement.
    _gateway_accepted: bool = field(default=False, init=False, repr=False, compare=False)
    # The local inbox placeholder belongs to this event until native admission succeeds.
    _owned_local_pending: Optional[int] = field(default=None, init=False, repr=False, compare=False)
    # Set when ``admit_internal_event`` hands its producer a receipt before any drain began. The
    # producer will not retry, so a restart drain runs this event instead of refusing it at the
    # drain gate or discarding it as a queued follow-up (#138123).
    _admitted_before_drain: bool = field(default=False, init=False, repr=False, compare=False)
    # Run-owned final presentation snapshot; never deserialized from ingress metadata.
    _notification_reply_muted: Optional[bool] = field(default=None, init=False, repr=False, compare=False)
    # Run-owned identity of a queued chain's terminal turn, for the chain's single post-turn judge.
    _post_turn_goal_identity: Optional[dict] = field(default=None, init=False, repr=False, compare=False)

    def absorb_reply_expected(self, other: "MessageEvent") -> None:
        """One turn now answers *other*: addressed human input wins over autonomous unknown input."""
        self_goal = bool((self.metadata or {}).get(GOAL_CONTINUATION_METADATA_KEY))
        other_goal = bool((other.metadata or {}).get(GOAL_CONTINUATION_METADATA_KEY))
        if self_goal:
            if other.reply_expected is True:
                self.reply_expected = True
            return
        if other_goal and self.reply_expected is None:
            return
        if self.reply_expected is not True and other.reply_expected is not False:
            self.reply_expected = other.reply_expected

    def _command_text(self) -> str:
        """Return the message text with leading Desktop attachment refs stripped.

        Desktop's buildContextText prepends ``@image:<path>``, ``@file:<path>``,
        or ``@url:<url>`` tags before the user's visible text.  Stripping these
        lets is_command / get_command / get_command_args work correctly even
        when the payload is prefixed with one or more media refs.
        """
        return _ATTACHMENT_REF_RE.sub("", (self.text or "").lstrip()).lstrip()

    def is_command(self) -> bool:
        """Check if this is a command message (e.g., /new, /reset)."""
        return self.allow_gateway_control and self._command_text().startswith("/")

    def get_command(self) -> Optional[str]:
        """Extract command name if this is a command message."""
        if not self.is_command():
            return None
        raw = self._command_text().split(maxsplit=1)[0][1:].lower().split("@", 1)[0]
        # Reject file paths: valid command names never contain /
        return None if "/" in raw else raw

    def get_command_args(self) -> str:
        """Get the arguments after a command."""
        if not self.is_command():
            return self.text
        parts = self._command_text().lstrip().split(maxsplit=1)
        args = parts[1] if len(parts) > 1 else ""
        # iOS auto-corrects -- to — (em dash) and - to – (en dash)
        return args.replace("\u2014\u2014", "--").replace("\u2014", "--").replace("\u2013", "-")
