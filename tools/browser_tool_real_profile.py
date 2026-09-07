"""Real-profile local browsing: snapshot the user's default Chromium profile into a
hermes-owned copy, launch the real browser binary on it, and attach agent-browser.

State (``_REAL_PROFILE_SESSION``, ``_real_profile_cdp_lock``, ``_real_profile_cdp_cache``,
``_real_profile_chrome_procs``) lives in ``tools.browser_tool``; it is read
through ``_bt`` (resolved per call — never import ``tools.browser_tool`` at import time).
"""

import os
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import Optional, Tuple
from hermes_cli._subprocess_compat import windows_hide_flags
from tools.browser_tool_origin import origin_module as _origin
from tools import browser_tool_cloud as _cloud
from tools import browser_tool_install as _install
from tools import browser_tool_lightpanda_fallback as _lp
from tools import browser_tool_session as _session

_RP = "browser.use_real_profile is on, but "


def _terminate_real_profile_chrome() -> None:
    """Terminate real-browser processes launched for real-profile sessions (idempotent, atexit-safe);
    agent-browser only ATTACHED to them, so its own session cleanup never kills them."""
    from tools.browser_lightpanda import _terminate
    _bt = _origin()
    while _bt._real_profile_chrome_procs:
        _terminate(_bt._real_profile_chrome_procs.pop(), what="real-profile chrome")


def _cdp_http_ready(http_cdp: str) -> bool:
    """True when an ``http://host:port`` CDP discovery root answers."""
    from tools.browser_lightpanda import _cdp_ready
    return _cdp_ready(http_cdp, timeout=1.0)


def _agent_browser_session_cmd(session_name: str, *cmd: str, log_label: str) -> Optional[subprocess.CompletedProcess]:
    """Run ``agent-browser --session <name> <cmd...>``; None when agent-browser is missing or the run fails."""
    _bt = _origin()
    try:
        browser_cmd = _install._find_agent_browser()
    except FileNotFoundError:
        return None
    try:
        return subprocess.run([*_session._agent_browser_argv(browser_cmd), "--session", session_name, *cmd],
                              capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=15,
                              env=_bt._build_browser_env(), stdin=subprocess.DEVNULL)
    except (subprocess.SubprocessError, OSError) as e:
        _bt.logger.debug("real-profile %s failed: %s", log_label, e)
        return None


def _agent_browser_get_cdp(session_name: str) -> Optional[str]:
    """HTTP CDP discovery root of an agent-browser session (from its ``ws://`` cdp-url), or None."""
    proc = _agent_browser_session_cmd(session_name, "get", "cdp-url", log_label="get cdp-url")
    m = re.search(r"ws://127\.0\.0\.1:(\d+)/", (proc.stdout or "").strip()) if proc is not None else None
    return f"http://127.0.0.1:{m.group(1)}" if m else None


def _read_devtools_port(data_dir: str) -> Optional[str]:
    """First line of Chrome's ``DevToolsActivePort`` in ``data_dir`` (None when unreadable)."""
    try:
        with open(os.path.join(data_dir, "DevToolsActivePort"), encoding="utf-8") as fh:
            return fh.readline().strip()
    except OSError:
        return None


def _cdp_on_data_dir(http_cdp: str, data_dir: str) -> bool:
    """True when the CDP endpoint's browser runs on ``data_dir`` (DevToolsActivePort match proves it
    is our profile copy, not a throwaway temp dir a raced/stale launch fell back to)."""
    m = re.search(r":(\d+)", http_cdp or "")
    return bool(m) and _read_devtools_port(data_dir) == m.group(1)


def _agent_browser_close_session(session_name: str) -> None:
    """Best-effort close of an agent-browser session (stale/wrong-dir cleanup)."""
    _agent_browser_session_cmd(session_name, "close", log_label="session close")


_REAL_PROFILE_CHROME_FLAGS = (
    "--remote-debugging-port=0", "--no-first-run", "--no-default-browser-check",
    "--disable-background-networking", "--disable-component-update", "--disable-default-apps",
    "--disable-hang-monitor", "--disable-popup-blocking", "--disable-prompt-on-repost",
    "--disable-sync", "--disable-features=Translate", "--no-startup-window",
)


