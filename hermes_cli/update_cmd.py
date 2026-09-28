"""Hermes update pipeline: dispatchers (``_cmd_update_impl``/``_cmd_update_check``) + git plumbing.

Each concern lives in ``update_cmd_<concern>.py`` and is re-imported here so
``hermes_cli.update_cmd.<name>`` keeps resolving (and stays monkeypatchable). Imports are one-way:
main -> update_cmd -> update_cmd_*; ``_m()`` resolves ``hermes_cli.main`` at call time.
"""

import logging
import json
import re
from contextlib import suppress
import os
import shlex
import shutil  # noqa: F401  (tests patch update_cmd.shutil.*; split modules resolve it here)
import subprocess
import sys
import time as _time
from dataclasses import dataclass
from pathlib import Path

from hermes_cli.config import get_hermes_home  # noqa: F401  (re-exported; patched via update_cmd)
from hermes_cli import update_handoff as _update_handoff
from hermes_cli.update_cmd_common import _best_effort
from hermes_cli._early_recovery import interrupted_pull_marker
from hermes_constants import get_default_hermes_root, project_venv_dir, venv_python_path

# Re-exports: every split-module name stays reachable (and monkeypatchable) as update_cmd.<name>.
from hermes_cli.update_abort_recovery import (  # noqa: F401
    _abort_recovery_is_complete, _qualified_serve_skips, _recover_gateway_restart_after_abort,
    _serve_unit_recovery_available, _surviving_pre_update_serve_runtimes,
    _warn_stale_serve_runtimes)
from hermes_cli.update_cmd_windows import (  # noqa: F401
    _HOLDER_VALUE_FLAGS_FALLBACK, _clear_windows_venv_holders_or_exit,
    _cold_start_windows_gateway_after_update, _desktop_owns_gateway_lifecycle,
    _detect_venv_python_processes, _format_venv_python_holders_message,
    _handoff_reapable_backend_pids, _hermes_holder_subcommand, _holder_value_flags,
    _holder_value_flags_cache, _ledger_manual_serve_holders, _ledger_reapable_backend_pids,
    _leftover_pausable_gateway_pids, _looks_like_desktop_control_plane,
    _orphaned_desktop_backend_pids, _pause_windows_gateways_for_update,
    _refresh_bootstrap_cache_scripts, _refresh_windows_gateway_launchers,
    _refuse_gateway_ancestor_tree_kill, _relaunch_stopped_serves,
    _restore_windows_gateway_service, _resume_windows_gateways_after_update,
    _resume_windows_gateways_and_merge_outcome, _self_and_non_gateway_ancestor_pids,
    _serve_relaunch_commands, _start_windows_gateway_service, _stop_process_trees,
    _stop_windows_gateway_service, _venv_launcher_ancestors,
    _wait_for_windows_update_gateway_exit, _write_update_planned_stop_marker)
from hermes_cli.update_cmd_fleet import (  # noqa: F401
    _FLEET_RESTART_PENDING_NAME, _FRESH_RESTART_SUPERVISORS, _GatewayRestartOutcome,
    _apply_pending_fleet_restart_catchup, _clear_fleet_restart_pending_marker,
    _current_checkout_sha, _defer_fleet_restart_after_update, _drain_or_signal_gateway_for_update, _fleet_probe_expected_runtimes,
    _fleet_restart_pending_marker_path, _for_each_systemd_gateway_unit,
    _gateway_recovery_partition, _gateway_service_matches_profile, _pending_fleet_restart_needed,
    _receipt_looks_unfinished, _receipt_reports_stale_runtime, _resolve_manage_cmd,
    _restart_gateway_fleet_after_update, _restart_launchd_gateway_after_update,
    _restart_macos_launchd_gateways, _restart_phase_failure_is_incomplete,
    _restart_systemd_gateway_units, _restart_systemd_gateway_units_best_effort,
    _run_pending_fleet_restart, _service_restart_sec,
    _service_unit_supports_graceful_sigusr1_restart, _surviving_gateway_pids_after_failed_restart,
    _systemctl, _systemctl_reset_and_restart, _verify_fleet_after_update,
    _wait_for_service_active, _warn_gateway_restart_phase_aborted,
    _warn_incomplete_gateway_fleet_restart, _warn_pending_fleet_restart,
    _warn_pending_fleet_restart_on_startup, _write_fleet_restart_pending_marker,
    _write_gateway_update_exit_code)
from hermes_cli.update_cmd_zip import (  # noqa: F401
    _ZIP_PRESERVED_TOP_LEVEL, _ZIP_STAGING_ARTIFACT_SUFFIXES, _abort_zip_update_if_dirty_tree,
    _atomic_replace_dir, _commit_staged_replacements, _discard_staged, _finish_zip_update,
    _is_zip_preserved_entry_status_line, _is_zip_staging_artifact_status_line, _stage_replacement,
    _update_via_zip, _zip_overlay_block_reason)
from hermes_cli.update_cmd_stash import (  # noqa: F401
    _AUTOSTASH_NAME_PREFIX, _AUTOSTASH_WARN_AGE_DAYS, _discard_stashed_changes,
    _git_untracked_paths, _park_stashed_changes, _print_stash_cleanup_guidance,
    _reject_unsafe_stash_restore, _resolve_stash_selector, _restore_stashed_changes,
    _restored_python_paths, _stash_apply_failed_only_on_existing_untracked,
    _stash_local_changes_if_needed, _warn_orphaned_update_autostashes)
from hermes_cli.update_cmd_config import (  # noqa: F401
    _LAST_SIBLING_SNAPSHOTS, _check_and_apply_config_migration, _migrate_sibling_profile_configs,
    _print_items, _run_config_check_fresh, _run_migrate_config_fresh)
from hermes_cli.update_cmd_deps import (  # noqa: F401
    _INSTALL_DEFINING_FILES, _UPDATE_CRITICAL_MODULES,
    _abort_dependency_sync_if_self_locked, _capture_active_lazy_features,
    _capture_active_tool_dependencies, _critical_module_import_failures,
    _defer_update_for_self_lock, _dependency_sync_would_rewrite, _desktop_app_present,
    _detect_self_loaded_native_modules, _editable_install_is_current, _ensure_uv_for_termux,
    _ensure_venv_pip, _install_psutil_android_compat, _is_android_python, _npm_bin_exists,
    _npm_lockfile_changed, _npm_manifest_paths, _npm_manifests_digest, _path_uid,
    _rebuild_desktop_after_update, _record_npm_lockfile_hash, _refresh_active_lazy_features,
    _reapply_plugin_python_dependencies,
    _refresh_active_memory_provider_dependencies, _refuse_update_if_venv_foreign_owned,
    _repair_node_deps_on_current_checkout, _restore_active_tool_dependencies,
    _sync_python_dependencies_after_pull, _update_node_dependencies,
    _upgrade_pip_before_lazy_refresh, _validate_critical_modules_import,
    _venv_core_imports_healthy, _venv_dependency_set_stale, _venv_foreign_owned_paths,
    _web_build_toolchain_ready, _web_toolchain_roots)
from hermes_cli.update_cmd_git import (  # noqa: F401
    OFFICIAL_REPO_URL, OFFICIAL_REPO_URLS, SKIP_UPSTREAM_PROMPT_FILE, _ORPHAN_RESCUE_REFS_TO_KEEP,
    _ORPHAN_RESCUE_REF_MAX_AGE_DAYS, _add_upstream_remote, _assess_parked_branch_switch,
    _branch_head_label, _branch_head_suffix, _classify_fetch_failure, _count_commits_between,
    _discard_lockfile_churn, _ensure_non_trampoline_git, _get_origin_url, _git_is_trampoline,
    _has_upstream_remote, _is_fork, _locate_real_git, _mark_skip_upstream_prompt,
    _normalize_managed_eol, _portable_git_candidates, _print_fetch_failure,
    _print_parked_branch_kept_notice, _print_parked_branch_skip_warning,
    _prune_orphan_rescue_refs, _should_skip_upstream_prompt, _sync_fork_with_upstream,
    _sync_with_upstream_if_needed)
from hermes_cli.update_cmd_maint import (  # noqa: F401
    _PRE_UPDATE_SNAPSHOT_KEEP, _PRE_UPDATE_SNAPSHOT_MAX_FILE_SIZE,
    _clear_stale_sqlite_sidecars,
    _ensure_acp_launcher, _ensure_fhs_path_guard, _finish_dashboard_update_cleanup,
    _format_time_ago, _post_update_sqlite_runtime_status, _print_bundled_skills_sync_report,
    _print_curator_first_run_notice, _print_curator_recent_run_notice,
    _print_fts_optimize_available_notice, _print_update_completion, _print_update_summary,
    _print_verified_update_completion, _read_project_version,
    _resolve_pre_update_backup_mode, _restore_state_db_from_snapshot,
    _run_post_update_maintenance, _run_pre_update_backup,
    _sweep_bytecode_after_update,
    _update_complete_message, _verify_and_restore_one_state_db,
    _verify_and_restore_state_dbs_post_update)
logger = logging.getLogger(__name__)


def _m():
    """Lazy ``hermes_cli.main`` handle: keeps main-side test patches effective, import one-way."""
    from hermes_cli import main
    return main


def _require_immutable_launchd() -> None:
    """S2 changes service definitions only through macOS launchd."""
    from hermes_cli.immutable_releases import ReleasePaths, read_pointer
    home = get_hermes_home()
    paths = ReleasePaths.for_home(home)
    if (bool(_updates_config().get("immutable_releases", False)) or read_pointer(paths.current) is not None
            or (paths.home / "release-layout.json").exists()) and sys.platform != "darwin":
        raise RuntimeError("immutable releases require macOS launchd; disable updates.immutable_releases on this platform")


def _previous_release_sha(paths, previous: str | None) -> str | None:
    """Resolve a receipt identity from the actual source/release, never a directory label."""
    if previous is None:
        return None
    path = Path(previous)
    if path.parent == paths.releases.resolve():
        try:
            value = (path / ".hermes_build_sha").read_text(encoding="utf-8").strip()
        except OSError:
            return None
        return value if re.fullmatch(r"[0-9a-fA-F]{40}", value) else None

    journal = paths.home / "release-layout.json"
    try:
        record = json.loads(journal.read_text(encoding="utf-8"))
        if Path(record["source"]).resolve() == path.resolve():
            value = record.get("source_sha")
            if isinstance(value, str) and re.fullmatch(r"[0-9a-fA-F]{40}", value):
                return value
    except (OSError, ValueError, KeyError, TypeError):
        pass
    # Older/interrupted journals may omit source_sha; HEAD still identifies the
    # checkout currently pointed to by `previous` if it remains a Git checkout.
    try:
        from hermes_cli.immutable_releases import release_sha
        value = release_sha(path)
        return value if re.fullmatch(r"[0-9a-fA-F]{40}", value) else None
    except (OSError, subprocess.CalledProcessError):
        return None


def _immutable_release_enabled(paths=None) -> bool:
    """One opt-in gate for fetched updates and no-pull reconciliation alike."""
    from hermes_cli.immutable_releases import ReleasePaths, read_pointer, resolved_release
    paths = paths or ReleasePaths.for_home(get_hermes_home())
    current = read_pointer(paths.current)
    if current is not None and current.parent == paths.releases.resolve() and not resolved_release(paths.home):
        raise RuntimeError(f"current release is not ready: {current}; repair the pointer or artifact before updating")
    return bool(_updates_config().get("immutable_releases", False) or resolved_release(paths.home))


_IMMUTABLE_RELEASE_ACK_CALL_SITES = frozenset({
    "_activate_immutable_release",
    "_finish_pending_release_transaction",
    "_catch_up_immutable_release",
    "_cmd_update_impl.rollback",
    "_cmd_update_impl.repair-service",
})


def _release_acknowledgement_timeout() -> float:
    return float(_updates_config().get("release_acknowledgement_timeout_seconds", 180.0))


def _await_release_acknowledgement(home: Path, *, timeout_seconds: float | None = None) -> bool:
    """Observe one issued reload through the release manager's shared wait."""
    from hermes_cli.immutable_releases import wait_for_release_acknowledgement
    return wait_for_release_acknowledgement(
        home, timeout_seconds=_release_acknowledgement_timeout()
        if timeout_seconds is None else timeout_seconds)


def _finish_pending_release_transaction(home: Path | None = None) -> dict | None:
    """Replay the durable intent before any updater or reconciliation decision."""
    from hermes_cli.immutable_releases import ReleasePaths, recover_pending_transaction
    home = home or get_hermes_home()
    paths = ReleasePaths.for_home(home)
    if not (paths.home / "release-txn.json").exists():
        return None
    from hermes_cli import gateway, gateway_launchd
    plist = gateway.get_launchd_plist_path() if sys.platform == "darwin" else None
    callback = (lambda: gateway_launchd._reload_installed_launchd_plist(plist)) if plist else None
    result = recover_pending_transaction(home, reload_callback=callback)
    if result and result.get("reload_pending"):
        if _await_release_acknowledgement(home):
            recovered = recover_pending_transaction(home, reload_callback=callback)
            result = recovered or {key: value for key, value in result.items()
                                   if key != "reload_pending"}
        if result and result.get("reload_pending"):
            raise RuntimeError("release reload pending: intended launchd gateway has not acknowledged startup within the configured observation window; recovery is observation-only after reload_issued. Inspect the label and plist; if no process exists, use an explicit operator-controlled service repair")
    return result


