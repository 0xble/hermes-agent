"""Carry MoA slot policy to the shared auxiliary fallback executor."""
from __future__ import annotations

from typing import Any, Callable


def slot_fallback_kwargs(
    slot: dict[str, Any], selected: dict[str, Any], runtime: dict[str, Any], prepare: Callable,
    *, aggregator: bool = False,
) -> dict[str, Any]:
    """A destination change replans canonical input and exposes its real accounting route."""
    from agent.moa_loop import _aggregator_reasoning_config, _slot_reasoning_config, _slot_runtime

    def transform(destination, messages, tools, reasoning_config, entry):
        candidate = dict(entry or {})
        selected.clear()
        selected.update(candidate, model=destination.model)
        selected.setdefault("provider", destination.provider)
        runtime.clear()
        runtime.update(_slot_runtime(selected), provider=selected["provider"], model=destination.model,
                       base_url=destination.base_url, api_mode=destination.api_mode)
        messages, tools = prepare(selected, runtime)
        reasoning = _aggregator_reasoning_config(selected) if aggregator else _slot_reasoning_config(selected)
        return messages, tools, reasoning

    return {"fallback_chain": slot.get("fallback_models"), "fallback_request_transform": transform}
