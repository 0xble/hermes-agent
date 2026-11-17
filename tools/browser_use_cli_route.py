"""Preparation phase for the Browser Use CLI execution route."""

from __future__ import annotations

import json
import time
from typing import Any, Optional


def prepare_browser_exec(code: str, session: str, timeout_s: int,
                         task_id: Optional[str], local: bool) -> dict[str, Any]:
    """Validate code and resolve one browser route before the CLI daemon starts.

    The late import keeps ``browser_use_cli`` the facade for its internal seams and
    avoids a module-level cycle.
    """
    from tools import browser_use_cli as cli
    from tools.registry import tool_error, tool_result
    from tools.browser_tool_vision import blocked_protected_date_pixels

    blocked_pixels = blocked_protected_date_pixels(task_id or "default")
    if blocked_pixels is not None:
        return {"error": blocked_pixels}
    if not code or not code.strip():
        return {"error": tool_error(
            "No code provided. Pass Python that uses the pre-imported helpers, e.g. "
            "new_tab(\"https://example.com\") then print(page_info()).")}

    verdict = cli._code_url_verdict(code)
    if verdict.blocked:
        return {"error": tool_error(verdict.blocked)}
    route_key = cli._backend_cache_key(task_id, session)
    local_sidecar = verdict.local_sidecar if cli._URL_RE.search(code) else cli._exec_route_is_sidecar.get(route_key, False)

    cmd = cli._find_cli()
    if not cmd:
        if cli._cloud_refuses_pinned_target():
            from tools import browser_tool_cloud as cloud
            return {"error": tool_result(cloud.browser_target_error(
                RuntimeError("browser-harness is unavailable"), cloud.browser_target()))}
        return {"error": tool_error("browser-harness is missing from Hermes's Python environment. "
                                     "Run `hermes update` to re-sync it.")}

    env = cli._base_subprocess_env()
    if session:
        if not cli._SESSION_RE.match(session):
            return {"error": tool_error(f"Invalid session name {session!r}: use 1-64 letters, digits, "
                                         "dashes, or underscores (e.g. 'r7k2').")}
        env["BU_NAME"] = session
    env, route_err, local_sidecar, generation = cli._route_exec_current(
        env, session, task_id, bool(local), local_sidecar, route_key)
    if route_err:
        try:
            route_payload = json.loads(route_err)
        except (TypeError, json.JSONDecodeError):
            route_payload = None
        if isinstance(route_payload, dict) and route_payload.get("error_type") == "browser_target_unavailable":
            return {"error": tool_result(route_payload)}
        return {"error": tool_error(route_err)}
    if local_sidecar:
        env["BU_NAME"] = cli._sidecar_daemon_name(session)
    cli.fit_default_socket_dir(env)
    cli._exec_route_is_sidecar[route_key] = local_sidecar
    bot_desktop_browser = bool(env.pop(cli._BOT_DESKTOP_BROWSER_SENTINEL, None))
    private_browser = env.pop(cli._PRIVATE_BROWSER_SENTINEL, None)
    if session and not private_browser:
        code = cli._OWN_TAB_PREAMBLE + code
    from tools.browser_use_shot import ACTIVATE_NEVER, harness_command
    command = harness_command(cmd, activate=env.pop(cli._CLOUD_BROWSER_SENTINEL, None) or ACTIVATE_NEVER)

    workspace = cli._workspace_dir(task_id)
    if workspace:
        env["BH_AGENT_WORKSPACE"] = workspace
    if "BU_AUTOSPAWN" not in env and not cli._cloud_refuses_pinned_target() \
            and cli.is_legacy_browser_use_cloud_config(cli._read_browser_cfg()):
        env["BU_AUTOSPAWN"] = "1"
    return {
        "code": code,
        "cmd": command,
        "env": env,
        "route_key": route_key,
        "local_sidecar": local_sidecar,
        "generation": generation,
        "bot_desktop_browser": bot_desktop_browser,
        "timeout": cli._clamp_timeout(timeout_s),
        "workspace": workspace,
    }