def _activate_immutable_release(*, defer: bool = False, sha: str | None = None,
                                source: Path | None = None,
                                source_python: Path | None = None) -> bool:
    """Stage an opted-in layout; only promote/reload with restart authorization."""
    from hermes_cli.immutable_releases import (
        ReleasePaths, activate_release, read_pointer, release_sha, stage_release,
        recover_pending_transaction,
    )
    _require_immutable_launchd()
    home = get_hermes_home()
    paths = ReleasePaths.for_home(home)
    # The first migration changes launchd's executable and must be deliberate.
    # Normal updates on an existing checkout must not create releases/ or a plist.
    if not _immutable_release_enabled(paths):
        _record_update_step("immutable_release", True, "skipped: release layout not opted in")
        return True
    source = source or _m().PROJECT_ROOT
    try:
        sha = sha or release_sha(source)
        if not sha:
            _record_update_step("immutable_release", True, "skipped: no git revision in mocked/non-git checkout")
            return True
        candidate, action = stage_release(source, home, sha=sha)
        if defer:
            _record_update_step("immutable_release", True, f"staged: {candidate}; activation deferred")
            return True
        # A pending record owns its target, even if its pointers already moved.
        # Complete it before evaluating a new candidate. The release manager
        # persists the exact intended plist before touching any mutable state.
        recovered = _finish_pending_release_transaction(home)
        if recovered is not None and recovered.get("current") == str(candidate):
            result = recovered
            first = recovered.get("source_sha") is not None
        else:
            first = read_pointer(paths.current) is None
            plist = None
            plist_body = None
            reload_callback = None
            if sys.platform == "darwin":
                from hermes_cli import gateway, gateway_launchd
                plist = gateway.get_launchd_plist_path()
                if plist.exists():
                    plist_body = gateway.generate_launchd_plist(release_target=candidate).encode("utf-8")
                    reload_callback = lambda: gateway_launchd._reload_installed_launchd_plist(plist)
                else:
                    plist = None
            result = activate_release(home, candidate, source=source if first else None,
                                      source_python=source_python if first else None,
                                      plist_path=plist, plist_body=plist_body,
                                      reload_callback=reload_callback)
        if result.get("reload_pending"):
            if _await_release_acknowledgement(home):
                result = _finish_pending_release_transaction(home) or {key: value for key, value in result.items()
                                                        if key != "reload_pending"}
            else:
                _record_update_step("immutable_release", False,
                                    f"reload issued, awaiting gateway acknowledgement: {candidate}; "
                                    "inspect the launchd label and recorded plist; if no process starts, "
                                    "repair the service explicitly under operator control")
                return False
        from hermes_cli.update_receipt import record_release_transition
        previous = result["previous"]
        from_sha = _previous_release_sha(paths, previous)
        if previous and from_sha is None:
            _record_update_step("immutable_release_identity", True,
                                f"from_sha unknown: no valid 40-hex source HEAD/journal SHA or release build marker for {previous}")
        record_release_transition(
            from_sha=from_sha,
            to_sha=sha, from_path=previous, to_path=str(candidate),
            kind="migration" if first else "promotion",
        )
        _record_update_step("immutable_release", True,
                            f"{action}: from={result['previous']} to={result['current']} sha={sha} migration={first}")
        return True
    except Exception as exc:
        _record_update_step("immutable_release", False, str(exc))
        logger.exception("Immutable release staging or activation failed; inspect current pointer and migration journal")
        return False


class UpdateConfigurationLoadError(Exception):
    """Config could not be read before updater options were resolved."""


def _updates_config() -> dict:
    """The ``updates:`` config section (``{}`` when absent/malformed); may raise on config errors."""
    from hermes_cli.config import load_config, validate_config_structure
    try:
        config = load_config() or {}
    except Exception as exc:
        raise UpdateConfigurationLoadError(str(exc)) from exc
    issues = [issue for issue in validate_config_structure({"updates": config.get("updates")})
              if issue.severity == "error"]
    if issues:
        raise ValueError("Invalid update configuration: " + "; ".join(issue.message for issue in issues))
    section = config.get("updates", {})
    return section if isinstance(section, dict) else {}


def _no_prompt_git_kwargs() -> dict:
    """``subprocess.run`` kwargs for network git: a 401 (GitHub outage) would block forever on
    ``Username for ...``; disable only the *prompt* (credential helpers still run) so it fails fast."""
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0", "GCM_INTERACTIVE": "Never"}
    return {"stdin": subprocess.DEVNULL, "env": env}


# CLI-startup files (+ web_server.py, launched by a fresh Windows Desktop install) that must
# parse post-update; the syntax guard rolls back when one doesn't.
_UPDATE_CRITICAL_FILES = (
    "hermes_cli/main.py", "hermes_cli/config.py", "hermes_cli/__init__.py",
    "hermes_cli/web_server.py", "cli.py", "run_agent.py", "model_tools.py", "toolsets.py",
    "hermes_constants.py")


def _record_update_step(step: str, ok: bool, detail: str = "") -> None:
    """Best-effort ``update_receipt.record_step``; the receipt must never break an update."""
    with suppress(Exception):
        from hermes_cli.update_receipt import record_step
        record_step(step, ok, detail)


def _record_update_skip(name: str, reason: str) -> None:
    """Best-effort ``update_receipt.record_skip``; a deliberate opt-out is a skip, not a
    failed step, so a receipt reader can tell "the user said no" from "it broke"."""
    with suppress(Exception):
        from hermes_cli.update_receipt import record_skip
        record_skip(name, reason)


# A fetch whose transport dead-stalls (HTTP/2 to GitHub on some networks, a black-holed proxy)
# otherwise leaves `hermes update` on "Fetching updates..." forever (#93759, #95777). Five
# minutes is generous for a scoped single-branch fetch and still ends in a real error.
NETWORK_GIT_TIMEOUT_SECONDS = 300


def _git_run(git_cmd, args, cwd=None, *, check=False, network=False):
    """Run git capturing utf-8 text (default cwd: checkout); ``network=True`` disables the
    terminal prompt so an HTTP 401 fails fast instead of hanging, and bounds the wait."""
    try:
        return subprocess.run(
            git_cmd + args, cwd=_m().PROJECT_ROOT if cwd is None else cwd, capture_output=True,
            text=True, encoding="utf-8", errors="replace", check=check,
            **({"timeout": NETWORK_GIT_TIMEOUT_SECONDS, **_no_prompt_git_kwargs()} if network else {}))
    except subprocess.TimeoutExpired as exc:
        # subprocess.run already killed the child; the checkout stays consistent because
        # fetch writes to tmp_pack_* and only renames on success. Report as a failed run
        # so every caller's existing stderr path prints one clear line.
        result = subprocess.CompletedProcess(
            exc.cmd, 124, stdout="",
            stderr=f"git {args[0]} timed out after {NETWORK_GIT_TIMEOUT_SECONDS}s with no response from the remote")
        if check:
            raise subprocess.CalledProcessError(124, exc.cmd, output="", stderr=result.stderr) from exc
        return result


def _capture_head_sha(git_cmd, cwd) -> str | None:
    """Return the current HEAD SHA, or None if it can't be resolved."""
    try:
        result = _git_run(git_cmd, ["rev-parse", "HEAD"], cwd, check=True)
        return result.stdout.strip() or None
    except (subprocess.CalledProcessError, OSError):
        return None


def _validate_python_files_syntax(root, relpaths) -> tuple[bool, str | None, str | None]:
    """Compile *relpaths* under *root*; the .pyc goes to a temp dir, not ``__pycache__/`` (no
    race with test workers, no stale pyc for another interpreter)."""
    import py_compile
    import tempfile
    root = Path(root)
    with tempfile.TemporaryDirectory(prefix="hermes-syntax-check-") as tmpdir:
        for relpath in relpaths:
            path = root / relpath
            if not path.exists():
                continue
            cfile = Path(tmpdir) / (str(relpath).replace("/", "__") + "c")
            try:
                py_compile.compile(str(path), cfile=str(cfile), doraise=True)
            except py_compile.PyCompileError as exc:
                return False, str(path), str(exc)
            except OSError as exc:
                return False, str(path), f"could not read: {exc}"
    return True, None, None


def _validate_critical_files_syntax(root) -> tuple[bool, str | None, str | None]:
    """Compile ``_UPDATE_CRITICAL_FILES`` -> ``(ok, failing_path, error_message)``."""
    return _validate_python_files_syntax(root, _UPDATE_CRITICAL_FILES)


def _gateway_prompt(prompt_text: str, default: str = "", timeout: float = 300.0) -> str:
    """File-based IPC prompt for ``--gateway``: write a marker the gateway forwards to the
    messenger, poll for a response file, fall back to *default* on timeout."""
    import json as _json
    import uuid as _uuid
    from hermes_constants import get_hermes_home  # noqa: F811  (deliberate: constants variant)
    home = get_hermes_home()
    prompt_path, response_path = home / ".update_prompt.json", home / ".update_response"
    response_path.unlink(missing_ok=True)

    payload = {"prompt": prompt_text, "default": default, "id": str(_uuid.uuid4())}
    tmp = prompt_path.with_suffix(".tmp")
    tmp.write_text(_json.dumps(payload), encoding="utf-8")
    tmp.replace(prompt_path)

    deadline = _time.monotonic() + timeout
    while _time.monotonic() < deadline:
        if response_path.exists():
            with suppress(OSError, ValueError):
                answer = response_path.read_text(encoding="utf-8").strip()
                response_path.unlink(missing_ok=True)
                prompt_path.unlink(missing_ok=True)
                return answer if answer else default
        _time.sleep(0.5)

    prompt_path.unlink(missing_ok=True)
    response_path.unlink(missing_ok=True)
    print(f"  (no response after {int(timeout)}s, using default: {default!r})")
    return default


def _called_process_error_cmd_parts(exc: subprocess.CalledProcessError) -> list[str]:
    """Normalize ``CalledProcessError.cmd`` into argv-style tokens."""
    cmd = exc.cmd
    if cmd is None:
        return []
    if isinstance(cmd, (str, bytes)):
        text = cmd.decode("utf-8", "replace") if isinstance(cmd, bytes) else cmd
        try:
            return shlex.split(text, posix=os.name != "nt")
        except ValueError:
            return text.split()
    return [str(part) for part in cmd]


def _called_process_error_is_git(exc: subprocess.CalledProcessError) -> bool:
    """True when the failed subprocess was git itself."""
    parts = _called_process_error_cmd_parts(exc)
    if not parts:
        return False
    # Windows argv may use backslashes; POSIX basename() would keep the whole path.
    name = os.path.basename(parts[0].replace("\\", "/")).lower()
    return name in {"git", "git.exe"}


def _called_process_error_is_python_dep_install(exc: subprocess.CalledProcessError) -> bool:
    """True when the failed subprocess was a uv/pip (or ensurepip) install."""
    parts = [part.lower() for part in _called_process_error_cmd_parts(exc)]
    if not parts:
        return False
    exe = os.path.basename(parts[0].replace("\\", "/"))
    return "ensurepip" in parts or ("install" in parts and (
        "pip" in parts or exe in {"pip", "pip.exe", "pip3", "pip3.exe", "uv", "uv.exe"}))


def _format_update_failure_stage(exc: subprocess.CalledProcessError) -> str:
    """Name the failed stage: git pull and dep install share one ``try``, and calling every
    CalledProcessError a git failure misled users and keyed the ZIP overlay on exception
    *type* rather than on git actually failing.

    See #85840, #87304.
    """
    if _called_process_error_is_python_dep_install(exc):
        return "Python dependency install failed"
    if _called_process_error_is_git(exc):
        return "Git update failed"
    return "Update step failed"


def _shim_quarantine_error_type() -> "type[BaseException]":
    """Strict-quarantine refusal type via ``_m()``; falls back to a never-raised private
    type when main.py lacks it (torn mid-update tree) so the ``except`` stays valid."""
    cls = getattr(_m(), "ShimQuarantineError", None)
    if isinstance(cls, type) and issubclass(cls, BaseException):
        return cls

    class _Never(Exception):
        pass

    return _Never


def _refuse_update_for_contended_shims(exc: BaseException) -> None:
    """Fail closed when live shims could not be quarantined: a rename failing every retry
    proves a holder without FILE_SHARE_DELETE, and installing anyway strands the venv between
    versions. The code swap is already committed; only the dep install is deferred (via the
    update-incomplete marker). Exits 2 so the receipt records a refusal, not a failure.

    See #87331.
    """
    print("✗ Cannot continue the update: live Hermes launcher(s) could not be")
    print("  moved aside:")
    for name in getattr(exc, "failed_shims", []) or ["hermes.exe"]:
        print(f"    {name}")
    print("  Another process is holding this install's venv — typically Hermes")
    print("  Desktop, a gateway, or another hermes REPL — and mutating the venv")
    print("  now would strand it half-updated.")
    print("  The dependency install has been deferred: close the process(es)")
    print("  above, then run any `hermes` command to finish it automatically.")
    # Idempotent (git path already dropped it); covers ZIP/repair paths so the deferral is never silent.
    _write_update_incomplete_marker()
    sys.exit(2)


def _should_zip_fallback_on_update_error(exc: BaseException) -> bool:
    """ZIP fallback is only for Windows git file-I/O breakage: after a dep-install failure the
    pull already succeeded, so a ZIP overlay can't fix it and would replace every top-level
    entry except venv/node_modules/.git/.env, deleting uncommitted and untracked files."""
    return (
        isinstance(exc, subprocess.CalledProcessError)
        and _m()._is_windows()
        and _called_process_error_is_git(exc))


def _print_called_process_error_tail(exc: subprocess.CalledProcessError, *, limit: int = 12) -> None:
    """Print a captured stderr/stdout tail when the failing call recorded one."""
    blob = exc.stderr or exc.stdout or ""
    if isinstance(blob, bytes):
        blob = blob.decode("utf-8", "replace")
    lines = [line for line in str(blob).splitlines() if line.strip()]
    if not lines:
        return
    print("  Last output:")
    for line in lines[-limit:]:
        print(f"    {line}")


def _invalidate_update_cache():
    """Delete the update-check cache for ALL profiles: the repo is shared, so one profile's
    update makes every profile current and a stale "commits behind" banner would linger."""
    default_home = get_default_hermes_root()
    profiles_root = default_home / "profiles"
    homes = [default_home]
    if profiles_root.is_dir():
        homes += [entry for entry in profiles_root.iterdir() if entry.is_dir()]
    for home in homes:
        with suppress(Exception):
            (home / ".update_check").unlink(missing_ok=True)


def _write_marker_file(path: Path, *, label: str) -> None:
    """Drop an update-recovery breadcrumb. Never raises."""
    if _m()._pytest_owns_live_checkout(path.parent):
        logger.debug("Skipping %s marker under pytest (live checkout)", label)
        return
    try:
        path.write_text(f"started={_time.time()}\npid={os.getpid()}\n", encoding="utf-8")
    except OSError as exc:
        logger.debug("Could not write %s marker: %s", label, exc)


def _write_update_incomplete_marker() -> None:
    """Drop the interrupted core-install breadcrumb. Never raises."""
    _write_marker_file(_m()._update_marker_path(), label="update-incomplete")


def _write_lazy_refresh_incomplete_marker() -> None:
    """Drop the interrupted lazy-refresh breadcrumb. Never raises."""
    _write_marker_file(_m()._lazy_refresh_marker_path(), label="lazy-refresh-incomplete")


def _format_concurrent_instances_message(matches: list[tuple[int, str]], scripts_dir: Path) -> str:
    """Explanation + remediation hint for the Windows concurrent-hermes.exe gate."""
    shim = scripts_dir / "hermes.exe"
    lines = [
        "✗ Another hermes.exe is running:",
        *(f"    PID {pid}  {name}" for pid, name in matches),
        "",
        f"  Updating now would fail to overwrite {shim} because",
        "  Windows blocks REPLACE on a running executable.",
        "",
        "  Close Hermes Desktop, exit any open `hermes` REPLs, and",
        "  stop the gateway (`hermes gateway stop`) before retrying.",
        ""]
    if matches:
        pid_args = " ".join(f"/PID {pid}" for pid, _ in matches)
        lines += [
            "  If you've already closed everything and these PIDs are",
            "  stale, terminate them directly, then retry the update:",
            f"      taskkill {pid_args} /F",
            ""]
    lines += [
        "  Override with `hermes update --force` if you've already",
        "  confirmed those processes will not write to the venv."]
    return "\n".join(lines)


