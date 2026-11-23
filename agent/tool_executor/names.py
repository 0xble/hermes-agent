'Tool-call naming helpers.'

from typing import Any


def _tc_name(tool_call: Any) -> str:
    return getattr(getattr(tool_call, "function", None), "name", "") or "tool"
