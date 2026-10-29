"""Recognize local interrupt diagnostics before transcript persistence or chat delivery."""

import re

from agent.message_sanitization import INTERRUPTED_TAIL_MARKER


_INTERRUPT_DIAGNOSTIC = re.compile(
    r"Operation interrupted(?:\.|: (?:waiting for model response|handling API error|"
    r"retrying API call after error|waiting for the provider to recover|"
    r"retrying empty response from model) \([^\n]{1,2000}\)\.|"
    r" during retry \([^\n]{1,2000}\)\.)"
)


def is_interrupt_diagnostic(text: object) -> bool:
    """Only complete, known local diagnostic shapes are disposable."""
    return isinstance(text, str) and (
        text.strip() == INTERRUPTED_TAIL_MARKER
        or _INTERRUPT_DIAGNOSTIC.fullmatch(text.strip()) is not None
    )