def _classify_concurrent_instance(pid: int) -> str:
    """Classify ``pid`` as "gateway" / "non-gateway" / "unknown" (psutil can't read it). Uses
    ``_is_pausable_gateway`` (same matcher as the Desktop preflight and venv-holder guard) so
    "gateway" is exactly what the pause/restart machinery stops; "unknown" gates as non-gateway."""
    try:
        import psutil  # noqa: PLC0415
        cmdline_list = psutil.Process(int(pid)).cmdline()
    except Exception:
        return "unknown"

    from hermes_cli._scan_venv_blockers import _is_pausable_gateway  # noqa: PLC0415
    return "gateway" if _is_pausable_gateway(" ".join(cmdline_list or [])) else "non-gateway"


def _filter_non_gateway_concurrent_instances(matches: list[tuple[int, str]]) -> list[tuple[int, str]]:
    """Drop gateway matches (the pause + post-update restart machinery handles them); anything else
    (TUI, Desktop backend child, another REPL) has no pause path, so the gate aborts."""
    return [(pid, name) for pid, name in matches if _classify_concurrent_instance(pid) != "gateway"]


def _log_only_write(text: str) -> None:
    """Write to update.log only: reaches past the ``_UpdateOutputStream`` stdout mirror so
    loud, low-signal subprocess output stays debuggable without flooding the terminal."""
    if not text:
        return
    stream = _m().sys.stdout
    log_file = getattr(stream, "_log", None)
    with suppress(Exception):
        if log_file is None:
            log_path = get_hermes_home() / "logs" / "update.log"
            log_path.parent.mkdir(parents=True, exist_ok=True)
            with log_path.open("a", encoding="utf-8") as fallback:
                fallback.write(text)
        else:
            log_file.write(text)
            log_file.flush()


def _run_logged_subprocess(cmd, *, cwd=None, env=None):
    """Stream combined build output to update.log, retaining it for failure reporting."""
    import codecs
    import io
    from hermes_cli._subprocess_compat import kill_process_tree, windows_hide_flags

    child_env = dict(os.environ if env is None else env)
    child_env.setdefault("PYTHONUNBUFFERED", "1")
    spawn = {"creationflags": windows_hide_flags()} if os.name == "nt" else {"process_group": 0}
    proc = subprocess.Popen(
        cmd, cwd=cwd, env=child_env, stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, **spawn)
    # read1 delivers partial lines too; incremental decoding preserves split UTF-8
    # and the universal-newline behavior callers previously got from text=True.
    decoder = io.IncrementalNewlineDecoder(codecs.getincrementaldecoder("utf-8")("replace"), True)
    output = []
    try:
        while True:
            chunk = proc.stdout.read1(8192)
            text = decoder.decode(chunk, final=not chunk)
            output.append(text)
            _log_only_write(text)
            if not chunk:
                break
        return subprocess.CompletedProcess(cmd, proc.wait(), stdout="".join(output))
    except BaseException:
        # Unlike Popen.__exit__, do not wait for a cancelled build to finish.
        kill_process_tree(proc)
        with suppress(subprocess.TimeoutExpired):
            proc.wait(timeout=5)
        raise
    finally:
        proc.stdout.close()


def _cmd_update_check(branch: str = "main", *, branch_explicit: bool = False):
    """``hermes update --check``: fetch and report without installing. ``branch_explicit`` is
    True iff --branch was passed (Docker installs print a notice instead of dropping the flag)."""
    # Same marker-first admission gate as the apply path, so --check never reports git
    # state for an install whose real update mechanism is an image pull.
    from hermes_cli.update_contract import evaluate_update_admission, record_refusal_receipt

    refusal = evaluate_update_admission(_m().PROJECT_ROOT)
    if refusal is not None:
        print(refusal.message)
        record_refusal_receipt(refusal)
        sys.exit(2)

    git_dir = _m().PROJECT_ROOT / ".git"
    if not git_dir.exists():
        print("✗ Not a git repository — cannot check for updates.")
        sys.exit(1)

    git_cmd = _base_git_cmd()

    # Interrupted fetches leave .git/*.lock behind ("File exists" forever); self-heal first.
    from hermes_cli.gitlock import clear_stale_git_locks, clear_stale_tmp_packs
    for lock_path in clear_stale_git_locks(_m().PROJECT_ROOT):
        print(f"  (removed stale git lock: {lock_path})")
    # Aborted fetches also strand tmp_pack_* debris (has reached 6 GB and corrupted the
    # pack dir); same age+process safety contract as the locks.
    swept = clear_stale_tmp_packs(_m().PROJECT_ROOT)
    if swept:
        print(f"  (removed {len(swept)} aborted-fetch pack temp file(s))")

    # Fetch only <branch> (a bare fetch pulls thousands of auto-generated branches). Prefer
    # upstream only for main (a fork's other branches have no upstream counterpart). Installer
    # checkouts are shallow: a plain fetch would unshallow them and rev-list would report a
    # bogus huge "behind" count, so fetch --depth 1 and report presence-only.
    is_shallow = _is_shallow_checkout(git_cmd)
    depth_args = ["--depth", "1"] if is_shallow else []

    # Probe locally for an 'upstream' remote before a network fetch non-forks always fail.
    fetch_result = None
    if branch == "main" and _git_run(git_cmd, ["remote", "get-url", "upstream"]).returncode == 0:
        print("→ Fetching from upstream...")
        fetch_result = _git_run(git_cmd, ["fetch"] + depth_args + ["upstream", branch], network=True)
    if fetch_result is not None and fetch_result.returncode == 0:
        compare_branch = f"upstream/{branch}"
    else:
        print("→ Fetching from origin...")
        fetch_result = _git_run(git_cmd, ["fetch"] + depth_args + ["origin", branch], network=True)
        compare_branch = f"origin/{branch}"

    if fetch_result.returncode != 0:
        _print_fetch_failure(fetch_result.stderr)
        sys.exit(1)

    if is_shallow:
        # The depth-1 fetch above leaves the previous tip behind as a ``.git/shallow`` graft
        # (git never removes old grafts); prune the stale ones so the file stops growing and
        # merge-base / the orphan-divergence heuristic keep working (#105951).
        from hermes_cli.gitlock import repair_broken_shallow_boundaries, prune_stale_shallow_grafts
        repaired = repair_broken_shallow_boundaries(_m().PROJECT_ROOT)
        if repaired:
            print(f"  (restored {repaired} broken shallow boundary(ies))")
        pruned = prune_stale_shallow_grafts(_m().PROJECT_ROOT)
        if pruned:
            print(f"  (pruned {pruned} stale shallow graft(s) left by past depth-1 checks)")

    # rev-list on a bogus ref exits 128 and (check=True) would traceback; verify first.
    verify_result = _git_run(git_cmd, ["rev-parse", "--verify", "--quiet", compare_branch])
    if verify_result.returncode != 0:
        print(f"✗ Branch '{branch}' not found on {compare_branch.split('/', 1)[0]}.")
        sys.exit(1)

    if is_shallow:
        # No history across the shallow boundary: compare tip SHAs, then recover the
        # exact count via the GitHub compare API (complete graph).
        head_sha, target_sha = _tip_shas(git_cmd, compare_branch)
        if head_sha and target_sha and head_sha == target_sha:
            print("✓ Already up to date.")
            return
        from hermes_cli.banner import _github_compare_behind
        # counted == 0 means local-ahead, not behind; None means the API could not count.
        _print_update_check_result(_github_compare_behind(head_sha, target_sha), compare_branch)
        return

    rev_result = _git_run(git_cmd, ["rev-list", f"HEAD..{compare_branch}", "--count"], check=True)
    _print_update_check_result(int(rev_result.stdout.strip()), compare_branch)


def _base_git_cmd() -> list[str]:
    """``git`` argv; Windows adds ``-c windows.appendAtomically=false`` (git can fail "unable to
    write loose object file: Invalid argument" on non-atomic appends)."""
    if sys.platform == "win32":
        return ["git", "-c", "windows.appendAtomically=false"]
    return ["git"]


def _is_shallow_checkout(git_cmd) -> bool:
    return _git_run(git_cmd, ["rev-parse", "--is-shallow-repository"]).stdout.strip() == "true"


def _tip_shas(git_cmd, target_ref: str) -> tuple[str, str]:
    """``(HEAD sha, <target_ref> sha)`` as printed by rev-parse ("" when unresolvable)."""
    return tuple(_git_run(git_cmd, ["rev-parse", ref]).stdout.strip() for ref in ("HEAD", target_ref))


def _print_update_check_result(behind: int | None, compare_branch: str) -> None:
    """Report ``--check``'s verdict: up to date, N commits behind, or behind by an unknown count."""
    if behind == 0:
        print("✓ Already up to date.")
        return
    if behind is not None:
        print(f"☤ Update available: {behind} {'commit' if behind == 1 else 'commits'} behind {compare_branch}.")
    else:
        print(f"☤ Update available (behind {compare_branch}).")
    from hermes_cli.config import recommended_update_command
    print(f"  Run '{recommended_update_command()}' to install.")


def _repair_venv_on_current_checkout(
    *, assume_yes, gateway_mode, pre_update_snapshot_id,
    had_desktop_app_before_update, active_lazy_features, active_tool_dependencies,
    _windows_gateway_resume) -> bool:
    """Reinstall ``.[all]`` + lazy/tool deps into an unhealthy (or handed-off) venv; returns
    whether the checkout can be reported complete."""
    # Self-lock deferral: the repair rewrites the venv too (same mapped-extension hazard).
    # See #86735.
    # Self-lock deferral (relocated preflight — #86735): if THIS process holds a native extension the sync
    # must rewrite, defer NOW — after the code swap, so only the dependency install is pending and the next
    # fresh launch completes it via the marker.
    _m()._abort_dependency_sync_if_self_locked(_windows_gateway_resume)
    _write_update_incomplete_marker()
    from hermes_cli.managed_uv import ensure_uv
    repair_uv = ensure_uv()
    # Venv gone entirely (repair interrupted after the old one was moved aside): recreate.
    venv_dir = project_venv_dir(_m().PROJECT_ROOT) or _m().PROJECT_ROOT / "venv"
    venv_python_missing = not venv_python_path(venv_dir, windows=_m()._is_windows()).exists()
    if venv_python_missing and repair_uv:
        print("→ Recreating virtual environment...")
        subprocess.run([repair_uv, "venv", venv_dir.name], cwd=_m().PROJECT_ROOT, check=False)
    repair_prefix, repair_env = _pip_install_prefix(repair_uv)
    _m()._install_python_dependencies_with_optional_fallback(repair_prefix, env=repair_env, group="all")
    _m()._refresh_active_lazy_features(repair_prefix, env=repair_env, features=active_lazy_features)
    _m()._restore_active_tool_dependencies(active_tool_dependencies, repair_prefix, env=repair_env)
    # Same order as the pull and ZIP paths: the ``[all]`` reinstall above may have stripped the
    # active memory provider's bridge packages (torch, embedding stacks, ...).
    _m()._refresh_active_memory_provider_dependencies()
    _m()._reapply_plugin_python_dependencies()
    # Core ``.[all]`` install finished. Clear the generic core breadcrumb before the lazy-refresh phase —
    # that phase uses its own marker so a later lazy failure cannot be "healed" by clearing the core marker
    # based on a narrow 7-package import probe (#58004 review).
    _m()._clear_update_incomplete_marker()
    healthy_after, detail_after = _venv_core_imports_healthy()
    if not healthy_after:
        print(f"⚠ Venv still unhealthy after repair: {detail_after}")
        print("  Close all Hermes windows/gateways and re-run: hermes update")
        return False
    print("✓ Dependencies repaired!")
    # The hand-off child never reaches the commits-pulled Node/web/Desktop
    # phase. Finish through the current-checkout repair path, whose npm digest
    # gate keeps this cheap when the pulled manifests did not change.
    return _repair_node_deps_on_current_checkout(
        _print_verified_update_completion,
        assume_yes=assume_yes,
        gateway_mode=gateway_mode,
        pre_update_snapshot_id=pre_update_snapshot_id,
        completion_message="✓ Update complete!",
        had_desktop_app_before_update=had_desktop_app_before_update,
    )


def _pip_install_prefix(uv_bin) -> tuple[list[str], dict | None]:
    """``(install prefix, env)``: ``uv pip`` isolated from third-party UV env vars (so a foreign
    UV_PYTHON_INSTALL_DIR can't hijack it), else ``sys.executable -m pip`` (avoids PEP 668 errors)."""
    if uv_bin:
        # Same third-party UV-env isolation as the main update path (#83914): a user-level
        # UV_PYTHON_INSTALL_DIR / UV_PYTHON from unrelated software must not steer which interpreter uv
        # resolves here.
        # See #83914.
        from hermes_cli.managed_uv import managed_python_env
        env = managed_python_env()
        env["VIRTUAL_ENV"] = str(project_venv_dir(_m().PROJECT_ROOT) or _m().PROJECT_ROOT / "venv")
        return [uv_bin, "pip"], env
    return [sys.executable, "-m", "pip"], None


def _repair_current_checkout(
    *, assume_yes, gateway_mode, pre_update_snapshot_id,
    had_desktop_app_before_update, active_lazy_features, active_tool_dependencies,
    upstream_checked, _windows_gateway_resume) -> bool:
    """Already-up-to-date path: keep the managed runtime current, repair a broken venv.
    Returns whether the checkout can be reported complete."""
    # "No new commits" != safe interpreter: uv can keep the same CPython patch while
    # python-build-standalone refreshes the embedded SQLite; keep the boundary hook here too.
    from hermes_cli.managed_uv import ensure_uv, update_managed_uv
    runtime_repairs = []
    update_managed_uv(repair_observer=runtime_repairs.append)
    repair_uv = ensure_uv(repair_observer=runtime_repairs.append)
    runtime_repaired = next((result for result in runtime_repairs if result.repaired), None)

    # A current checkout does NOT imply a healthy install (a prior sync may have died
    # partway, e.g. Windows locked .pyd); probe or "Already up to date!" hides a bricked venv.
    healthy, detail = _venv_core_imports_healthy()
    # The Windows shim hand-off child is current BY DESIGN; its one job is the pending sync,
    # not venv health — without this it would print "Already up to date!" and skip it.
    handed_off_sync = os.environ.get(_m()._UPDATE_REEXEC_ENV) == "1"
    # Importable is not synced: a venv installed from an older release imports fine while its
    # pins lag the checkout (the sync after the pull was refused or died, #97208).
    stale, stale_detail = (
        _venv_dependency_set_stale() if healthy and not handed_off_sync else (False, ""))
    if handed_off_sync:
        print("→ Finishing the dependency install handed off by hermes.exe...")
    elif not healthy:
        print("⚠ Checkout is current, but the venv is unhealthy:")
        print(f"  {detail}")
        print("→ Repairing Python dependencies...")
    elif stale:
        print("⚠ Checkout is current, but its dependencies were never synced after the last pull:")
        print(f"  {stale_detail}")
        print("→ Syncing Python dependencies...")
    if handed_off_sync or not healthy or stale:
        current_checkout_complete = _repair_venv_on_current_checkout(
            assume_yes=assume_yes, gateway_mode=gateway_mode,
            pre_update_snapshot_id=pre_update_snapshot_id,
            had_desktop_app_before_update=had_desktop_app_before_update,
            active_lazy_features=active_lazy_features,
            active_tool_dependencies=active_tool_dependencies,
            _windows_gateway_resume=_windows_gateway_resume)
    else:
        if runtime_repaired is not None:
            # A successful SQLite repair swaps in a venv built from uv.lock alone, so core
            # imports pass while lazily-installed backends and ``hermes tools`` dependencies
            # are gone (#112571). Restore the pre-cutover snapshots exactly as the pull path
            # and the unhealthy-venv repair do; the healthy core set needs no reinstall.
            repair_prefix, repair_env = _pip_install_prefix(repair_uv)
            # Same marker discipline as the pull path: an interrupted or failed lazy restore
            # must leave the breadcrumb so the next `hermes` run finishes the repair.
            _write_lazy_refresh_incomplete_marker()
            if _m()._refresh_active_lazy_features(
                    repair_prefix, env=repair_env, features=active_lazy_features):
                _m()._clear_lazy_refresh_incomplete_marker()
            else:
                print("  ⚠ Lazy-refresh recovery incomplete — run `hermes` again "
                      "to finish import-based venv repair.")
            _m()._restore_active_tool_dependencies(
                active_tool_dependencies, repair_prefix, env=repair_env)
            # Same order as the pull path: the swapped-in venv was built from uv.lock alone.
            _m()._refresh_active_memory_provider_dependencies()
            _m()._reapply_plugin_python_dependencies()
        current_checkout_complete = _repair_node_deps_on_current_checkout(
            _print_verified_update_completion, assume_yes=assume_yes, gateway_mode=gateway_mode,
            pre_update_snapshot_id=pre_update_snapshot_id,
            completion_message=(
                "✓ Already up to date!" if upstream_checked
                else "✓ Up to date with your fork (official repo not checked)."),
            had_desktop_app_before_update=had_desktop_app_before_update)
    if runtime_repaired is not None and not _m()._is_windows():
        print()
        print("⚠ Restart required to finish the managed Python runtime repair.")
        print(
            "  Any running Hermes gateways, Desktop backends, or other "
            "long-lived processes still use the previous runtime.")
        print("  Restart each of them to pick up the repaired runtime.")
    return current_checkout_complete


