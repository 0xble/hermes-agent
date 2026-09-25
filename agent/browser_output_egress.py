"""Final model-visible browser result boundary for session-scoped protected date components.

Individual browser backends still redact binary data and known structured control nodes.
This boundary applies to every browser tool, including future registry entries and
plugin-transformed results, before hooks or the model can observe them.
"""

import json
from typing import Any


def scrub_browser_result(name: str, result: Any, task_id: str | None) -> Any:
    if not name.startswith("browser_"):
        return result
    from agent.redact import (has_vault_date_components, redact_registered_vault_number,
                              redact_registered_vault_snapshot, redact_sensitive_text)

    task = task_id or "default"
    try:
        from tools.browser_tool import _last_session_key
        tab = _last_session_key(task)
    except Exception:
        tab = task
    if not has_vault_date_components(tab):
        return result

    parsed = result
    serialized = isinstance(result, str)
    if serialized:
        try:
            parsed = json.loads(result)
        except (ValueError, TypeError):
            pass

    # Protection lives until the browser session closes. An observed origin is not
    # proof the protected page is gone: the session may simply have focused another
    # tab while the filled one stays open, so no origin probe may clear it here.

    def scrub(value: Any, key: str = "") -> Any:
        if isinstance(value, str):
            if key == "snapshot":
                return redact_registered_vault_snapshot(value, tab=tab)
            return redact_sensitive_text(value, force=True, vault_tab=tab)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return redact_registered_vault_number(value, tab=tab)
        if isinstance(value, dict):
            return {scrub(k): scrub(v, str(k)) for k, v in value.items()}
        if isinstance(value, list):
            return [scrub(v, key) for v in value]
        if isinstance(value, tuple):
            return tuple(scrub(v, key) for v in value)
        return value

    cleaned = scrub(parsed)
    return json.dumps(cleaned, ensure_ascii=False) if serialized and parsed is not result else cleaned
