"""Browser exec result projection, with original CLI patch seams late-bound."""

def exec_result(proc, task_id, workspace, session, route_key, started):
    from agent.redact import redact_sensitive_text
    from tools.registry import tool_result
    from tools.browser_use_cli import (_take_real_profile_note, _STDERR_CAP_CHARS,
                                       _find_screenshot, _native_screenshot_result)
    vault_tab = task_id or "default"
    result = {
        "success": proc.returncode == 0,
        "exit_code": proc.returncode,
        "output": redact_sensitive_text(proc.stdout, force=True, vault_tab=vault_tab),
    }
    if workspace:
        result["workspace"] = workspace
    if session:
        result["session"] = session
    result.update(_take_real_profile_note(route_key))
    stderr = redact_sensitive_text((proc.stderr or "").strip(), force=True, vault_tab=vault_tab)
    if len(stderr) > _STDERR_CAP_CHARS:
        stderr = stderr[:_STDERR_CAP_CHARS] + "\n… (stderr truncated)"
    if stderr:
        result["stderr"] = stderr
    screenshot = _find_screenshot(proc.stdout, started)
    if screenshot:
        result["screenshot_path"] = screenshot
        native = _native_screenshot_result(result, screenshot)
        if native is not None:
            return native
    return tool_result(result)