def _reconcile_diverged_checkout(git_cmd, branch: str, pre_pull_sha) -> None:
    """Fast-forward failed: merge on a custom branch (local commits survive) or reset --hard on the
    same branch after parking the old HEAD behind a rescue ref. ``sys.exit(1)`` on failure."""
    # A custom branch (local commits atop origin/<branch>) also can't ff, and reset --hard
    # would discard that work: merge instead, stop on conflict.
    _cur_branch = (_git_run(git_cmd, ["branch", "--show-current"]).stdout or "").strip()
    if _cur_branch and _cur_branch != branch:
        print(
            f"  ⚠ Checkout is on custom branch '{_cur_branch}' — "
            f"merging origin/{branch} instead of resetting so local commits survive...")
        # Best-effort safety tag as a recovery anchor.
        _git_run(git_cmd, ["tag", f"pre-update-{_time.strftime('%Y%m%d-%H%M%S')}"])
        if _git_run(git_cmd, ["merge", "--no-edit", f"origin/{branch}"]).returncode != 0:
            _git_run(git_cmd, ["merge", "--abort"])
            print("✗ Merge conflict between local commits and upstream — update stopped, nothing was changed.")
            print(f"  Resolve manually: cd {_m().PROJECT_ROOT} && git merge origin/{branch}")
            print("  Then re-run the update. Local work is untouched.")
            sys.exit(1)
        return
    # Same branch: the reset below is right either way, but the two causes of divergence here
    # are indistinguishable from the checkout alone. An upstream force-push/rebase loses
    # nothing; local commits on this branch lose everything, and the reflog is the only way
    # back — an expiring log the user has to know to reach for, in a directory Hermes updates
    # unattended. So park pre_pull_sha behind a rescue ref for BOTH, orphan divergence (no
    # common ancestor: corrupted HEAD, re-init) included.
    merge_base_result = _git_run(git_cmd, ["merge-base", "HEAD", f"origin/{branch}"])
    has_common_ancestor = bool(
        merge_base_result.returncode == 0 and merge_base_result.stdout.strip())
    if pre_pull_sha:
        from datetime import datetime as _dt, timezone
        # SHA suffix so two updates in the same second get distinct refs.
        kind = "diverged" if has_common_ancestor else "orphan"
        rescue_ref = (
            f"refs/hermes-update-backups/{kind}-{branch}-"
            f"{_dt.now(timezone.utc).strftime('%Y%m%d-%H%M%S')}-{pre_pull_sha[:12]}")
        head = (
            f"  ⚠ Local history has diverged from origin/{branch} — "
            if has_common_ancestor else
            f"  ⚠ Local history shares no common ancestor with origin/{branch} (orphan divergence) — ")
        if _git_run(git_cmd, ["update-ref", rescue_ref, pre_pull_sha]).returncode == 0:
            print(
                f"{head}backed up current HEAD to {rescue_ref} before resetting. "
                f"This backup expires after {_ORPHAN_RESCUE_REF_MAX_AGE_DAYS} days.")
            if has_common_ancestor:
                dropped = (_git_run(
                    git_cmd, ["rev-list", "--count", f"origin/{branch}..{pre_pull_sha}"]).stdout or "").strip()
                print(f"    {dropped or 'Some'} commit(s) not on origin/{branch} leave the branch; "
                      f"list them with: git log origin/{branch}..{rescue_ref}")
        else:
            # update-ref failure is intentionally non-fatal, but never claim a backup exists.
            print(
                f"{head}attempted to back up current HEAD to {rescue_ref} before resetting, "
                f"but the backup write failed (pre-reset SHA was {pre_pull_sha}).")
        _prune_orphan_rescue_refs(git_cmd, _m().PROJECT_ROOT, branch)
    print("  ⚠ Fast-forward not possible (history diverged), resetting to match remote...")
    reset_result = _git_run(git_cmd, ["reset", "--hard", f"origin/{branch}"])
    if reset_result.returncode != 0:
        print(f"✗ Failed to reset to origin/{branch}.")
        if reset_result.stderr.strip():
            print(f"  {reset_result.stderr.strip()}")
        print(f"  Try manually: git fetch origin && git reset --hard origin/{branch}")
        sys.exit(1)


def _rollback_if_pulled_syntax_error(git_cmd, pre_pull_sha) -> None:
    """Post-pull syntax guard: roll back to *pre_pull_sha* and ``sys.exit(1)`` when a critical
    file no longer compiles (a bad admin-merge past CI must not brick the CLI)."""
    syntax_ok, failing_path, syntax_error = _validate_critical_files_syntax(_m().PROJECT_ROOT)
    if syntax_ok:
        return
    print()
    print("✗ Pulled code has a syntax error in a critical file:")
    print(f"  {failing_path}")
    # py_compile errors can be multi-line; show enough for the SyntaxError text.
    for line in str(syntax_error).splitlines()[:6] if syntax_error else ():
        print(f"    {line}")
    print()
    if pre_pull_sha:
        print(f"→ Rolling back to {pre_pull_sha[:10]}...")
        rollback_result = _git_run(git_cmd, ["reset", "--hard", pre_pull_sha])
        if rollback_result.returncode == 0:
            print("  ✓ Rollback complete — your install is unchanged.")
            print("  Try ``hermes update`` again later once a fix lands.")
        else:
            print("  ✗ Rollback failed. Recover manually with:")
            print(f"    cd {_m().PROJECT_ROOT} && git reset --hard {pre_pull_sha}")
            if rollback_result.stderr.strip():
                print(f"    ({rollback_result.stderr.strip().splitlines()[0]})")
    else:
        print("  Could not capture pre-pull SHA — recover manually with:")
        print(f"    cd {_m().PROJECT_ROOT} && git reflog && git reset --hard <prev-sha>")
    sys.exit(1)


def _pull_updates(
    git_cmd, branch, auto_stash_ref, *, prompt_for_restore, gw_input_fn, discard_local_changes,
    keep_stash):
    """Fast-forward onto ``origin/<branch>`` and settle the autostash. Divergence by shape:
    custom branch -> merge, same branch -> rescue ref then reset; a
    post-pull syntax error in a critical file rolls back. Exits on failure; returns pre-pull SHA."""
    update_succeeded = False
    # Pre-pull SHA for auto-rollback (stray conflict markers once bricked every updater).
    # Capture the pre-pull SHA so we can auto-roll-back if the new code has a syntax error in a
    # critical-path file (PR #28452 incident: orphan merge-conflict markers in hermes_cli/config.py bricked
    # every user who ran ``hermes update`` for the 7 minutes between the bad commit and the fix landing).
    pre_pull_sha = _capture_head_sha(git_cmd, _m().PROJECT_ROOT)
    # Git moves the tree file by file and HEAD last: if this process dies in between, the next launch
    # of any entry point finds this marker and puts the old tree back (_early_recovery). The target is
    # the resolved commit, so a later `git fetch` cannot widen what that restore considers.
    pull_marker = interrupted_pull_marker(_m().PROJECT_ROOT)
    target_sha = (_git_run(git_cmd, ["rev-parse", f"origin/{branch}^{{commit}}"]).stdout or "").strip()
    with _best_effort('Could not write the interrupted-pull marker: %s'):
        pull_marker.write_text(
            f"pid={os.getpid()}\npre={pre_pull_sha}\ntarget={target_sha}\nstash={auto_stash_ref or ''}\n",
            encoding="utf-8")
    try:
        try:
            # merge --ff-only the already-fetched ref instead of `git pull`, which would do a
            # SECOND network fetch; identical in effect given the fresh tracking ref.
            if _git_run(git_cmd, ["merge", "--ff-only", f"origin/{branch}"]).returncode != 0:
                _reconcile_diverged_checkout(git_cmd, branch, pre_pull_sha)
        except KeyboardInterrupt:
            raise  # Ctrl-C reached git too (same process group): the tree may be torn, keep the marker
        except BaseException:
            pull_marker.unlink(missing_ok=True)  # git exited on its own (sys.exit on conflict/reset failure)
            raise
        pull_marker.unlink(missing_ok=True)  # git is done: the tree is whole again
        _rollback_if_pulled_syntax_error(git_cmd, pre_pull_sha)
        update_succeeded = True
    finally:
        if auto_stash_ref is not None:
            # No stash restore if the update failed — tree state is unknown.
            if not update_succeeded:
                print(f"  ℹ️  Local changes preserved in stash (ref: {auto_stash_ref})")
                print("  Restore manually with: git stash apply")
            elif discard_local_changes:
                # Non-interactive + updates.non_interactive_local_changes: discard.
                _m()._discard_stashed_changes(git_cmd, _m().PROJECT_ROOT, auto_stash_ref)
            elif keep_stash:
                # --keep-stash (desktop updater): leave edits parked rather than re-apply silently.
                _m()._park_stashed_changes(auto_stash_ref)
            else:
                _m()._restore_stashed_changes(
                    git_cmd, _m().PROJECT_ROOT, auto_stash_ref, prompt_user=prompt_for_restore,
                    input_fn=gw_input_fn)
    return pre_pull_sha


@dataclass
class _CheckoutPlan:
    """What the pre-pull checkout phase decided (see ``_prepare_checkout_for_update``)."""

    auto_stash_ref: "str | None"
    commit_count: int
    in_place_update: bool
    parked_branch_switched: bool
    prompt_for_restore: bool
    switch_block_reason: "str | None"
    upstream_checked: bool


def _apply_parked_branch_guard(
    git_cmd, branch, current_branch, *, switch_branch, _windows_gateway_resume
) -> tuple[bool, bool, "str | None"]:
    """Decide how a checkout parked on another branch is brought to *branch* (stash-switch-pull-
    switch-back used to "update" main while the running code stayed behind).

    By branch contents + updates.parked_branch_strategy: fully merged -> switch back;
    unmerged -> "switch" (default; loud "kept" notice) or "update_in_place" (merge origin/<target>
    INTO the branch, checkout never moves; --switch-branch overrides once); dirty/unverifiable ->
    touch nothing, warn, ``sys.exit(1)`` with the code update SKIPPED (also when the target is
    missing). Returns ``(parked_branch_switched, in_place_update, switch_block_reason)``.
    """
    if current_branch == branch or current_branch == "HEAD":
        return False, False, None
    switch_safe, switch_block_reason = _m()._assess_parked_branch_switch(
        git_cmd, _m().PROJECT_ROOT, current_branch, branch)
    if not switch_safe:
        _m()._print_parked_branch_skip_warning(
            git_cmd, _m().PROJECT_ROOT, current_branch, branch, switch_block_reason)
        print()
        print(f"⚠ Update finished — code update SKIPPED{_branch_head_suffix(git_cmd, _m().PROJECT_ROOT)}")
        _m()._resume_windows_gateways_after_update(_windows_gateway_resume)
        sys.exit(1)
    if not switch_block_reason.startswith("unmerged:"):
        print(f"  ⚠ Checkout was parked on '{current_branch}' (fully merged) — switching back to {branch}...")
        return True, False, switch_block_reason
    _in_place_configured = False
    with _best_effort('Could not read updates.parked_branch_strategy: %s'):
        _in_place_configured = (
            _updates_config().get("parked_branch_strategy", "switch") == "update_in_place")
    if not _in_place_configured or switch_branch:
        _m()._print_parked_branch_kept_notice(
            current_branch, branch, switch_block_reason.split(":", 1)[1])
        return True, False, switch_block_reason
    # --branch typos used to surface via the checkout failing, which this path skips.
    if _git_run(git_cmd, ["rev-parse", "--verify", "--quiet", f"origin/{branch}"]).returncode != 0:
        print(f"✗ Branch '{branch}' does not exist locally or on origin.")
        sys.exit(1)
    print(
        f"  ℹ On branch '{current_branch}' — updating it in place from "
        f"origin/{branch} (no branch switch; local commits preserved).")
    return False, True, switch_block_reason