def _real_profile_unsupported_reason(browser) -> Optional[str]:
    """Fail-closed message when the default browser can't be used, else None.

    A pre-release channel lives in a profile dir we don't resolve; normalizing to the stable
    family would drive a DIFFERENT profile/account (wrong-principal bug), so refuse rather than guess.
    """
    from hermes_cli.browser_connect import UNSUPPORTED_CHANNEL
    if browser is None:
        return (_RP + "your default browser is not a supported Chromium browser (Chrome, Edge, Brave, "
                "Brave Origin, Chromium). Real-profile browsing requires a Chromium default; set one or turn the toggle off.")
    if browser == UNSUPPORTED_CHANNEL:
        return (_RP + "your default browser is a pre-release Chromium channel (Beta / Dev / Canary), which "
                "real-profile browsing does not support. Set your default to a "
                "stable Chrome / Edge / Brave / Brave Origin / Chromium, or turn the toggle off.")
    return None


def _real_profile_snapshot_error(err: str) -> str:
    """User-facing message for a failed profile snapshot; a locked profile adds the approved-close
    command, which the agent must ASK the user about first (it quits their browser)."""
    from hermes_cli.browser_connect import _PROFILE_LOCKED_PREFIX
    if err and err.startswith(_PROFILE_LOCKED_PREFIX):
        return (err[len(_PROFILE_LOCKED_PREFIX):] + " To close it (only after the user approves — it "
                "quits their browser and loses unsaved tabs), run: `hermes browser close-profile`, then retry.")
    return f"{_RP}{err}"


