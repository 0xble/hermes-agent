"""Telegram progress formatting and daily-pause explanation."""

import re
from typing import Optional

from gateway.platforms.base import OUTBOUND_PROGRESS, utf16_len

_BACKTICK_RUN_RE = re.compile(r'`+')


def _progress_code_span(text: str) -> str:
    """``text`` as one CommonMark inline code span: a backtick run longer than any inside it, padded
    with a space when it starts or ends with a backtick (or with a space on both sides, which
    CommonMark would otherwise strip). Newlines fold to spaces, as a code span renders them anyway."""
    text = " ".join(str(text).split("\n"))
    if not text.strip():
        return text
    fence = "`" * (max((len(run) for run in _BACKTICK_RUN_RE.findall(text)), default=0) + 1)
    pad = " " if text[0] == "`" or text[-1] == "`" or (text[0] == " " and text[-1] == " ") else ""
    return f"{fence}{pad}{text}{pad}{fence}"


class TelegramProgressMixin:
    def progress_edit_pause_reason(self, chat_id: str) -> Optional[str]:
        """Optional adapter capability for the turn's skipped-edit explanation."""
        return "daily budget" if self._daily_sheds(chat_id, OUTBOUND_PROGRESS) else None

    def format_progress_text(self, chat_id: str, text: str) -> str:
        """Explain a frozen first bubble without spending another Telegram call."""
        if self.progress_edit_pause_reason(chat_id):
            notice = "\nLive updates paused (daily Telegram limit)"
            # The policy sheds split tails too, so reserve the notice inside the first bubble.
            text = self.truncate_message(
                text, self.MAX_MESSAGE_LENGTH - utf16_len(self.format_message(notice)),
                len_fn=lambda value: utf16_len(self.format_message(value)),
                indicator_overhead=self.CHUNK_INDICATOR_OVERHEAD,
            )[0]
            return text + notice
        return text

    def format_progress_literal(self, text: str) -> str:
        """Tool names and previews in progress lines are code spans, so neither the rich Markdown
        parser nor ``format_message`` reads ``mcp__a__b``, ``**/*.md``, ``README.md`` or ``\\s+``
        as markup or a link."""
        return _progress_code_span(text)
