"""Identity of a stalled compression attempt, shared by the stall-backoff recorders."""

from __future__ import annotations

import contextlib
from typing import Any, Optional


def _stall_source_fingerprint(agent: Any, messages: Any, approx_tokens: Optional[int]) -> str:
    """Identity of the stalled source context + summary strategy."""
    compressor = getattr(agent, "context_compressor", None)
    model = getattr(compressor, "summary_model", None) or getattr(agent, "model", None) or ""
    n_messages = len(messages) if isinstance(messages, list) else 0
    tokens = 0
    with contextlib.suppress(TypeError, ValueError):
        tokens = int(approx_tokens or 0)
    return f"msgs={n_messages}:tokens={tokens}:model={model}"