def _prepare_checkout_for_update(
    git_cmd, branch, current_branch, *, is_fork, assume_yes, gateway_mode, gw_input_fn,
    switch_branch, _windows_gateway_resume):
    """Parked-branch guard, land on the target, stash, count new commits. Exits when the
    checkout is unsafe to move or the target is missing. ``commit_count`` is 0 when up to
    date, -1 when tips differ but the shallow count is unrecoverable."""
    parked_branch_switched, in_place_update, switch_block_reason = _apply_parked_branch_guard(
        git_cmd, branch, current_branch, switch_branch=switch_branch,
        _windows_gateway_resume=_windows_gateway_resume)

    if not in_place_update and current_branch == "HEAD" != branch:
        print(f"  ⚠ Currently on detached HEAD — switching to {branch} for update...")
    auto_stash_ref = _m()._stash_local_changes_if_needed(git_cmd, _m().PROJECT_ROOT)
    if (
        not in_place_update and current_branch != branch
        and _git_run(git_cmd, ["checkout", branch]).returncode != 0):
        track_result = _git_run(git_cmd, ["checkout", "-B", branch, f"origin/{branch}"])
        if track_result.returncode != 0:
            # Restore the stash before bailing so the user isn't stranded.
            if auto_stash_ref is not None:
                _m()._restore_stashed_changes(
                    git_cmd, _m().PROJECT_ROOT, auto_stash_ref, prompt_user=False, input_fn=gw_input_fn)
            print(f"✗ Branch '{branch}' does not exist locally or on origin.")
            if track_result.stderr.strip():
                print(f"  {track_result.stderr.strip().splitlines()[0]}")
            sys.exit(1)

    prompt_for_restore = (
        auto_stash_ref is not None
        and not assume_yes
        and (gateway_mode or (sys.stdin.isatty() and sys.stdout.isatty())))

    # On shallow checkouts `rev-list --count` can report the entire remote ancestry. The
    # zero/nonzero gate is still sound; treat the shallow NUMBER as unknown and recover it
    # via the GitHub compare API when possible.
    result = _git_run(git_cmd, ["rev-list", f"HEAD..origin/{branch}", "--count"], check=True)
    commit_count = int(result.stdout.strip())

    apply_is_shallow = _is_shallow_checkout(git_cmd)
    if commit_count > 0 and apply_is_shallow:
        from hermes_cli.banner import _github_compare_behind
        counted = _github_compare_behind(*_tip_shas(git_cmd, f"origin/{branch}"))
        # counted == 0 means local-ahead: falls through to the up-to-date path.
        commit_count = counted if counted is not None else -1

    # A fork can match origin yet trail upstream, so the sync can move HEAD with
    # commit_count == 0; detect that BEFORE the no-update return so deps, restarts AND the
    # fleet matrix still run (it used to live in the early-return branch and verified nothing).
    # The sync can therefore advance HEAD even though the origin comparison found no commits. Detect that
    # BEFORE taking the no-update return so dependency refreshes, gateway restarts, AND the fleet version
    # matrix still run for the pulled code (#73108 — previously the sync lived inside the commit_count == 0
    # branch, which returns immediately after: an update that pulled hundreds of upstream commits printed
    # "Already up to date!" and verified nothing). Non-fork checkouts have no upstream question: origin IS
    # the official repo, so "Already up to date!" is fully verified there.
    upstream_checked = True
    if commit_count == 0 and is_fork and branch == "main":
        pre_sync_sha = _capture_head_sha(git_cmd, _m().PROJECT_ROOT)
        upstream_checked = _m()._sync_with_upstream_if_needed(
            git_cmd, _m().PROJECT_ROOT, assume_yes=assume_yes, input_fn=gw_input_fn)
        post_sync_sha = _capture_head_sha(git_cmd, _m().PROJECT_ROOT)
        if pre_sync_sha and post_sync_sha and pre_sync_sha != post_sync_sha:
            synced_count = _count_commits_between(
                git_cmd, _m().PROJECT_ROOT, pre_sync_sha, post_sync_sha)
            # HEAD moving is proof of an update even if the count can't be read.
            commit_count = max(1, synced_count)

    return _CheckoutPlan(
        auto_stash_ref=auto_stash_ref, commit_count=commit_count, in_place_update=in_place_update,
        parked_branch_switched=parked_branch_switched, prompt_for_restore=prompt_for_restore,
        switch_block_reason=switch_block_reason, upstream_checked=upstream_checked)


@dataclass
class _UpdateOptions:
    """Resolved ``hermes update`` inputs (flags, config, pre-update snapshots)."""

    active_lazy_features: object
    active_tool_dependencies: object
    pre_update_version: object
    gw_input_fn: object
    assume_yes: bool
    keep_stash: bool
    switch_branch: bool
    discard_local_changes: bool
    no_gateway_restart: bool = False


def _resolve_update_options(args, gateway_mode: bool) -> _UpdateOptions:
    """Snapshot pre-update state and resolve the flags/config ``_cmd_update_impl`` runs on."""
    # Snapshot before a managed-runtime refresh can replace site-packages, while the old
    # environment can still prove which optional backends were active.
    active_lazy_features = _m()._capture_active_lazy_features()
    active_tool_dependencies = _m()._capture_active_tool_dependencies()

    # Captured before any pull so the completion line can report the transition.
    # Snapshot the pre-update version before files are replaced so the completion line can report the
    # transition (prime-agent#630 port).
    # Snapshot the pre-update version before any code is pulled so the completion line can report the
    # transition (prime-agent#630 port).
    pre_update_version = _read_project_version()
    gw_input_fn = (
        (lambda prompt, default="": _gateway_prompt(prompt, default)) if gateway_mode else None)
    assume_yes = bool(getattr(args, "yes", False))
    # --keep-stash (desktop updater): never re-apply the autostash; only when an update
    # landed — abort/no-op paths still restore since the tree is unchanged.
    keep_stash = bool(getattr(args, "keep_stash", False))
    # --switch-branch: prefer switching over an in-place merge so an update never writes the
    # branch's history; only meaningful with parked_branch_strategy "update_in_place".
    # See #89507.
    switch_branch = bool(getattr(args, "switch_branch", False))
    # --no-gateway-restart (cron inside the gateway's own cgroup): update code
    # and dependencies but defer the fleet restart so the updater is not killed
    # by its own restart. The pending-restart marker is kept for catch-up.
    no_gateway_restart = bool(getattr(args, "no_gateway_restart", False))

    # Interactive terminals always stash-and-ask; only non-interactive updates consult
    # updates.non_interactive_local_changes (auto-restore vs discard).
    # Validate the section for interactive and non-interactive runs alike; an
    # invalid update policy must not silently select a different path.
    updates = _updates_config()
    discard_local_changes = False
    if gateway_mode or assume_yes or not (sys.stdin.isatty() and sys.stdout.isatty()):
        _mode = str(updates.get("non_interactive_local_changes", "stash")).lower()
        discard_local_changes = _mode == "discard"
    return _UpdateOptions(
        active_lazy_features=active_lazy_features,
        active_tool_dependencies=active_tool_dependencies, pre_update_version=pre_update_version,
        gw_input_fn=gw_input_fn, assume_yes=assume_yes, keep_stash=keep_stash,
        switch_branch=switch_branch, discard_local_changes=discard_local_changes,
        no_gateway_restart=no_gateway_restart)


def _begin_update_receipt_and_plan(args):
    """Open the receipt, snapshot the fleet, refuse on Windows shim holders. Returns the
    pre-update plan (None if the probe failed); ``sys.exit(2)`` when a non-gateway hermes.exe
    holds the venv shim."""
    # Structured receipt: record what this run discovers/does/skips so silent failures are diagnosable.
    with _best_effort('Update receipt unavailable: %s'):
        # See #74973, #81193, #85753, #88848, #91277.
        from hermes_cli.update_receipt import begin_update_receipt
        begin_update_receipt()

    # Plan phase: snapshot runtimes/supervisors/version (read-only; probe failure records
    # nothing). Re-read AFTER the restart phase to reconcile — the plan is the worklist.
    # Plan phase (#91277 Phase 2): snapshot the pre-update fleet — every running Hermes runtime, its
    # supervisor, and its running code version — into the receipt, so a post-mortem can compare what the
    # update SAW against what it did. ``_pre_update_plan`` is read again AFTER the restart phase to
    # reconcile every planned runtime against the phase's bookkeeping (restart via declared mechanism — the
    # plan is the worklist, not just a printout).
    _pre_update_plan = None
    with _best_effort('Update plan phase failed: %s'):
        from hermes_cli.update_inventory import collect_runtime_inventory, record_plan_in_receipt
        _pre_update_plan = collect_runtime_inventory()
        record_plan_in_receipt(_pre_update_plan)
        if _pre_update_plan.runtimes:
            _n = len(_pre_update_plan.runtimes)
            _profiles = ", ".join(sorted({r.profile for r in _pre_update_plan.runtimes}))
            print(f"→ Fleet: {_n} running service(s) across profiles: {_profiles}")

    # Windows: another hermes.exe holding the venv shim means WinError 32 spam and a
    # deferred-rename leftover or silent ZIP fallback. Positively identified gateways are
    # paused/restarted by the update instead; anything else still aborts.
    # Continuing would result in a string of WinError 32 warnings and then either a deferred-rename leftover
    # or a failed git-pull fast path that silently falls back to the slower ZIP route. See issue #26670.
    # Exception (#37039): when every concurrent instance is a gateway runtime, the pause machinery a few
    # lines below (``_pause_windows_gateways_for_update``) stops it before any file mutation, and the
    # post-update restart phase brings it back. Aborting just to make the user run the same kill manually is
    # friction without benefit. Anything not positively identified as a gateway (TUI shell, Desktop backend
    # child, unreadable cmdline) still aborts exactly as before.
    if _m()._is_windows() and not getattr(args, "force", False):
        scripts_dir = _m()._venv_scripts_dir()
        concurrent = _m()._detect_concurrent_hermes_instances(scripts_dir) if scripts_dir is not None else []
        non_gateway = _m()._filter_non_gateway_concurrent_instances(concurrent) if concurrent else []
        if non_gateway:
            print(_format_concurrent_instances_message(non_gateway, scripts_dir))
            sys.exit(2)
    return _pre_update_plan


def _prepare_git_command() -> tuple[bool, list, bool]:
    """Return ``(use_zip_update, git_cmd, is_fork)``; ``sys.exit(1)`` when not a git repo
    on a non-Windows host (Windows falls back to ZIP: broken git file I/O, AV, NTFS filters)."""
    git_dir = _m().PROJECT_ROOT / ".git"
    use_zip_update = not git_dir.exists()
    if use_zip_update and sys.platform != "win32":
        print("✗ Not a git repository. Please reinstall:")
        print("  curl -fsSL https://hermes-agent.nousresearch.com/install.sh | bash")
        sys.exit(1)

    git_cmd = _base_git_cmd()
    if sys.platform == "win32" and git_dir.exists():
        _git_run(git_cmd, ["config", "windows.appendAtomically", "false"])
    # A broken Git-for-Windows trampoline refuses every call with a "BUG (fork bomb)" guard;
    # swap in a real binary up front so git survives instead of degrading to ZIP.
    # See #87876.
    git_cmd = _ensure_non_trampoline_git(git_cmd)

    # Before stash/branch logic: npm rewrites package-lock.json non-deterministically and
    # line-ending churn is machine-made dirt; both would otherwise force an autostash every update.
    _discard_lockfile_churn(git_cmd, _m().PROJECT_ROOT)
    _normalize_managed_eol(git_cmd, _m().PROJECT_ROOT)

    origin_url = _m()._get_origin_url(git_cmd, _m().PROJECT_ROOT)
    is_fork = _is_fork(origin_url)

    if is_fork:
        print("⚠ Updating from fork:")
        print(f"  {origin_url}")
        print()
    return use_zip_update, git_cmd, is_fork


def _verify_head_after_pull(
    git_cmd, branch: str, pre_pull_sha, *, in_place_update: bool, _windows_gateway_resume
) -> str | None:
    """Return the post-pull HEAD SHA; ``sys.exit(1)`` if the pull was a no-op or landed off-branch."""
    # A detached checkout pinned to a SHA can report "N new commit(s)" and a successful
    # merge --ff-only yet stay put; surface the no-op instead of claiming "Code updated!".
    # Verify HEAD actually moved (issue #79678). ``merge --ff-only`` succeeding only means the merge
    # completed, not that the update applied: a checkout that is pinned to a raw SHA (detached HEAD) can
    # report "N new commit(s)" against origin yet still sit on the old commit afterward (the branch-switch
    # step re-detaches to the SHA). Before this guard, ``hermes update`` printed "✓ Code updated!" and
    # reinstalled deps + rebuilt the desktop app against the stale tree — no error, no warning, ``hermes
    # doctor`` healthy. Compare pre-pull and post-pull HEAD; if they match, surface the no-op instead of
    # claiming success.
    post_pull_sha = _capture_head_sha(git_cmd, _m().PROJECT_ROOT)
    if pre_pull_sha and post_pull_sha == pre_pull_sha:
        print()
        print("✗ Code did not move — update was a no-op.")
        print(
            f"  HEAD is pinned to {pre_pull_sha[:10]} (detached checkout); "
            f"origin/{branch} advanced but the working tree stayed put.")
        print(
            "  Reattach to the branch and retry: "
            f"git -C {_m().PROJECT_ROOT} checkout {branch} && hermes update")
        _m()._resume_windows_gateways_after_update(_windows_gateway_resume)
        sys.exit(1)

    # HEAD must be on the target or "Code updated!" is a lie; an IN-PLACE update is the one
    # legitimate exception (origin/<target> merged INTO the checked-out branch).
    post_pull_branch = _current_branch_name(git_cmd)
    if not in_place_update and post_pull_branch and post_pull_branch not in {branch, "HEAD"}:
        print()
        print(
            f"✗ Update pulled origin/{branch}, but the checkout is on "
            f"'{post_pull_branch}' — not claiming success.")
        print(
            "  Switch to the target branch and retry: "
            f"git -C {_m().PROJECT_ROOT} checkout {branch} && hermes update")
        _m()._resume_windows_gateways_after_update(_windows_gateway_resume)
        sys.exit(1)
    return post_pull_sha


def _current_branch_name(git_cmd, *, check: bool = False) -> str:
    """``rev-parse --abbrev-ref HEAD`` (literal "HEAD" when detached)."""
    return _git_run(git_cmd, ["rev-parse", "--abbrev-ref", "HEAD"], check=check).stdout.strip()


def _handle_update_called_process_error(
    e, args, gateway_mode: bool, had_desktop_app_before_update: bool,
    _windows_gateway_resume=None) -> None:
    """Git/installer failure: ZIP-fallback when safe, else report and ``sys.exit(1)``."""
    stage = _format_update_failure_stage(e)
    if _should_zip_fallback_on_update_error(e):
        print(f"⚠ {stage}: {e}")
        print("→ Falling back to ZIP download...")
        print()
        desktop_build_ok = _update_via_zip(
            args, had_desktop_app_before_update=had_desktop_app_before_update,
            _windows_gateway_resume=_windows_gateway_resume)
        if gateway_mode:
            _write_gateway_update_exit_code(desktop_build_ok)
    else:
        if _called_process_error_is_python_dep_install(e):
            print(f"✗ {stage} (the code update itself succeeded).")
            _print_called_process_error_tail(e)
            print()
            print("  Hermes may not start until the dependencies are installed. Fix the error above")
            print("  (usually network or disk space), then run `hermes update` again.")
            if _m()._is_windows():
                print("  If `hermes update` itself will not start, retry through the venv interpreter:")
                print(
                    '    venv\\Scripts\\python.exe -c '
                    '"from hermes_cli.main import main; main()" update --yes')
        else:
            print(f"✗ {stage}.")
            print(f"  Details: {e}")
            _print_called_process_error_tail(e)
        _finalize_receipt("failed", 'Update receipt finalize failed: %s')
        sys.exit(1)


def _finalize_receipt(status: str, debug_message: str) -> None:
    """Best-effort ``finalize_update_receipt(status)``; the receipt must never break an update."""
    with _best_effort(debug_message):
        from hermes_cli.update_receipt import finalize_update_receipt
        finalize_update_receipt(status)