def _launch_real_profile_chrome(real_binary: str, copy_dir: str) -> Tuple[Optional[int], Optional[str]]:
    """Launch the user's REAL browser binary on the profile COPY; return (debug_port, error).

    agent-browser's own launch force-adds --use-mock-keychain / --password-store=basic, which makes
    macOS Chrome drop every keychain-encrypted cookie (signed-out copy); launching the real binary
    ourselves keeps the OS keychain path intact and agent-browser attaches via ``--cdp <port>``.
    Headless by default (a focus-stealing window defeats a background capability); Chrome's NEW
    headless shares the profile's cookie store (legacy --headless does not). browser.headed /
    AGENT_BROWSER_HEADED opts into a window, except on a display-less Linux host (launch would die).
    """
    _bt = _origin()
    try:
        os.unlink(os.path.join(copy_dir, "DevToolsActivePort"))  # stale port confuses reuse probes
    except OSError:
        pass
    chrome_argv = [real_binary, f"--user-data-dir={copy_dir}", *_REAL_PROFILE_CHROME_FLAGS]
    _has_display = bool(os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"))
    if not (_cloud._is_headed_mode() and (_has_display or not sys.platform.startswith("linux"))):
        chrome_argv.append("--headless=new")
    try:
        chrome_proc = subprocess.Popen(chrome_argv, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                       stdin=subprocess.DEVNULL, start_new_session=True, env=_bt._build_browser_env())
    except (subprocess.SubprocessError, OSError) as e:
        return None, f"{_RP}the launch failed: {e}"
    _bt._real_profile_chrome_procs.append(chrome_proc)

    deadline = time.monotonic() + 30.0
    while time.monotonic() < deadline:
        line = _read_devtools_port(copy_dir) or ""
        if line.isdigit():
            return int(line), None
        if chrome_proc.poll() is not None:
            _terminate_real_profile_chrome()
            return None, _RP + "Chrome exited during startup (another instance may hold the profile copy)."
        time.sleep(0.25)
    _terminate_real_profile_chrome()
    return None, _RP + "the real-profile browser did not expose a debug port in time. Retry, or turn the toggle off."


def _attach_agent_browser_to_real_profile(port: int, copy_dir: str) -> Tuple[Optional[str], Optional[str]]:
    """Make agent-browser ATTACH to the running Chrome (never launch its own); returns ``(http_cdp, error)``.

    The daemon may answer with the endpoint of a browser IT spawned (throwaway temp profile);
    the DevToolsActivePort OUR Chrome wrote is authoritative on disagreement.
    """
    _bt = _origin()
    try:
        browser_cmd = _install._find_agent_browser()
    except FileNotFoundError as e:
        return None, f"{_RP}the local browser engine (agent-browser) is not installed: {e}"
    argv = [*_session._agent_browser_argv(browser_cmd), "--session", _bt._REAL_PROFILE_SESSION,
            "--cdp", str(port), "open", "about:blank"]
    try:
        proc = subprocess.run(argv, capture_output=True, text=True, encoding="utf-8", errors="replace",
                              timeout=_bt._get_open_command_timeout(first_open=True), env=_bt._build_browser_env(),
                              stdin=subprocess.DEVNULL)
    except subprocess.TimeoutExpired:
        return None, _RP + "the real-profile browser took too long to start. Retry, or turn the toggle off."
    except (subprocess.SubprocessError, OSError) as e:
        return None, f"{_RP}the launch failed: {e}"
    if proc.returncode != 0:
        tail = (proc.stderr or proc.stdout or "").strip().splitlines()
        return None, f"{_RP}the real-profile browser failed to start: {tail[-1] if tail else f'exit {proc.returncode}'}"
    cdp = _agent_browser_get_cdp(_bt._REAL_PROFILE_SESSION)
    our_port = _read_devtools_port(copy_dir)
    if our_port is not None and (m := re.search(r":(\d+)", cdp or "")) and m.group(1) != our_port:
        cdp = f"http://127.0.0.1:{our_port}"
    if not cdp:
        return None, _RP + "the real-profile browser started without exposing a devtools endpoint. Retry, or turn the toggle off."
    return cdp, None


def _process_uses_data_dir(data_dir: str) -> bool:
    """True when a LIVE Chromium process owns ``data_dir``.

    A ready CDP endpoint alone does not prove the snapshot's own browser is behind it; without this
    an unrelated listener could be handed the identity's authenticated session.
    """
    try:
        import psutil
    except ImportError:
        return False
    expected = os.path.normcase(os.path.realpath(data_dir))
    for proc in psutil.process_iter(["cmdline"]):
        try:
            command = proc.info.get("cmdline") or []
        except (psutil.Error, OSError):
            continue
        for index, arg in enumerate(command):
            candidate = None
            if arg.startswith("--user-data-dir="):
                candidate = arg.partition("=")[2]
            elif arg == "--user-data-dir" and index + 1 < len(command):
                candidate = command[index + 1]
            if candidate and os.path.normcase(os.path.realpath(candidate)) == expected:
                return True
    return False


def _cdp_owned_by_data_dir(http_cdp: str, data_dir: str) -> bool:
    """Ready, port-matched, AND backed by a live process on the expected snapshot."""
    _bt = _origin()
    return (_bt._cdp_http_ready(http_cdp) and _bt._cdp_on_data_dir(http_cdp, data_dir)
            and _process_uses_data_dir(data_dir))


def _owned_profile_cdp(data_dir: str) -> Optional[str]:
    """Recover a live CDP endpoint owned by an isolated snapshot (survives a gateway crash)."""
    _bt = _origin()
    port = _read_devtools_port(data_dir)
    if not port or not port.isdigit() or not 0 < int(port) < 65536:
        return None
    cdp = f"http://127.0.0.1:{port}"
    if not _bt._cdp_http_ready(cdp) or not _bt._cdp_owned_by_data_dir(cdp, data_dir):
        return None
    return cdp


def _read_real_profile_headed_mode(copy_dir: str) -> Optional[bool]:
    """Persisted effective headed mode for a managed runtime, or None when unverifiable.

    Persisted rather than in-memory so cleanup after a gateway restart follows the mode the runtime
    is ACTUALLY in, not a contradictory global default (HERMES-091).
    """
    try:
        value = Path(copy_dir, ".hermes-browser-mode").read_text(encoding="utf-8").strip()
    except OSError:
        return None
    return {"headed": True, "headless": False}.get(value)


def _attach_agent_browser_to_cdp(session_name: str, cdp: str) -> Optional[str]:
    """Attach an identity-scoped agent-browser daemon to ``cdp``; error string or None."""
    _bt = _origin()
    match = re.search(r":(\d+)", cdp)
    if not match:
        return "invalid CDP endpoint"
    try:
        browser_cmd = _bt._find_agent_browser()
    except FileNotFoundError as exc:
        return f"local browser engine (agent-browser) is not installed: {exc}"
    argv = [*_session._agent_browser_argv(browser_cmd), "--session", session_name,
            "--cdp", match.group(1), "open", "about:blank"]
    try:
        proc = subprocess.run(
            argv, capture_output=True, text=True, encoding='utf-8', errors='replace',
            stdin=subprocess.DEVNULL,
            timeout=_bt._get_open_command_timeout(first_open=True),
            env=_bt._build_browser_env())
    except subprocess.TimeoutExpired:
        return "agent-browser took too long to attach"
    except (subprocess.SubprocessError, OSError) as exc:
        return f"agent-browser attach failed: {exc}"
    if proc.returncode != 0:
        tail = (proc.stderr or proc.stdout or "").strip().splitlines()
        return f"agent-browser failed to attach: {tail[-1] if tail else f'exit {proc.returncode}'}"
    return None


def _cleanup_real_profile_state() -> None:
    """Stop real-profile runtimes and delete every credential snapshot (consent revoked)."""
    _bt = _origin()
    _bt._close_all_real_profile_runtimes()
    try:
        from hermes_cli.browser_connect import cleanup_real_profile_snapshots
        cleanup_real_profile_snapshots()
    except Exception as e:
        _bt.logger.debug("real-profile cleanup-on-consent-off failed: %s", e)
    _bt._real_profile_cdp_cache.clear()
    _bt._real_profile_headed_modes.clear()


def _real_profile_cdp(requested_identity: Optional[str] = None, *, headed: Optional[bool] = None) -> tuple:
    """Resolve ``(cdp_url, error)`` for consented real-profile browsing.

    Snapshot -> launch the matching installed browser on the copy -> attach agent-browser over the
    snapshot-owned CDP endpoint. The copy is a non-default dir, so it sidesteps the Chrome >=136
    default-profile remote-debugging block and never contends with the user's running browser.

    HERMES-064: ``requested_identity`` selects one configured alias, and EVERY resource below —
    snapshot dir, agent-browser session, cross-process lock, CDP cache entry, Chromium process,
    Browser Use daemon — is keyed by that identity plus the active Hermes home, so two aliases
    never share a cookie jar. An unconfigured install (identity resolves to None) keeps the legacy
    ``profile.last_used`` path byte-for-byte.

    HERMES-091/092: ``headed`` binds the mode when a NEW runtime launches and fails closed against a
    live runtime in the other mode; the effective mode is persisted beside the snapshot so cleanup
    after a restart follows the runtime's real state.

    ``(None, message)`` fail-closed; ``(None, None)`` when consent is off.
    """
    _bt = _origin()
    if not _bt._use_real_profile():
        # Consent off: delete the snapshot store so revoking consent actually removes the
        # credential copies, and drop every per-identity runtime.
        _bt._cleanup_real_profile_state()
        return None, None

    # Lightpanda rejects ``--profile``; check BEFORE default-browser detection so a
    # host with no Chromium default still reports the actionable engine conflict.
    if _lp._using_lightpanda_engine():
        return None, (_RP + "browser.engine is set to 'lightpanda', which cannot load a real Chromium profile. "
                      "Set browser.engine to 'auto' or 'chrome' to use real-profile browsing, or turn the toggle off.")

    effective_headed = _bt._is_headed_mode() if headed is None else headed
    has_display = bool(os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"))
    wants_headed = effective_headed and (has_display or not sys.platform.startswith("linux"))

    from contextlib import nullcontext
    from hermes_cli.browser_connect import (chromium_executable, detect_default_chromium,
                                            real_profile_copy_dir, real_profile_data_dir,
                                            snapshot_real_profile)
    from hermes_cli.browser_identity import (BrowserIdentityError, BrowserIdentityProcessLock,
                                             resolve_browser_identity)

    try:
        identity = resolve_browser_identity(requested_identity)
    except BrowserIdentityError as exc:
        return None, str(exc)

    session_name, identity_lock, cache_key = _bt._real_profile_runtime_resources(identity)
    scoped_runtime_key = cache_key.rpartition(":")[2] if identity is not None else ""

    def mode_conflict() -> Optional[str]:
        running_headed = _bt._real_profile_headed_modes.get(cache_key)
        if running_headed is None:
            if headed is None:
                return None
            return ("The Hermes real-profile browser is already running, but its headed mode cannot "
                    "be verified. Close it and retry to apply an explicit headed value safely.")
        if running_headed == wants_headed:
            return None
        running, requested = ("headed" if running_headed else "headless",
                              "headed" if wants_headed else "headless")
        return (f"The Hermes real-profile browser is already running {running}; it cannot be reused "
                f"as {requested}. Close the existing browser session or use the same headed value, "
                "then retry.")

    # The in-process lock serializes this gateway; the file lock serializes other Hermes processes
    # racing the same snapshot. Only a configured identity has a snapshot to fence.
    process_lock = (BrowserIdentityProcessLock(scoped_runtime_key) if identity is not None
                    else nullcontext())

    with identity_lock, process_lock:
        cached = _bt._real_profile_cdp_cache.get(cache_key)
        if identity is None and cached and _bt._cdp_http_ready(cached):
            if conflict := mode_conflict():
                return None, conflict
            return cached, None

        browser = identity.browser if identity is not None else detect_default_chromium()
        if unsupported := _real_profile_unsupported_reason(browser):
            return None, unsupported

        # Resolve the copy dir as a PATH only (no copy) and probe reuse first. CRITICAL: the
        # snapshot overlay truncates/rewrites Cookies / Login Data and must NOT run while a live
        # copy-browser holds the user-data-dir open — that corrupts the databases.
        copy_dir = real_profile_copy_dir(
            browser,
            identity=identity.alias if identity is not None else None,
            source_profile=identity.source_profile if identity is not None else "")

        if identity is not None and cached:
            if _bt._cdp_owned_by_data_dir(cached, copy_dir):
                if conflict := mode_conflict():
                    return None, conflict
                return cached, None
            _bt._agent_browser_close_session(session_name)
            # A failed ownership probe does not prove the launched Chromium exited. Stop the tracked
            # snapshot owner before any later overlay can rewrite its credential databases.
            _bt._stop_real_profile_browser(cache_key)
        _bt._real_profile_cdp_cache.pop(cache_key, None)
        _bt._real_profile_headed_modes.pop(cache_key, None)
        if identity is not None:
            # Browser Use daemons retain their original CDP attachment; stop them before
            # relaunching this identity on a new endpoint.
            if not _bt._reload_browser_use_runtime(scoped_runtime_key):
                return None, ("could not stop the identity's stale Browser Use session; refusing to "
                              "relaunch it on a different CDP endpoint")

        existing = _bt._agent_browser_get_cdp(session_name)
        existing_owned = (
            _bt._cdp_owned_by_data_dir(existing, copy_dir) if identity is not None and existing
            else bool(existing and _bt._cdp_http_ready(existing) and _bt._cdp_on_data_dir(existing, copy_dir)))
        if existing and existing_owned:
            existing_headed = _read_real_profile_headed_mode(copy_dir)
            if existing_headed is None and headed is not None:
                return None, ("The Hermes real-profile browser is already running, but its headed mode "
                              "cannot be verified. Close it and retry to apply an explicit headed "
                              "value safely.")
            if existing_headed is not None and existing_headed != wants_headed:
                running, requested = ("headed" if existing_headed else "headless",
                                      "headed" if wants_headed else "headless")
                return None, (f"The Hermes real-profile browser is already running {running}; it cannot "
                              f"be reused as {requested}. Close the existing browser session or use the "
                              "same headed value, then retry.")
            _bt._real_profile_cdp_cache[cache_key] = existing
            if existing_headed is None:
                _bt._real_profile_headed_modes.pop(cache_key, None)
            else:
                _bt._real_profile_headed_modes[cache_key] = existing_headed
            _bt._track_real_profile_session(cache_key, session_name)
            # This gateway did not launch the recovered Chromium, but its verified managed data
            # directory is enough for owned-process fallback termination later.
            _bt._real_profile_browser_processes.setdefault(cache_key, (None, copy_dir))
            return existing, None
        if existing:  # stale/wrong-dir session: close it so nothing holds the dir open
            _bt._agent_browser_close_session(session_name)

        # A snapshot browser deliberately survives gateway restarts: recover it BEFORE any overlay.
        recovered = _owned_profile_cdp(copy_dir)
        if recovered:
            recovered_headed = _read_real_profile_headed_mode(copy_dir)
            if recovered_headed is None and headed is not None:
                return None, ("The Hermes real-profile browser is already running, but its headed mode "
                              "cannot be verified. Close it and retry to apply an explicit headed "
                              "value safely.")
            if recovered_headed is None:
                _bt._real_profile_headed_modes.pop(cache_key, None)
            else:
                _bt._real_profile_headed_modes[cache_key] = recovered_headed
            if conflict := mode_conflict():
                _bt._real_profile_headed_modes.pop(cache_key, None)
                return None, conflict
            if attach_error := _attach_agent_browser_to_cdp(session_name, recovered):
                return None, f"{_RP}{attach_error}"
            _bt._real_profile_cdp_cache[cache_key] = recovered
            _bt._real_profile_browser_processes[cache_key] = (None, copy_dir)
            _bt._track_real_profile_session(cache_key, session_name)
            return recovered, None

        # No live browser owns the dir now — safe to (re)snapshot + overlay.
        snap_dir, err = snapshot_real_profile(
            browser,
            src=real_profile_data_dir(browser) if identity is not None else None,
            source_profile=identity.source_profile if identity is not None else None,
            identity=identity.alias if identity is not None else None)
        if err or not snap_dir:
            return None, _real_profile_snapshot_error(err)
        copy_dir = snap_dir

        # Launch the EXACT installed browser family on the copy. agent-browser injects
        # --use-mock-keychain / --password-store=basic for absolute profile paths, which makes macOS
        # Chrome reject and delete cookies copied from the real profile; Hermes owns this process and
        # agent-browser only attaches to its CDP endpoint.
        executable = chromium_executable(browser)
        if not executable:
            return None, f"{_RP}the installed {browser} browser executable was not found"
        port_file = os.path.join(copy_dir, "DevToolsActivePort")
        try:
            os.remove(port_file)
        except FileNotFoundError:
            pass
        except OSError as exc:
            return None, f"{_RP}the stale debug-port marker could not be removed: {exc}"

        if headed is True and not wants_headed:
            return None, ("headed=true requires a graphical display, but no DISPLAY or "
                          "WAYLAND_DISPLAY is available on this Linux host.")
        chrome_argv = [executable, "--remote-debugging-port=0", "--remote-debugging-address=127.0.0.1",
                       f"--user-data-dir={copy_dir}", "--profile-directory=Default", "--no-first-run",
                       "--no-default-browser-check", "--no-startup-window",
                       # The snapshot carries the user's authenticated state: keep the clone out of
                       # Chrome Sync and background update loops so agent activity cannot leak into
                       # or contend with the live profile's account state.
                       "--disable-sync", "--disable-background-networking", "--disable-component-update"]
        if not wants_headed:
            chrome_argv.append("--headless=new")
        try:
            Path(copy_dir, ".hermes-browser-mode").write_text(
                "headed" if wants_headed else "headless", encoding="utf-8")
        except OSError as exc:
            return None, f"{_RP}mode state could not be saved: {exc}"
        try:
            chrome_proc = subprocess.Popen(
                chrome_argv, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                env=_bt._build_browser_env(), start_new_session=os.name != "nt",
                creationflags=windows_hide_flags())
        except OSError as exc:
            return None, f"{_RP}the matching browser could not be launched: {exc}"
        with _bt._real_profile_cdp_locks_guard:
            _bt._real_profile_browser_processes[cache_key] = (chrome_proc, copy_dir)

        port = None
        deadline = time.monotonic() + 30.0
        while time.monotonic() < deadline:
            candidate = _read_devtools_port(copy_dir) or ""
            if candidate.isdigit() and 0 < int(candidate) < 65536:
                if _bt._cdp_http_ready(f"http://127.0.0.1:{candidate}"):
                    port = candidate
                    break
            if chrome_proc.poll() is not None:
                _bt._stop_real_profile_browser(cache_key)
                return None, f"{_RP}the matching browser exited before its debug endpoint became ready"
            time.sleep(0.1)
        if port is None:
            _bt._stop_real_profile_browser(cache_key)
            return None, f"{_RP}the matching browser did not expose a debug endpoint in time"

        cdp = f"http://127.0.0.1:{port}"
        if attach_error := _attach_agent_browser_to_cdp(session_name, cdp):
            _bt._stop_real_profile_browser(cache_key)
            return None, f"{_RP}{attach_error}"
        if not _bt._cdp_owned_by_data_dir(cdp, copy_dir):
            _bt._agent_browser_close_session(session_name)
            _bt._stop_real_profile_browser(cache_key)
            return None, (_RP + "the launched browser did not prove ownership of the identity "
                          "snapshot; refusing the CDP attach")
        _bt._real_profile_cdp_cache[cache_key] = cdp
        _bt._real_profile_headed_modes[cache_key] = wants_headed
        _bt._track_real_profile_session(cache_key, session_name)
        _bt.logger.info("real-profile browser ready for %s%s at %s (%s)", browser,
                        f" identity {identity.alias!r}" if identity is not None else "", cdp, copy_dir)
        return cdp, None
