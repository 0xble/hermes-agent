"""Captured subprocess execution and output for tools RPCs.

Bodies bind to server globals through the existing method context seam.
"""

from .method_ctx import bind_module


def _exec_out(rid, output: str) -> dict:
    """command.dispatch display-only result."""
    return _ok(rid, {"type": "exec", "output": output})


def _capture_run_kwargs(timeout: int) -> dict:
    """Shared captured-text subprocess.run kwargs: UTF-8 + lossy decode (non-UTF-8 child output must
    not crash the gateway thread on Windows), no stdin, no console flash under the desktop parent."""
    return dict(
        capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout,
        stdin=subprocess.DEVNULL, creationflags=_tools_mod("hermes_cli._subprocess_compat").windows_hide_flags())


def _captured_exec(rid, cmd, timeout: int, *, on_result, timeout_err: tuple, fail_code: int,
                   shell: bool = False, env: dict | None = None) -> dict:
    """Run ``cmd`` captured (see ``_capture_run_kwargs``) and hand the CompletedProcess to
    ``on_result``; TimeoutExpired → ``timeout_err`` (code, message), other errors → ``fail_code``."""
    try:
        return on_result(subprocess.run(cmd, cwd=os.getcwd(), shell=shell, env=env, **_capture_run_kwargs(timeout)))
    except subprocess.TimeoutExpired:
        return _err(rid, *timeout_err)
    except Exception as e:
        return _err(rid, fail_code, str(e))


def _joined_output(r) -> str:
    """stdout + stderr of a CompletedProcess, non-empty parts only, newline-joined and stripped."""
    return "\n".join(p for p in (r.stdout or "", r.stderr or "") if p).strip()



def _dispatch_quick(rid, params, session, name, arg):
    qc = _load_cfg().get("quick_commands", {}).get(name)
    if qc is None:
        return None
    if qc.get("type") == "exec":
        # Sanitized env: the TUI server process holds every API key in os.environ.
        env = _tools_mod("tools.environments.local").build_subprocess_env()
        r = subprocess.run(qc.get("command", ""), shell=True, env=env, **_capture_run_kwargs(30))
        output = _joined_output(r)[:4000]
        output = _tools_mod("agent.redact").redact_sensitive_text(output) if output else output
        if r.returncode != 0:
            return _err(rid, 4018, output or f"quick command failed with exit code {r.returncode}")
        return _exec_out(rid, output)
    return _ok(rid, {"type": "alias", "target": qc.get("target", "")}) if qc.get("type") == "alias" else None


def register(server) -> None:
    bind_module(globals(), server)