@dataclass(frozen=True)
class _ReleaseReconcileState:
    enabled: bool
    current: str  # absent, equal, different
    candidate: str  # none, staged, failed-partial
    journal: str  # none, in-progress, done, rolled-back
    service: str  # none, source, current, stale-release
    running: str  # none, source, current, other (root AND SHA)
    defer: bool
    pending_transaction: bool = False


# Ordered partial states. Each row names only the observations it constrains;
# the first matching row owns the transition.
_RELEASE_RECONCILE_TABLE = (
    ({"pending_transaction": True}, "complete-transaction"),
    ({"enabled": False, "current": "absent"}, "no-op"),
    ({"current": "equal", "candidate": "none"}, "fail-with-message"),
    ({"current": "equal", "candidate": "failed-partial"}, "fail-with-message"),
    ({"current": "equal", "service": "none", "running": "none"}, "no-op"),
    ({"current": "equal", "service": "current", "running": "none"}, "no-op"),
    ({"current": "equal", "service": "none", "running": "current"}, "no-op"),
    ({"current": "equal", "service": "current", "running": "current"}, "no-op"),
    ({"current": "equal", "defer": True}, "defer-record"),
    ({"current": "equal", "defer": False}, "repair-service"),
    ({"defer": True}, "defer-record"),
    ({"candidate": "staged", "defer": False}, "activate-staged"),
    ({"defer": False}, "build+activate"),
)


def _reconcile_immutable_release(state: _ReleaseReconcileState) -> str:
    """Resolve one no-pull transition; reject impossible combinations explicitly."""
    if (not state.pending_transaction and
            ((state.current == "absent" and (state.service == "current" or state.running == "current"))
             or (state.current == "different" and state.journal == "none"))):
        raise ValueError(f"unreachable immutable release state: {state}")
    for constraints, action in _RELEASE_RECONCILE_TABLE:
        if all(getattr(state, name) == want for name, want in constraints.items()):
            return action
    raise ValueError(f"unreachable immutable release state: {state}")


def _release_service_state(paths, current) -> str:
    if sys.platform != "darwin":
        return "none"
    from hermes_cli import gateway
    plist = gateway.get_launchd_plist_path()
    if not plist.exists():
        return "none"
    if current is not None and gateway.launchd_plist_is_current(
            release_target=current if current.parent == paths.releases.resolve() else None):
        return "current" if current.parent == paths.releases.resolve() else "source"
    from hermes_cli.immutable_releases import migration_plist
    journal = paths.home / "release-layout.json"
    if journal.exists():
        original = migration_plist(paths.home)
        if original and original[0] == plist and original[1] == plist.read_bytes():
            return "source"
    return "stale-release"


def _release_running_state(paths, source, current, sha) -> str:
    from hermes_cli.update_receipt import collect_fleet_versions
    rows = collect_fleet_versions(expected_sha_override=sha, expected_root_override=paths.release(sha))
    if not rows:
        return "none"
    roots = {(row.get("code_root"), row.get("code_sha")) for row in rows}
    if roots == {(str(source.resolve()), sha)}:
        return "source"
    if current is not None and roots == {(str(current), sha)}:
        return "current"
    return "other"


def _catch_up_immutable_release(*, defer: bool, sha: str | None = None,
                                source: Path | None = None) -> None:
    """Reconcile source, artifact, journal, service and gateway before fleet catch-up."""
    from hermes_cli.immutable_releases import ReleasePaths, read_pointer, release_sha, _release_is_ready
    paths = ReleasePaths.for_home(get_hermes_home())
    if defer and (paths.home / "release-txn.json").exists():
        message = "Pending release transaction requires a restart-authorized run; restart-prohibited reconciliation cannot replay it"
        _record_update_step("immutable_release_catchup", False, message)
        _finalize_receipt("partial", "Release transaction remains pending: %s")
        raise SystemExit(message)
    if not _immutable_release_enabled(paths):
        return
    current = read_pointer(paths.current)
    source = source or _m().PROJECT_ROOT
    sha = sha or release_sha(source)
    candidate = paths.release(sha)
    candidate_state = ("staged" if _release_is_ready(candidate, sha)
                       else "failed-partial" if candidate.exists() else "none")
    journal_path = paths.home / "release-layout.json"
    record = json.loads(journal_path.read_text(encoding="utf-8")) if journal_path.exists() else {}
    journal = record.get("state", "done" if current is not None else "in-progress" if record else "none")
    if journal not in {"none", "in-progress", "done", "rolled-back"}:
        raise RuntimeError(f"invalid release migration state: {journal}")
    state = _ReleaseReconcileState(
        _immutable_release_enabled(paths),
        "absent" if current is None or journal == "rolled-back" else "equal" if current == candidate else "different",
        candidate_state, journal, _release_service_state(paths, current),
        _release_running_state(paths, source, current, sha), defer,
        (paths.home / "release-txn.json").exists(),
    )
    action = _reconcile_immutable_release(state)
    if action == "complete-transaction":
        _finish_pending_release_transaction(paths.home)
        return
    if action == "fail-with-message":
        print(f"✗ Immutable release needs manual recovery: current={current}, candidate={candidate_state}, journal={journal}. "
              "Do not restart the gateway until the release pointer and migration record are repaired.")
        _record_update_step("immutable_release_catchup", False, f"manual recovery required: {state}")
        _finalize_receipt("partial", "Release reconciliation requires recovery: %s")
        raise SystemExit(1)
    if action == "no-op":
        return
    if action == "defer-record":
        if state.current != "equal" and state.candidate != "staged":
            if not _activate_immutable_release(defer=True, sha=sha, source=source):
                _record_update_step("immutable_release_catchup", False, "deferred stage failed")
                _finalize_receipt("partial", "Release deferred stage failed: %s")
                raise SystemExit(1)
        _record_update_step("immutable_release_catchup", True, f"deferred: {state}")
        print(f"  Release {sha} still awaits reconciliation; --no-gateway-restart keeps the running gateway active.")
        return
    if action == "repair-service":
        from hermes_cli import gateway
        if state.service not in {"none", "current"}:
            from hermes_cli import gateway_launchd
            from hermes_cli.immutable_releases import activate_release
            plist = gateway.get_launchd_plist_path()
            if current is None or not plist.exists():
                raise RuntimeError("cannot repair launchd service without current release and plist")
            body = gateway.generate_launchd_plist(release_target=current).encode("utf-8")
            result = activate_release(paths.home, current, plist_path=plist, plist_body=body,
                                      reload_callback=lambda: gateway_launchd._reload_installed_launchd_plist(plist),
                                      force_reload=True)
            if result.get("reload_pending"):
                if _await_release_acknowledgement(paths.home):
                    result = _finish_pending_release_transaction(paths.home) or {key: value for key, value in result.items()
                                                                    if key != "reload_pending"}
                if result.get("reload_pending"):
                    _record_update_step("immutable_release_catchup", False, "service reload awaiting gateway acknowledgement within the configured observation window")
                    _finalize_receipt("partial", "Release service repair remains pending: %s")
                    raise SystemExit(1)
        if state.running not in {"none", "current"}:
            _write_fleet_restart_pending_marker(expected_sha=sha)
        _record_update_step("immutable_release_catchup", True, f"repaired service/runtime: {state}")
        return
    # A partial build is not ready. Stage from the exact commit without ever
    # building in or syncing the source checkout.
    if not _activate_immutable_release(sha=sha, source=source):
        _record_update_step("immutable_release_catchup", False, f"candidate={sha}")
        _finalize_receipt("partial", "Release catch-up failed: %s")
        raise SystemExit(1)
    _write_fleet_restart_pending_marker(expected_sha=sha)
    _record_update_step("immutable_release_catchup", True, f"{action}: {sha} before fleet restart")


def _finish_already_up_to_date(
    git_cmd, branch: str, current_branch: str, _plan, *, assume_yes: bool, gateway_mode: bool,
    gw_input_fn, pre_update_snapshot_id, had_desktop_app_before_update: bool,
    active_lazy_features, active_tool_dependencies, _windows_gateway_resume,
    no_gateway_restart: bool = False) -> None:
    """"Already up to date" path: restore stash/branch, repair the checkout, catch up the fleet.
    ``sys.exit(1)`` when the repair is incomplete (after gateway exit code + partial receipt)."""
    _invalidate_update_cache()

    # Restore stash and switch back if we moved. EXCEPTION: a parked branch verified clean +
    # fully merged stays on the target — re-parking on the stale branch recreates the incident.
    if _plan.auto_stash_ref is not None:
        _m()._restore_stashed_changes(
            git_cmd, _m().PROJECT_ROOT, _plan.auto_stash_ref, prompt_user=_plan.prompt_for_restore,
            input_fn=gw_input_fn)
    if _plan.parked_branch_switched:
        if _plan.switch_block_reason.startswith("unmerged:"):
            _count = _plan.switch_block_reason.split(":", 1)[1]
            print(
                f"  ✓ Checkout was parked on '{current_branch}' — switched back to {branch}; "
                f"{_count} unmerged commit(s) kept on '{current_branch}'.")
        else:
            print(f"  ✓ Checkout was parked on '{current_branch}' (fully merged) — switched back to {branch}.")
    elif current_branch not in {branch, "HEAD"}:
        _git_run(git_cmd, ["checkout", current_branch])

    current_checkout_complete = _repair_current_checkout(
        assume_yes=assume_yes, gateway_mode=gateway_mode,
        pre_update_snapshot_id=pre_update_snapshot_id,
        had_desktop_app_before_update=had_desktop_app_before_update,
        active_lazy_features=active_lazy_features,
        active_tool_dependencies=active_tool_dependencies, upstream_checked=_plan.upstream_checked,
        _windows_gateway_resume=_windows_gateway_resume)
    # Same contract as the pull path's _resume_windows_gateways_and_merge_outcome: a failed
    # Windows gateway resume (e.g. the relaunch verification racing a Job-Object kill, #48820)
    # must demote this run to incomplete, never abort it. A bare call here let the identical
    # RuntimeError the pull path treats as a warning kill "Already up to date" outright (#115563).
    resume_outcome = _GatewayRestartOutcome(
        incomplete=False, phase_errors=[], pre_restart_gateway_pids=[],
        restarted_services=[], failed_or_stale_units=[], relaunched_profiles=[],
        externally_supervised_profiles=[], killed_pids=set(),
    )
    _resume_windows_gateways_and_merge_outcome(resume_outcome, _windows_gateway_resume, gateway_mode)
    if resume_outcome.incomplete:
        current_checkout_complete = False
    # A prior pull may still owe the fleet a restart; catch up here too, BEFORE the exit
    # gate so a partial outcome can't strand the fleet on stale code.
    # Catch up even on the "Already up to date" path — that early return is what left the gateway on stale
    # code for two days. Runs BEFORE the runtime-verification exit gate below: a vulnerable SQLite runtime
    # demotes the outcome to partial, but must not strand the fleet on stale code (#91277 fleet contract —
    # the pending-restart check always executes). Under --no-gateway-restart the
    # catch-up is deferred instead (executing it would kill the cron's own gateway).
    # A deferred/failed release promotion is a separate obligation from the
    # pending fleet restart; never restart A when source HEAD already names B.
    _catch_up_immutable_release(defer=no_gateway_restart)
    _apply_pending_fleet_restart_catchup(
        defer=no_gateway_restart, checkout_complete=current_checkout_complete)
    if not current_checkout_complete:
        if gateway_mode:
            _write_gateway_update_exit_code(False)
        _finalize_receipt("partial", 'Update receipt finalize (current checkout) failed: %s')
        sys.exit(1)
    # No new commit was pulled: never migrate/reload launchd on a repair/no-op.
    # The pending fleet restart above remains the sole catch-up authority.


def _apply_fetched_immutable_update(git_cmd, branch, opts, args, *, gateway_mode,
                                    had_desktop_app_before_update, pre_update_snapshot_id,
                                    _pre_update_plan, _windows_gateway_resume):
    """Fetch-only release path. Source HEAD, files and venv are never updated."""
    from hermes_cli.immutable_releases import (
        ReleasePaths, read_pointer, release_sha, stage_release, _release_is_ready,
    )
    source = _m().PROJECT_ROOT.resolve()
    home = get_hermes_home()
    paths = ReleasePaths.for_home(home)
    fetched = _git_run(git_cmd, ["rev-parse", "--verify", "FETCH_HEAD^{commit}"], check=True).stdout.strip()
    if not re.fullmatch(r"[0-9a-fA-F]{40}", fetched):
        raise RuntimeError("fetch did not resolve to a complete commit SHA")
    current = read_pointer(paths.current)
    if current is not None and current.parent != paths.releases.resolve():
        raise RuntimeError("current is not a release; remove the obsolete source pointer before updating")
    if current is not None and not _release_is_ready(current, current.name):
        raise RuntimeError("current release is not ready; refusing to activate from an invalid pointer")
    # Migration metadata and the source plist are captured with the candidate
    # inside one transaction at activation, not before staging/handoff.
    # A deferred candidate must be promoted on the next normal update, even
    # when the remote has not advanced beyond that exact staged commit.
    if current is not None and current.name == fetched:
        _catch_up_immutable_release(defer=opts.no_gateway_restart, sha=fetched, source=source)
        _apply_pending_fleet_restart_catchup(defer=opts.no_gateway_restart)
        return
    # stage_release archives FETCH_HEAD itself, builds web and its own locked
    # environment in a build-aside directory, then atomically publishes it.
    candidate, _ = stage_release(source, home, sha=fetched)
    if candidate != paths.release(fetched) or not _release_is_ready(candidate, fetched):
        raise RuntimeError("staged release does not match fetched commit")
    _write_fleet_restart_pending_marker(
        expected_sha=fetched,
        runtimes=_pre_update_plan.to_dict().get("runtimes") if _pre_update_plan is not None else None,
    )
    _hand_off_post_swap(
        args, swap="immutable", branch=branch, pre_pull_sha=release_sha(source),
        is_fork=False, opts=opts, gateway_mode=gateway_mode,
        had_desktop_app_before_update=had_desktop_app_before_update,
        pre_update_snapshot_id=pre_update_snapshot_id, _pre_update_plan=_pre_update_plan,
        _windows_gateway_resume=_windows_gateway_resume, release=candidate, source=source,
    )


def _apply_pulled_update(
    git_cmd, branch, pre_pull_sha, _plan, opts, *, gateway_mode, is_fork, desktop_dir,
    had_desktop_app_before_update, pre_update_snapshot_id, _pre_update_plan,
    _windows_gateway_resume, args) -> None:
    """Post-pull phase, pre-swap half: verify HEAD, arm the fleet marker, sweep bytecode, then
    hand the rest of the run to an interpreter born on the pulled code (never returns)."""
    _invalidate_update_cache()
    post_pull_sha = _verify_head_after_pull(
        git_cmd, branch, pre_pull_sha, in_place_update=_plan.in_place_update,
        _windows_gateway_resume=_windows_gateway_resume)

    # Gateways still serve pre-pull modules until the restart phase; an interrupt before a
    # completed restart leaves this marker so the next update catches up even when git is
    # current. Distinct from ``.update-incomplete`` (venv/install repair).
    # See #95294.
    _write_fleet_restart_pending_marker(
        expected_sha=post_pull_sha or "",
        runtimes=_pre_update_plan.to_dict().get("runtimes") if _pre_update_plan is not None else None,
    )
    # Stale .pyc would ImportError on gateway restart when new source references new names.
    _sweep_bytecode_after_update(branch)

    _hand_off_post_swap(
        args, swap="git", branch=branch, pre_pull_sha=pre_pull_sha, is_fork=is_fork, opts=opts,
        gateway_mode=gateway_mode, had_desktop_app_before_update=had_desktop_app_before_update,
        pre_update_snapshot_id=pre_update_snapshot_id, _pre_update_plan=_pre_update_plan,
        _windows_gateway_resume=_windows_gateway_resume)


# ``store_true`` update flags the post-swap child must see exactly as the user passed them.
_POST_SWAP_FORWARDED_FLAGS = (
    ("gateway", "--gateway"), ("no_backup", "--no-backup"), ("backup", "--backup"),
    ("yes", "--yes"), ("keep_stash", "--keep-stash"), ("switch_branch", "--switch-branch"),
    ("force", "--force"), ("force_venv", "--force-venv"),
    ("no_gateway_restart", "--no-gateway-restart"),
)


def _post_swap_argv_tail(args) -> list[str]:
    tail = [flag for attr, flag in _POST_SWAP_FORWARDED_FLAGS if getattr(args, attr, False)]
    branch = getattr(args, "branch", None)
    if branch:
        tail += ["--branch", str(branch)]
    return tail


def _post_swap_payload(
    *, swap: str, branch: str, opts, gateway_mode: bool, had_desktop_app_before_update: bool,
    pre_pull_sha=None, is_fork: bool = False, pre_update_snapshot_id=None, _pre_update_plan=None,
    _windows_gateway_resume=None, release=None, source=None) -> dict:
    """Everything the post-swap tail needs that only the pre-swap process could observe: the
    open receipt (detached here — the child resumes it), the pre-update fleet plan, the
    pre-update version and active features, the Windows pause token. Flags cross as argv."""
    from hermes_cli.update_receipt import detach_update_receipt

    return {
        "swap": swap, "branch": branch, "pre_pull_sha": pre_pull_sha, "is_fork": bool(is_fork),
        "release": str(release) if release else None, "candidate_sha": Path(release).name if release else None,
        "source": str(source) if source else None,
        "source_python": sys.executable if swap == "immutable" else None,
        "gateway_mode": bool(gateway_mode),
        "had_desktop_app_before_update": bool(had_desktop_app_before_update),
        "pre_update_snapshot_id": pre_update_snapshot_id,
        "pre_update_version": opts.pre_update_version,
        "active_lazy_features": opts.active_lazy_features,
        "active_tool_dependencies": opts.active_tool_dependencies,
        "plan": _pre_update_plan.to_dict() if _pre_update_plan is not None else None,
        "windows_gateway_resume": _windows_gateway_resume,
        # {profile: snapshot_id} from the pre-update backup; the post-migration safety nets for
        # sibling profiles read it (update_cmd_config._LAST_SIBLING_SNAPSHOTS).
        "sibling_snapshots": dict(_sibling_snapshots_module()._LAST_SIBLING_SNAPSHOTS),
        "receipt": detach_update_receipt(),
    }


def _sibling_snapshots_module():
    # The backup phase REBINDS ``update_cmd_config._LAST_SIBLING_SNAPSHOTS``; read the module
    # attribute at call time, never this module's import-time copy of the empty dict.
    import hermes_cli.update_cmd_config as _cfg
    return _cfg


def _hand_off_post_swap(args, **payload_kwargs) -> None:
    """Re-execute ``hermes update --post-swap`` on the pulled tree and exit with its code.

    The parent detaches from the receipt and its Windows resume hook — the child owns both —
    and only relays the exit code (``hermes_cli/update_handoff.py``).
    """
    from hermes_cli.update_receipt import resume_update_receipt

    payload = _post_swap_payload(**payload_kwargs)
    code = _update_handoff.continue_update_in_fresh_interpreter(
        payload, argv_tail=_post_swap_argv_tail(args))
    token = payload_kwargs.get("_windows_gateway_resume")
    if token and code is not None:
        # The child got its own copy (serialized before this flip) and owns the resume; every
        # parent-side hook (atexit, the ZIP path's ``finally``) reads this flag and stays out
        # of the way. When no child ran, the parent still resumes what it paused.
        token["resume_needed"] = False
    if code is None:
        # No child ran: take the receipt back so this failure is recorded, and leave the
        # install breadcrumb so the next launch finishes the dependency sync (new code, old deps).
        if payload["receipt"]:
            resume_update_receipt(payload["receipt"])
        _record_update_step("post_swap_handoff", False, "child interpreter could not start")
        _m()._write_update_incomplete_marker()
        if payload_kwargs.get("gateway_mode"):
            _write_gateway_update_exit_code(False)
        code = 1
    sys.exit(code)


def _run_post_swap_phase(args, gateway_mode: bool) -> None:
    """Child half of the update (``--post-swap``): resume the receipt and finish the run on the
    pulled code."""
    from hermes_cli.update_receipt import resume_update_receipt

    payload = _update_handoff.read_handoff(args.post_swap)
    with suppress(OSError):
        Path(args.post_swap).unlink()
    if payload.get("receipt"):
        resume_update_receipt(payload["receipt"])
    _execute_post_swap(payload, args, gateway_mode)


def _immutable_phase_error(phase: str, release: Path, home: Path, reason: str = "") -> str:
    detail = f"{phase} failed for release {release.name} ({release})"
    if reason:
        detail += f": {reason}"
    return (f"{detail}; evidence: {home / 'release-txn.json'}, "
            f"{home / 'logs/update_receipts/latest.json'}; "
            "recover with: hermes update (or hermes update --rollback if current is unhealthy)")


def _execute_post_swap(payload: dict, args, gateway_mode: bool) -> None:
    """The tail ``_apply_pulled_update`` / ``_update_via_zip`` used to run in the pre-pull
    interpreter, driven from a hand-off payload."""
    from dataclasses import replace as _replace
    import hermes_cli.update_cmd_config as _cfg
    from hermes_cli.update_inventory import UpdatePlan

    _cfg._LAST_SIBLING_SNAPSHOTS = dict(payload.get("sibling_snapshots") or {})
    _pre_update_plan = UpdatePlan.from_dict(payload["plan"]) if payload.get("plan") else None
    _windows_gateway_resume = payload.get("windows_gateway_resume")
    if _windows_gateway_resume:
        import atexit as _atexit
        _atexit.register(_m()._resume_windows_gateways_after_update, _windows_gateway_resume)
    # Flags and config resolve here exactly as they did pre-swap; the three pre-update
    # snapshots come from the payload (the new tree would report the NEW version).
    opts = _replace(
        _resolve_update_options(args, gateway_mode),
        pre_update_version=payload.get("pre_update_version"),
        active_lazy_features=payload.get("active_lazy_features"),
        active_tool_dependencies=payload.get("active_tool_dependencies"))
    had_desktop_app_before_update = bool(payload.get("had_desktop_app_before_update"))
    desktop_dir = _m().PROJECT_ROOT / "apps" / "desktop"

    try:
        if payload.get("swap") == "zip":
            desktop_build_ok = _finish_zip_update(
                active_tool_dependencies=opts.active_tool_dependencies,
                pre_update_version=opts.pre_update_version,
                had_desktop_app_before_update=had_desktop_app_before_update,
                _windows_gateway_resume=_windows_gateway_resume)
            if gateway_mode:
                _write_gateway_update_exit_code(desktop_build_ok)
            return
        if payload.get("swap") == "immutable":
            from hermes_cli.immutable_releases import ReleasePaths
            release = Path(payload["release"]).resolve(strict=True)
            paths = ReleasePaths.for_home(get_hermes_home())
            if release.parent != paths.releases.resolve() or release.name != payload["candidate_sha"]:
                raise RuntimeError("immutable handoff release identity mismatch")
            if _m().PROJECT_ROOT.resolve() != release:
                raise RuntimeError("immutable post-swap child did not import the staged release")
            # This child imports from the staged release. Shared-state changes
            # must run using its schema and bundled assets before that code is
            # selected by current; a failure never promotes a partial candidate.
            from hermes_cli.update_cmd_maint import strict_immutable_maintenance
            try:
                strict_immutable_maintenance(release)
                update_complete = _run_post_update_maintenance(
                    assume_yes=opts.assume_yes, gateway_mode=gateway_mode,
                    pre_update_snapshot_id=payload.get("pre_update_snapshot_id"),
                    had_desktop_app_before_update=had_desktop_app_before_update,
                    node_failures=[], desktop_build_ok=True,
                    pre_update_version=opts.pre_update_version)
            except Exception as exc:
                detail = _immutable_phase_error("immutable_maintenance", release, paths.home, str(exc))
                _record_update_step("immutable_maintenance", False, detail)
                print(f"✗ {detail}")
                if gateway_mode:
                    _write_gateway_update_exit_code(False)
                _finalize_receipt("partial", "Immutable release maintenance failed: %s")
                raise SystemExit(1) from exc
            _record_update_step("immutable_maintenance", update_complete,
                                "required shared state synchronized" if update_complete else
                                _immutable_phase_error("immutable_maintenance", release, paths.home,
                                                       "post-update maintenance incomplete"))
            if not update_complete:
                print(f"✗ {_immutable_phase_error('immutable_maintenance', release, paths.home, 'post-update maintenance incomplete')}")
                if gateway_mode:
                    _write_gateway_update_exit_code(False)
                _finalize_receipt("partial", "Immutable release maintenance incomplete: %s")
                raise SystemExit(1)
            if not _activate_immutable_release(defer=opts.no_gateway_restart,
                                                sha=release.name, source=Path(payload["source"]),
                                                source_python=Path(payload["source_python"]) if payload.get("source_python") else None):
                detail = _immutable_phase_error("immutable_activation", release, paths.home)
                _record_update_step("immutable_activation", False, detail)
                print(f"✗ {detail}")
                _finalize_receipt("partial", "Immutable release activation failed: %s")
                raise SystemExit(1)
            _record_update_step("immutable_activation", True,
                                "staged; activation deferred" if opts.no_gateway_restart else
                                f"activated release {release.name} ({release})")
            if not opts.no_gateway_restart:
                restart = _restart_gateway_fleet_after_update(
                    _pre_update_plan, gateway_mode, acknowledged_release_root=release)
                _resume_windows_gateways_and_merge_outcome(restart, _windows_gateway_resume, gateway_mode)
                _verify_fleet_after_update(
                    restart, _pre_update_plan=_pre_update_plan, _windows_gateway_resume=_windows_gateway_resume,
                    node_failures=[], update_complete=update_complete, expected_sha=release.name,
                    expected_root=release)
            else:
                _record_update_step("immutable_release", True, "activation deferred; fleet restart not requested")
                _finalize_receipt("success", "Immutable release staged: %s")
            return
        # The parent already ran the checkout preflight (fork banner, lockfile churn, EOL); the
        # child only needs a working git.
        git_cmd = _ensure_non_trampoline_git(_base_git_cmd())
        _finish_pulled_update(
            git_cmd, payload["branch"], payload.get("pre_pull_sha"), opts, gateway_mode=gateway_mode,
            is_fork=bool(payload.get("is_fork")), desktop_dir=desktop_dir,
            had_desktop_app_before_update=had_desktop_app_before_update,
            pre_update_snapshot_id=payload.get("pre_update_snapshot_id"),
            _pre_update_plan=_pre_update_plan, _windows_gateway_resume=_windows_gateway_resume)
    except _shim_quarantine_error_type() as e:
        _refuse_update_for_contended_shims(e)
    except subprocess.CalledProcessError as e:
        _handle_update_called_process_error(
            e, args, gateway_mode, had_desktop_app_before_update,
            _windows_gateway_resume=_windows_gateway_resume)


def _finish_pulled_update(
    git_cmd, branch, pre_pull_sha, opts, *, gateway_mode, is_fork, desktop_dir,
    had_desktop_app_before_update, pre_update_snapshot_id, _pre_update_plan,
    _windows_gateway_resume) -> None:
    """Post-swap tail (git path): sync Python/Node/web/Desktop, maintenance, fleet restart."""
    if is_fork and branch == "main":
        _m()._sync_with_upstream_if_needed(
            git_cmd, _m().PROJECT_ROOT, assume_yes=opts.assume_yes, input_fn=opts.gw_input_fn)

    # .[all], falling back to base + extras individually so one broken extra doesn't strip
    # the rest; the ownership preflight refuses first on foreign-owned (sudo-pip) venv files.
    _sync_python_dependencies_after_pull(
        git_cmd, branch, pre_pull_sha, active_lazy_features=opts.active_lazy_features,
        active_tool_dependencies=opts.active_tool_dependencies,
        _windows_gateway_resume=_windows_gateway_resume)
    node_failures = _update_node_dependencies()
    web_build_ok = _m()._build_web_ui(_m().PROJECT_ROOT / "web")
    # Legacy post-swap work never activates a release. Immutable layouts are
    # selected before the code swap; a non-ready current pointer is refused by
    # _immutable_release_enabled rather than falling through to this path.

    desktop_build_ok = _rebuild_desktop_after_update(
        desktop_dir, had_desktop_app_before_update=had_desktop_app_before_update)

    print()
    print(f"✓ Code updated!{_branch_head_suffix(git_cmd, _m().PROJECT_ROOT)}")

    update_complete = _run_post_update_maintenance(
        assume_yes=opts.assume_yes, gateway_mode=gateway_mode,
        pre_update_snapshot_id=pre_update_snapshot_id,
        had_desktop_app_before_update=had_desktop_app_before_update,
        node_failures=node_failures, desktop_build_ok=desktop_build_ok,
        pre_update_version=opts.pre_update_version)

    # Exit code *before* the restart: under --gateway this process lives in the gateway's
    # systemd cgroup and the systemctl-restart fallback SIGKILLs it (KillMode=mixed), so
    # the marker would never land and the new gateway's watcher would time out spuriously.
    if gateway_mode:
        _write_gateway_update_exit_code(update_complete)

    if opts.no_gateway_restart:
        # Cron inside the gateway's own cgroup: restarting the fleet now would
        # SIGUSR1-drain this updater's own gateway and systemd would SIGKILL the
        # updater with it. Defer instead — the pending marker written above is
        # kept for the next normal update. Skipping the restart phase also skips
        # its stale-module purge: this live interpreter still serves pre-update
        # code and must not have its sys.modules graph mutated mid-flight.
        # Windows pause/resume still runs (paused gateways must be resumed onto
        # pre-update code); only the fleet restart + verification are deferred.
        # The resume outcome is RETAINED (not discarded): a failed resume must
        # still make this update partial, exactly as on the normal path.
        resume_outcome = _GatewayRestartOutcome(
            incomplete=False, phase_errors=[], pre_restart_gateway_pids=[],
            restarted_services=[], failed_or_stale_units=[], relaunched_profiles=[],
            externally_supervised_profiles=[], killed_pids=set(),
        )
        _resume_windows_gateways_and_merge_outcome(
            resume_outcome, _windows_gateway_resume, gateway_mode)
        _defer_fleet_restart_after_update(
            update_complete=update_complete, resume_incomplete=resume_outcome.incomplete)
        return

    _restart = _restart_gateway_fleet_after_update(_pre_update_plan, gateway_mode)
    _resume_windows_gateways_and_merge_outcome(_restart, _windows_gateway_resume, gateway_mode)
    _verify_fleet_after_update(
        _restart, _pre_update_plan=_pre_update_plan, _windows_gateway_resume=_windows_gateway_resume,
        node_failures=node_failures, update_complete=update_complete,
        expected_sha=None, expected_root=None)


def _cmd_update_impl(args, gateway_mode: bool):
    """Body of ``cmd_update`` — kept separate so the wrapper can always restore stdio even on
    ``sys.exit``. Self-lock deferral deliberately does NOT run here (pre-fetch it stranded users
    on the OLD checkout in an exit-2 loop); it runs right before the dependency sync."""
    # Rollback does not fetch, prompt, inspect dependency options or mutate the checkout.
    from hermes_cli.update_handoff import adopt_handed_off_gateway_resume

    if getattr(args, "rollback", False) and getattr(args, "no_gateway_restart", False):
        raise ValueError("--rollback cannot be combined with --no-gateway-restart: rollback requires a fleet restart")
    # Resolve policy before any recovery action: a cron or gateway invocation
    # may have restart disabled even when it did not spell the CLI flag itself.
    opts = None if getattr(args, "rollback", False) else _resolve_update_options(args, gateway_mode)
    if opts is not None and getattr(opts, "no_gateway_restart", False):
        from hermes_cli.immutable_releases import ReleasePaths
        paths = ReleasePaths.for_home(get_hermes_home())
        if (paths.home / "release-txn.json").exists():
            message = ("Pending immutable release transaction requires a restart-authorized run; "
                       "--no-gateway-restart prohibits recovery. No launchctl action was attempted.")
            print(f"✗ {message}")
            from hermes_cli.update_receipt import begin_update_receipt
            begin_update_receipt()
            _record_update_step("immutable_release_recovery", False, message)
            _finalize_receipt("partial", "Release reload remains pending: %s")
            raise SystemExit(1)
    if not getattr(args, "rollback", False):
        try:
            _finish_pending_release_transaction()
        except RuntimeError as exc:
            from hermes_cli.update_receipt import begin_update_receipt
            begin_update_receipt()
            _record_update_step("immutable_release_recovery", False, str(exc))
            _finalize_receipt("partial", "Release reload remains pending: %s")
            print(f"✗ {exc}")
            raise SystemExit(1) from exc
    _require_immutable_launchd()

    if getattr(args, "rollback", False):
        from hermes_cli.immutable_releases import ReleasePaths, read_pointer, rollback
        from hermes_cli.update_receipt import begin_update_receipt
        begin_update_receipt()
        home = get_hermes_home()
        paths = ReleasePaths.for_home(home)
        # Finish an interrupted rollback without deriving A from pointers already
        # swapped toward A. Otherwise a second --rollback would undo itself.
        pending_path = paths.home / "release-txn.json"
        pending_operation = (json.loads(pending_path.read_text(encoding="utf-8"))["operation"]
                             if pending_path.exists() else None)
        before = read_pointer(paths.current)
        recovered = _finish_pending_release_transaction(home)
        if pending_operation in {"rollback", "first-migration-rollback"} and recovered is not None:
            result = recovered
            before = Path(recovered["previous"]) if recovered.get("previous") else before
        else:
            if before is None:
                raise RuntimeError("cannot roll back without a current release")
            from hermes_cli import gateway, gateway_launchd
            plist = gateway.get_launchd_plist_path() if sys.platform == "darwin" else None
            previous = read_pointer(paths.previous)
            source_layout = previous is not None and previous.parent != paths.releases.resolve()
            plist_body = None
            if plist and plist.exists() and not source_layout and previous is not None:
                plist_body = gateway.generate_launchd_plist(release_target=previous).encode("utf-8")
            callback = (lambda: gateway_launchd._reload_installed_launchd_plist(plist)) if plist and plist.exists() else None
            result = rollback(home, plist_path=plist if plist and plist.exists() else None,
                              plist_body=plist_body, reload_callback=callback)
        if result.get("reload_pending"):
            if _await_release_acknowledgement(home):
                result = _finish_pending_release_transaction(home) or {key: value for key, value in result.items()
                                                        if key != "reload_pending"}
            if result.get("reload_pending"):
                message = ("Rollback reload pending: intended launchd gateway has not acknowledged "
                           "the release within the configured observation window")
                _record_update_step("immutable_rollback", False, message)
                _finalize_receipt("partial", "Release rollback remains pending: %s")
                raise SystemExit(1)
        current_target = result["current"]
        if current_target is None:
            raise RuntimeError("rollback produced no current target")
        if before is not None and str(before) == current_target:
            completed = paths.home / "release-last-txn.json"
            if completed.exists():
                last = json.loads(completed.read_text(encoding="utf-8"))
                if last.get("operation") in {"rollback", "first-migration-rollback"}:
                    message = "Already rolled back, nothing changed"
                    print(message)
                    _record_update_step("immutable_rollback", True, message)
                    _finalize_receipt("success", "Release rollback already complete: %s")
                    return
        source_layout = Path(current_target).parent != paths.releases.resolve()
        from hermes_cli.update_receipt import record_release_transition
        record_release_transition(
            from_sha=before.name, to_sha=str(result.get("source_sha") or Path(current_target).name),
            from_path=str(before), to_path=current_target,
            kind="migration_reversal" if source_layout else "rollback",
        )
        _record_update_step("immutable_rollback", True,
                            f"from={before} to={result['current']} from_sha={before.name} "
                            f"to_sha={result.get('source_sha') or Path(current_target).name}")
        print(f"✓ Rolled back current release to {result['current']}")
        restart = _restart_gateway_fleet_after_update(
            None, gateway_mode, acknowledged_release_root=Path(current_target))
        if gateway_mode:
            _write_gateway_update_exit_code(not restart.incomplete)
        _verify_fleet_after_update(
            restart, _pre_update_plan=None, _windows_gateway_resume=None,
            node_failures=[], update_complete=not restart.incomplete,
            expected_sha=result.get("source_sha") if source_layout else Path(current_target).name,
            expected_root=Path(current_target), rollback=True,
        )
        return

    assert opts is not None
    gw_input_fn, assume_yes = opts.gw_input_fn, opts.assume_yes
    # A child spawned off hermes.exe already outwaited its parent in ``cmd_update``.
    if getattr(args, "post_swap", None):
        # Second half of a run whose pre-pull interpreter stopped at the code swap.
        _run_post_swap_phase(args, gateway_mode)
        return

    print("☤ Updating Hermes Agent...")
    print()

    _pre_update_plan = _begin_update_receipt_and_plan(args)

    # Backup before any git/file mutation; the snapshot id (None if disabled/failed) feeds
    # the post-update cron-jobs safety net.
    # _run_pre_update_backup records its own receipt entry: only it can distinguish an
    # opt-out from a failure, and the reason matters more than the bare outcome.
    pre_update_snapshot_id = _m()._run_pre_update_backup(args)

    # A legacy re-exec child resumes exactly the fleet its parent stopped; re-running discovery
    # here found the parent's just-relaunched gateway and force-killed it (#101600).
    _windows_gateway_resume = adopt_handed_off_gateway_resume() or _m()._pause_windows_gateways_for_update()
    if _windows_gateway_resume:
        import atexit as _atexit
        _atexit.register(_m()._resume_windows_gateways_after_update, _windows_gateway_resume)

    # Any venv python still running (typically the Desktop `hermes serve` backend) keeps .pyd
    # locked and would corrupt the sync; refuse rather than race (the app respawns a killed
    # backend). NOT bypassed by --force (desktop updater, shim guard only); --force-venv is.
    if _m()._is_windows() and not getattr(args, "force_venv", False):
        _clear_windows_venv_holders_or_exit(args, gateway_mode, _windows_gateway_resume)

    # After every fail-closed venv guard, before either path can remove the release tree.
    # Self-lock deferral moved: the venv-holder sweep above excludes this process by design (a CLI `hermes
    # update` IS the venv python), and an updater that has imported a native venv extension cannot rewrite
    # its own mapped .pyd (#83569). That check used to run HERE — before the fetch — but firing pre-fetch
    # meant a deferral stranded the user on the OLD checkout, and any startup path that eagerly loaded
    # cryptography turned every Windows update into an exit-2 loop (#86735/#86780/#86781). It now runs via
    # _abort_dependency_sync_if_self_locked() after the code swap, immediately before the dependency sync —
    # the only phase the lock can actually break — and only when the sync would truly rewrite the loaded
    # distribution.
    desktop_dir = _m().PROJECT_ROOT / "apps" / "desktop"
    had_desktop_app_before_update = _desktop_app_present(desktop_dir)

    use_zip_update, git_cmd, is_fork = _prepare_git_command()

    if use_zip_update:
        try:
            desktop_build_ok = _update_via_zip(
                args, had_desktop_app_before_update=had_desktop_app_before_update,
                _windows_gateway_resume=_windows_gateway_resume)
        finally:
            # No-op once the post-swap child owns the token (``resume_needed`` flipped);
            # still resumes after a pre-swap refusal (dirty tree, --branch).
            _m()._resume_windows_gateways_after_update(_windows_gateway_resume)
        if gateway_mode:
            _write_gateway_update_exit_code(desktop_build_ok)
        return

    try:
        # Scoped fetch: a bare `git fetch origin` pulls thousands of branches and can stall.
        branch = _m()._resolve_update_branch(args)

        # Self-heal abandoned .git/*.lock files (crashed fetch) or the fetch fails "File exists".
        from hermes_cli.gitlock import clear_stale_git_locks, clear_stale_tmp_packs
        cleared = clear_stale_git_locks(_m().PROJECT_ROOT)
        if cleared:
            print("  (removed stale git lock(s): %s)" % ", ".join(cleared))
        swept = clear_stale_tmp_packs(_m().PROJECT_ROOT)
        if swept:
            print("  (removed %d aborted-fetch pack temp file(s))" % len(swept))
        # Shallow installer checkouts collect one `.git/shallow` graft per past depth-1 fetch
        # (#105951); stale grafts break merge-base and push this run into the divergence path.
        from hermes_cli.gitlock import repair_broken_shallow_boundaries, prune_stale_shallow_grafts
        repaired = repair_broken_shallow_boundaries(_m().PROJECT_ROOT)
        if repaired:
            print(f"  (restored {repaired} broken shallow boundary(ies))")
        pruned = prune_stale_shallow_grafts(_m().PROJECT_ROOT)
        if pruned:
            print(f"  (pruned {pruned} stale shallow graft(s) left by past depth-1 checks)")

        # Surface autostashes left by earlier updates (--keep-stash, failed restores).
        # Surface autostash entries left behind by earlier updates (#63717 problem 6) — parked --keep-stash
        # runs and failed restores preserve the stash but nothing ever mentioned it again.
        _m()._warn_orphaned_update_autostashes(git_cmd, _m().PROJECT_ROOT)

        print("→ Fetching updates...")
        fetch_result = _git_run(git_cmd, ["fetch", "origin", branch], network=True)
        if fetch_result.returncode != 0:
            _print_fetch_failure(fetch_result.stderr)
            sys.exit(1)

        if _immutable_release_enabled():
            _apply_fetched_immutable_update(
                git_cmd, branch, opts, args, gateway_mode=gateway_mode,
                had_desktop_app_before_update=had_desktop_app_before_update,
                pre_update_snapshot_id=pre_update_snapshot_id, _pre_update_plan=_pre_update_plan,
                _windows_gateway_resume=_windows_gateway_resume)
            return

        current_branch = _current_branch_name(git_cmd, check=True)
        _plan = _prepare_checkout_for_update(
            git_cmd, branch, current_branch, is_fork=is_fork, assume_yes=assume_yes,
            gateway_mode=gateway_mode, gw_input_fn=gw_input_fn, switch_branch=opts.switch_branch,
            _windows_gateway_resume=_windows_gateway_resume)
        commit_count = _plan.commit_count

        if commit_count == 0:
            _finish_already_up_to_date(
                git_cmd, branch, current_branch, _plan, assume_yes=assume_yes,
                gateway_mode=gateway_mode, gw_input_fn=gw_input_fn,
                pre_update_snapshot_id=pre_update_snapshot_id,
                had_desktop_app_before_update=had_desktop_app_before_update,
                active_lazy_features=opts.active_lazy_features,
                active_tool_dependencies=opts.active_tool_dependencies,
                _windows_gateway_resume=_windows_gateway_resume,
                no_gateway_restart=opts.no_gateway_restart)
            return

        if commit_count > 0:
            print(f"→ Found {commit_count} new commit(s)")
        else:
            # Shallow, exact count unrecoverable — but the tips differ, so there IS an update.
            print("→ Updates available (commit count unknown on this shallow checkout)")

        print("→ Pulling updates...")
        pre_pull_sha = _pull_updates(
            git_cmd, branch, _plan.auto_stash_ref, prompt_for_restore=_plan.prompt_for_restore,
            gw_input_fn=gw_input_fn, discard_local_changes=opts.discard_local_changes,
            keep_stash=opts.keep_stash)
        _apply_pulled_update(
            git_cmd, branch, pre_pull_sha, _plan, opts, gateway_mode=gateway_mode,
            is_fork=is_fork, desktop_dir=desktop_dir,
            had_desktop_app_before_update=had_desktop_app_before_update,
            pre_update_snapshot_id=pre_update_snapshot_id, _pre_update_plan=_pre_update_plan,
            _windows_gateway_resume=_windows_gateway_resume, args=args)
    except _shim_quarantine_error_type() as e:
        # Strict quarantine refused BEFORE any installer ran — defer via marker, exit 2, no ZIP.
        # See #87331.
        _refuse_update_for_contended_shims(e)
    except subprocess.CalledProcessError as e:
        _handle_update_called_process_error(
            e, args, gateway_mode, had_desktop_app_before_update,
            _windows_gateway_resume=_windows_gateway_resume)


# ---- BEGIN PLUGIN-COMPAT (revert-scheduled; see COMPAT_MANIFEST.md) ----
# Names external plugins imported from this module before the Sep 2026 decomposition.
# Internal code MUST NOT use these (scripts/check_compat_pointers.py fails CI if it does).
# The whole block is removed by reverting the commit that added it.
from typing import Optional  # noqa: F401,E402
from datetime import datetime  # noqa: F401,E402
import hashlib  # noqa: F401,E402
import json  # noqa: F401,E402
# ---- END PLUGIN-COMPAT ----
