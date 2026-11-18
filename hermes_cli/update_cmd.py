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
import shutil
import subprocess
import sys
import time as _time
from dataclasses import dataclass
from pathlib import Path
from typing import NoReturn

# Old updaters load _no_prompt_git_kwargs from here after the checkout swap (frozen surface).
from hermes_cli._subprocess_compat import no_prompt_git_kwargs as _no_prompt_git_kwargs
from hermes_cli.config import get_hermes_home
from hermes_cli import update_handoff as _update_handoff
from hermes_cli.update_cmd_common import _best_effort, _record_stop
# Captured BEFORE a checkout swap: parent transport/lifecycle never imports new code.
from hermes_cli.update_completion import run_completion
from hermes_cli.update_channel import adopt_retired_channel
from pm.receipt import accept_worker_receipt as _accept_completion_pm_receipt
from hermes_cli import update_receipt as _completion_receipt, update_cmd_config as _completion_config
from hermes_cli._old_updater import stop_for_relaunch
from hermes_cli._early_recovery import git_operation_in_progress, interrupted_pull_marker, is_object_id
from hermes_cli import update_cmd_commit as _commit
from hermes_cli import update_cmd_posix_pause as _posix_pause
from hermes_cli.update_cmd_pause_gate import _checkout_move, _moves_for
from hermes_cli import update_cmd_check as _check
from hermes_cli.forward_only_guard import (
    leftover_forward_only_state, refuse_if_forward_only_leftovers,
)

# Re-exports: every split-module name stays reachable (and monkeypatchable) as update_cmd.<name>.
from hermes_cli.update_abort_recovery import (
    _abort_recovery_is_complete, _qualified_serve_skips, _recover_gateway_restart_after_abort,
    _serve_unit_recovery_available, _surviving_pre_update_serve_runtimes,
    _warn_stale_serve_runtimes)
from hermes_cli.update_cmd_windows import (
    _HOLDER_VALUE_FLAGS_FALLBACK,
    _cold_start_windows_gateway_after_update, _desktop_owns_gateway_lifecycle,
    _detect_venv_python_processes, _hermes_holder_subcommand, _holder_value_flags,
    _holder_value_flags_cache, _looks_like_desktop_control_plane,
    _pause_windows_gateways_for_update,
    _refresh_bootstrap_cache_scripts, _refresh_windows_gateway_launchers,
    _refuse_gateway_ancestor_tree_kill,
    _restore_windows_gateway_service, _resume_windows_gateways_after_update,
    _resume_windows_gateways_and_merge_outcome, _self_and_non_gateway_ancestor_pids,
    _start_windows_gateway_service,
    _stop_windows_gateway_service, _venv_launcher_ancestors,
    _wait_for_windows_update_gateway_exit, _write_update_planned_stop_marker)
from hermes_cli.update_cmd_fleet import (
    _FLEET_RESTART_PENDING_NAME, _FRESH_RESTART_SUPERVISORS, _GatewayRestartOutcome,
    _apply_pending_fleet_restart_catchup,
    _clear_fleet_restart_pending_marker,
    _current_checkout_sha, _drain_or_signal_gateway_for_update, _fleet_probe_expected_runtimes,
    _fleet_restart_pending_marker_path, _fleet_restart_skip_reason, _for_each_systemd_gateway_unit,
    _gateway_recovery_partition, _gateway_service_matches_profile, _pending_fleet_restart_needed,
    _receipt_looks_unfinished, _receipt_reports_stale_runtime, _resolve_manage_cmd,
    _restart_gateway_fleet_after_update, _restart_launchd_gateway_after_update,
    _restart_macos_launchd_gateways, _restart_phase_failure_is_incomplete,
    _restart_systemd_gateway_units,
    _run_pending_fleet_restart,
    _service_unit_supports_graceful_sigusr1_restart, _surviving_gateway_pids_after_failed_restart,
    _systemctl, _systemctl_reset_and_restart,
    _wait_for_service_active, _warn_gateway_restart_phase_aborted,
    _warn_incomplete_gateway_fleet_restart, _warn_pending_fleet_restart,
    _warn_pending_fleet_restart_on_startup, _write_fleet_restart_pending_marker,
    _write_gateway_update_exit_code)
from hermes_cli.update_cmd_fleet_verify import _verify_fleet_after_update
from hermes_cli.update_cmd_zip import (
    _ZIP_PRESERVED_TOP_LEVEL, _ZIP_STAGING_ARTIFACT_SUFFIXES, _abort_zip_update_if_dirty_tree,
    _atomic_replace_dir, _commit_staged_replacements, _discard_staged,
    _is_zip_preserved_entry_status_line, _is_zip_staging_artifact_status_line, _stage_replacement,
    _update_via_zip, _zip_overlay_block_reason)
from hermes_cli.update_cmd_stash import (
    _AUTOSTASH_NAME_PREFIX, _AUTOSTASH_WARN_AGE_DAYS, _clear_pending_autostash,
    _discard_stashed_changes,
    _git_untracked_paths, _park_stashed_changes, _print_stash_cleanup_guidance,
    _reject_unsafe_stash_restore, _resolve_stash_selector, _restore_stashed_changes,
    _restored_python_paths, _stash_apply_failed_only_on_existing_untracked,
    _stash_local_changes_if_needed, _target_would_move_head, _unrestored_autostash_notice,
    _warn_orphaned_update_autostashes)
from hermes_cli.update_cmd_config import (
    _LAST_SIBLING_SNAPSHOTS, _check_and_apply_config_migration, _migrate_sibling_profile_configs,
    _print_items, _reload_config_modules, _run_config_check_fresh, _run_migrate_config_fresh)
from hermes_cli.update_cmd_validation import (
    _UPDATE_CRITICAL_MODULES, _critical_module_import_failures,
    _validate_critical_modules_import)
from hermes_cli.old_updater_deps import (
    _capture_active_lazy_features, _npm_lockfile_changed, _path_uid,
    _rebuild_desktop_after_update, _refresh_active_lazy_features,
    _refresh_active_memory_provider_dependencies, _update_node_dependencies)
from hermes_cli.update_cmd_git import (
    OFFICIAL_REPO_URL, OFFICIAL_REPO_URLS, SKIP_UPSTREAM_PROMPT_FILE, _ORPHAN_RESCUE_REFS_TO_KEEP,
    _ORPHAN_RESCUE_REF_MAX_AGE_DAYS, _add_upstream_remote, _assess_parked_branch_switch,
    _branch_head_label, _branch_head_suffix, _classify_fetch_failure, _count_commits_between,
    _discard_lockfile_churn, _ensure_non_trampoline_git, _get_origin_url, _git_is_trampoline,
    _has_upstream_remote, _is_fork, _locate_real_git, _mark_skip_upstream_prompt,
    _normalize_managed_eol, _park_detached_head, _portable_git_candidates, _print_fetch_failure,
    _print_parked_branch_kept_notice, _print_parked_branch_skip_warning,
    _prune_orphan_rescue_refs, _push_synced_fork, _should_skip_upstream_prompt, _sync_fork_with_upstream,
    _sync_with_upstream_if_needed, UpstreamTargetBroken)
from hermes_cli.update_cmd_maint import (
    _PRE_UPDATE_SNAPSHOT_KEEP, _PRE_UPDATE_SNAPSHOT_MAX_FILE_SIZE,
    _checkout_version, _ensure_acp_launcher, _ensure_fhs_path_guard, _finish_dashboard_update_cleanup,
    _format_time_ago, _post_update_sqlite_runtime_status, _print_bundled_skills_sync_report,
    _print_curator_first_run_notice, _print_curator_recent_run_notice,
    _print_fts_optimize_available_notice, _print_update_completion, _print_update_summary,
    _prepare_updated_checkout,
    _print_verified_update_completion, _purge_stale_hermes_modules, _read_project_version,
    _reload_process_scan_modules, _reload_updated_runtime_modules,
    _resolve_pre_update_backup_mode, _restore_state_db_from_snapshot,
    _run_post_update_maintenance, _run_pre_update_backup,
    _sweep_bytecode_after_update,
    _update_complete_message, _verify_and_restore_one_state_db,
    _verify_and_restore_state_dbs_post_update)
from datetime import UTC
logger = logging.getLogger(__name__)


def get_default_hermes_root() -> NoReturn:
    # Shim to suppress old updater work until relaunch. No path is safe to invent.
    stop_for_relaunch()


def _ensure_uv_for_termux(pip_cmd: list[str]) -> NoReturn:
    # Shim to stop the old updater doing work until relaunch, not bootstrap uv.
    stop_for_relaunch()


def _ensure_venv_pip(pip_cmd: list, python_exe: str) -> NoReturn:
    # Shim to stop the old updater doing work until relaunch, not bootstrap pip.
    stop_for_relaunch()


def _pip_install_prefix(uv_bin) -> NoReturn:
    # Shim to stop the old updater doing work until relaunch, not form an install.
    stop_for_relaunch()


def _refuse_update_for_contended_shims(exc: BaseException) -> NoReturn:
    # Shim to stop the old updater doing work until relaunch. Write no markers.
    stop_for_relaunch()


def _shim_quarantine_error_type() -> type[Exception]:
    # Shim to stop the old updater doing work until relaunch. Its old except
    # clause needs an exception type, but must not catch real failures.
    class _NeverRaised(Exception):
        pass

    return _NeverRaised


def _m():
    """Lazy ``hermes_cli.main`` reference.

    Lets callers keep patching ``hermes_cli.main.<helper>`` (the historical
    test surface) and have those patches reach this code path, and defers the
    import so ``hermes_cli.main`` -> ``hermes_cli.update_cmd`` stays one-way
    at import time.
    """
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
            value = (path / ".hermes_build_sha").read_text(encoding="utf-8-sig").strip()
        except OSError:
            return None
        return value if re.fullmatch(r"[0-9a-fA-F]{40}", value) else None

    journal = paths.home / "release-layout.json"
    try:
        record = json.loads(journal.read_text(encoding="utf-8-sig"))
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
    # The overlap/forward-only handover was withdrawn. Refuse to activate an immutable
    # release while its launchd label or durable intent can still own traffic.
    try:
        refuse_if_forward_only_leftovers(home)
    except Exception as exc:
        _record_update_step("immutable_release", False, str(exc))
        logger.error("Immutable release activation refused: %s", exc)
        return False
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
        from hermes_cli.admission_canary import observe as observe_admission_canary
        observe_admission_canary(home, candidate)
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
        from hermes_cli.admission_canary import AdmissionCanaryError
        if isinstance(exc, AdmissionCanaryError):
            print(f"✗ {exc}")
        else:
            logger.exception("Immutable release staging or activation failed; inspect current pointer and migration journal")
        return False


class UpdateConfigurationLoadError(Exception):
    """Config could not be read before updater options were resolved."""


class UpdateConfigurationError(ValueError):
    """The authored updates section failed validation."""


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
        raise UpdateConfigurationError("Invalid update configuration: " + "; ".join(issue.message for issue in issues))
    section = config.get("updates", {})
    return section if isinstance(section, dict) else {}


def _map_ssl_cert_file_for_git(git_cmd) -> None:
    """Point git's libcurl at the same CA bundle Python already trusts.

    git ignores ``SSL_CERT_FILE`` (its libcurl reads only ``GIT_SSL_CAINFO`` /
    ``http.sslCAInfo``), so on a network with a TLS-inspecting proxy whose
    corporate root lives only in that variable the channel read succeeds and
    the very next ``git fetch`` dies with "certificate signer not trusted" —
    half the updater trusting a bundle the other half refuses. Written into
    ``os.environ`` once so every git child the updater spawns sees it: the
    network fetches (via ``_no_prompt_git_kwargs``) and the partial-clone
    checkout's lazy promisor fetches (which inherit the process env).

    An explicit ``GIT_SSL_CAINFO`` or a configured ``http.sslCAInfo`` wins:
    the env var outranks the config file in git's precedence, so mapping over
    either would silently override a deliberate choice.
    """
    bundle = os.environ.get("SSL_CERT_FILE")
    if not bundle or os.environ.get("GIT_SSL_CAINFO"):
        return
    configured = _git_run(git_cmd, ["config", "--get", "http.sslCAInfo"])
    if configured.returncode == 0 and configured.stdout.strip():
        return
    os.environ["GIT_SSL_CAINFO"] = bundle


# Startup files of a tree that does not declare its own (pyproject ``[tool.hermes]
# update-startup-files``, read by ``update_cmd_commit.startup_files``): every release before the
# declaration has this layout, so the list stays as it is when files move. The first rows are what
# the launcher and ``hermes_bootstrap`` import before any application code (review G3).
_UPDATE_CRITICAL_FILES = (
    "hermes_bootstrap.py", "hermes_cli/_parser.py", "hermes_cli/_launchers.py",
    "hermes_cli/_early_recovery.py", "hermes_cli/update_lock.py", "hermes_cli/update_custody.py",
    "hermes_cli/update_handoff.py", "hermes_cli/venv_sync.py", "pm/__init__.py", "pm/environments.py",
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

def _record_pre_update_backup_outcome(args, snapshot_id) -> None:
    """Record the pre-update backup as a skip when it was disabled, else as a step.

    ``snapshot_id`` is None both when the backup was deliberately turned off (config
    ``updates.pre_update_backup: off``/``false``, or ``--no-backup``) and when a requested backup
    produced nothing. Recording both as ``ok=false, "disabled or failed"`` made an opt-out
    indistinguishable from a real failure in the receipt, so a disabled safety net read as a
    broken one (#94944 is the shipped-opt-out case). A deliberate opt-out is a SKIP WITH its
    reason; only a requested-but-empty backup is a failed step.
    """
    if snapshot_id:
        _record_update_step("pre_update_backup", True, f"snapshot={snapshot_id}")
        return
    if _resolve_pre_update_backup_mode(args) == "off":
        reason = ("disabled by --no-backup" if getattr(args, "no_backup", False)
                  else "disabled by updates.pre_update_backup (mode: off)")
        _record_update_skip("pre_update_backup", reason)
        return
    _record_update_step("pre_update_backup", False, "no snapshot captured")


def _record_snapshot_stage(args, snapshot_id) -> None:
    skipped = not snapshot_id and _resolve_pre_update_backup_mode(args) == "off"
    _completion_receipt.record_stage("snapshot", "success" if snapshot_id else "skipped" if skipped else "failed")



def _git_run(git_cmd, args, cwd=None, *, check=False, network=False):
    """Run git capturing utf-8 text (default cwd: checkout); ``network=True`` disables the
    terminal prompt so an HTTP 401 fails fast instead of hanging, and bounds the wait.

    Every spawn carries ``windows_hide_flags()``: the updater's git children run under the
    console-less desktop backend, and a bare spawn flashes a console window each (#117781)."""
    from hermes_cli._subprocess_compat import windows_hide_flags
    # ``_no_prompt_git_kwargs()`` already carries the hide flags for network
    # calls, so layer them instead of passing the keyword twice.
    spawn_kwargs = {"timeout": NETWORK_GIT_TIMEOUT_SECONDS, **_no_prompt_git_kwargs()} if network else {}
    spawn_kwargs.setdefault("creationflags", windows_hide_flags())
    from hermes_cli.update_custody import run_git

    # The one custody policy (R2): local mutators keep the update's checkout lock fd, network
    # git runs without it and dies with us, no git forks a detached gc/maintenance child.
    try:
        return run_git(
            git_cmd, args, cwd=_m().PROJECT_ROOT if cwd is None else cwd, capture_output=True,
            text=True, encoding="utf-8", errors="replace", check=check,
            **spawn_kwargs)
    except subprocess.TimeoutExpired as exc:
        # subprocess.run already killed the child; the checkout stays consistent because
        # fetch writes to tmp_pack_* and only renames on success. Report as a failed run
        # so every caller's existing stderr path prints one clear line.
        result = subprocess.CompletedProcess(
            exc.cmd, 124, stdout="",
            stderr=f"git {args[0]} timed out after {NETWORK_GIT_TIMEOUT_SECONDS}s (a stalled remote, or a transfer too large for the limit)")
        if check:
            raise subprocess.CalledProcessError(124, exc.cmd, output="", stderr=result.stderr) from exc
        return result


def _heal_stale_shallow_checkout(repo_root: Path, branch: str) -> None:
    """Unshallow a shallow checkout before the bounded fetch (#123254). Non-fatal: on failure the
    update proceeds with the fetch it always ran."""
    from hermes_cli.gitlock import heal_shallow_history

    try:
        if heal_shallow_history(repo_root, branch, **_no_prompt_git_kwargs()):
            print("  ✓ Fetched the commit history this shallow checkout was missing")
    except (OSError, subprocess.SubprocessError) as exc:
        detail = (getattr(exc, "stderr", None) or str(exc)).strip().splitlines()[-1:] or [type(exc).__name__]
        print(f"  ⚠ Could not fetch the missing commit history ({detail[0]}); a very stale install may time out")


def _read_head_sha(git_cmd, cwd) -> str | None:
    """Read a checkout's current HEAD without applying rescue-specific hooks."""
    try:
        from hermes_cli.update_custody import run_git

        result = run_git(
            git_cmd, ["rev-parse", "HEAD"],
            cwd=cwd,
            capture_output=True,
            text=True, encoding="utf-8", errors="replace",
            check=True,
        )
        return result.stdout.strip() or None
    except (subprocess.CalledProcessError, OSError):
        return None


def _capture_head_sha(git_cmd, cwd) -> str | None:
    """Return the current HEAD SHA for the pre-update rescue path, or None if unresolved."""
    return _read_head_sha(git_cmd, cwd)


def _live_release_sha(git_cmd) -> str | None:
    """Return the revision currently serving Hermes before this update moves anything."""
    with suppress(Exception):
        from hermes_cli.immutable_releases import resolved_release
        release = resolved_release(get_hermes_home())
        if release is not None and re.fullmatch(r"[0-9a-fA-F]{40}", release.name):
            return release.name
    head = _read_head_sha(git_cmd, _m().PROJECT_ROOT)
    if head:
        return head
    return None


def _guard_against_main_drop(
    args, git_cmd, target_ref: str, *, target_is_head: bool, skip: bool = False, resume_gateways=None,
) -> None:
    """Refuse a target that would remove live commits already contained by origin/main."""
    def resume() -> None:
        if resume_gateways is not None:
            with suppress(Exception):
                resume_gateways()

    if skip or target_is_head:
        return  # Release channels have their own forward/rollback admission contract.
    if getattr(args, "allow_drop_main", False):
        with suppress(Exception):
            from hermes_cli.update_receipt import record_fact
            record_fact("allow_drop_main", True)
        return

    live_sha = _live_release_sha(git_cmd)
    if not live_sha:
        message = "✗ Refusing update: could not determine the currently live release safely. No update was applied."
        _record_update_step("main_drop_guard", False, message)
        _record_stop("main_drop_check")
        with suppress(Exception):
            from hermes_cli.update_receipt import finalize_update_receipt
            finalize_update_receipt("refused", stop_reason="main_drop_check")
        resume()
        print(message)
        raise SystemExit(2)

    from hermes_cli.update_main_guard import MainDropCheckError, MainDropCheckSkipped, find_main_drop
    try:
        finding = find_main_drop(
            lambda command, **kwargs: _git_run(git_cmd, command, cwd=_m().PROJECT_ROOT, **kwargs),
            target_ref,
            live_sha,
        )
    except MainDropCheckSkipped as exc:
        _record_update_step("main_drop_check_skipped", True, str(exc))
        return
    except MainDropCheckError as exc:
        message = f"✗ Refusing update: could not verify origin/main safely ({exc}). No update was applied."
        _record_update_step("main_drop_guard", False, message)
        _record_stop("main_drop_check")
        with suppress(Exception):
            from hermes_cli.update_receipt import finalize_update_receipt
            finalize_update_receipt("refused", stop_reason="main_drop_check")
        resume()
        print(message)
        raise SystemExit(2)
    if finding is None:
        return

    message = finding.message
    _record_update_step("main_drop_guard", False, message)
    _record_stop("main_drop")
    with suppress(Exception):
        from hermes_cli.update_receipt import record_fact, finalize_update_receipt
        record_fact("main_drop", {"count": finding.count, "commits": [sha for sha, _ in finding.commits]})
        finalize_update_receipt("refused", stop_reason="main_drop")
    resume()
    print(message)
    raise SystemExit(2)


def _validate_python_files_syntax(
    root, relpaths
) -> tuple[bool, str | None, str | None]:
    """Compile *relpaths* under *root* without writing bytecode into the tree."""
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


# Modules imported on every agent startup. Unlike _UPDATE_CRITICAL_FILES (which
# is only parsed), these are actually *imported* so that cross-module breakage
# is caught — a file can be syntactically perfect and still fail to import
# because a name it pulls from a sibling module no longer exists.


def _gateway_prompt(prompt_text: str, default: str = "", timeout: float = 300.0) -> str:
    """File-based IPC prompt for gateway mode.

    Writes a prompt marker file so the gateway can forward the question to the
    user, then polls for a response file.  Falls back to *default* on timeout.

    Used by ``hermes update --gateway`` so interactive prompts (stash restore,
    config migration) are forwarded to the messenger instead of being silently
    skipped.
    """
    import json as _json
    import uuid as _uuid
    from hermes_constants import get_hermes_home

    if _posix_pause.gateways_paused():  # the gateway that would relay it is stopped for the update
        print(f"  → {prompt_text} → {default or '(empty)'} (gateways are paused for the update)")
        return default
    home = get_hermes_home()
    prompt_path = home / ".update_prompt.json"
    response_path = home / ".update_response"

    # Clean any stale response file
    response_path.unlink(missing_ok=True)

    payload = {
        "prompt": prompt_text,
        "default": default,
        "id": str(_uuid.uuid4()),
    }
    tmp = prompt_path.with_suffix(".tmp")
    tmp.write_text(_json.dumps(payload), encoding="utf-8")
    tmp.replace(prompt_path)

    # Poll for response
    deadline = _time.monotonic() + timeout
    while _time.monotonic() < deadline:
        if response_path.exists():
            try:
                answer = response_path.read_text(encoding="utf-8-sig").strip()
                response_path.unlink(missing_ok=True)
                prompt_path.unlink(missing_ok=True)
                return answer if answer else default
            except (OSError, ValueError):
                pass
        _time.sleep(0.5)

    # Timeout — clean up and use default
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
    # Windows argv may use backslashes; basename() on POSIX would otherwise
    # keep the whole path. Normalize separators before taking the name.
    name = os.path.basename(parts[0].replace("\\", "/")).lower()
    return name in {"git", "git.exe"}


def _called_process_error_is_python_dep_install(
    exc: subprocess.CalledProcessError,
) -> bool:
    """True when the failed subprocess was a uv/pip (or ensurepip) install."""
    parts = [part.lower() for part in _called_process_error_cmd_parts(exc)]
    if not parts:
        return False
    exe = os.path.basename(parts[0].replace("\\", "/"))
    if "ensurepip" in parts:
        return True
    if "install" in parts and (
        "pip" in parts or exe in {"pip", "pip.exe", "pip3", "pip3.exe", "uv", "uv.exe"}
    ):
        return True
    return False


def _format_update_failure_stage(exc: subprocess.CalledProcessError) -> str:
    """Name the update stage that actually failed.

    The git pull and the Python-dependency install share one ``try`` in
    ``_cmd_update_impl``. Calling every ``CalledProcessError`` a git failure
    (the historical Windows message) sent users hunting in the wrong place
    and, worse, keyed the ZIP overlay on exception *type* rather than on git
    actually having failed (#87304, #85840).
    """
    if _called_process_error_is_python_dep_install(exc):
        return "Python dependency install failed"
    if _called_process_error_is_git(exc):
        return "Git update failed"
    return "Update step failed"


def _should_zip_fallback_on_update_error(exc: BaseException) -> bool:
    """ZIP fallback is for Windows git file-I/O breakage, not later stages.

    A dependency-install failure (locked ``hermes.exe`` / ``uv pip install``
    exit 2) is not a git failure. The pull has already succeeded by then, so
    re-downloading the source ZIP cannot fix the install and would replace
    every top-level entry except ``venv`` / ``node_modules`` / ``.git`` /
    ``.env`` — permanently deleting uncommitted edits and untracked files.
    """
    return (
        isinstance(exc, subprocess.CalledProcessError)
        and _m()._is_windows()
        and _called_process_error_is_git(exc)
        # Only from an untouched checkout: the ZIP overlay replaces every top-level entry, so
        # after git already stashed, switched or half-moved the tree it would bury that state.
        and _commit.run_checkout_untouched(_m().PROJECT_ROOT)
    )


def _print_called_process_error_tail(
    exc: subprocess.CalledProcessError, *, limit: int = 12
) -> None:
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


# Single source of truth for the top-level entries the ZIP swap preserves —
# consumed by both the dirty-tree filter below and _update_via_zip's swap loop.


# Release tags have the form v1.2.3. A tag can have a pre-release suffix.
# The stable channel ignores tags with a suffix. Stable means final releases only.
# The major component is capped at three digits. The historical CalVer tags
# (for example v2026.7.20) use a four-digit year, and a numeric sort would
# rank them above every SemVer release. This matches _SEMVER_TAG_RE in
# scripts/write_install_stamp.py.


def _write_update_incomplete_marker() -> None:
    # Historical updater hook. PM's successful facts determine completion.
    stop_for_relaunch()


def _write_lazy_refresh_incomplete_marker() -> None:
    # Historical updater hook. There is no separate lazy-refresh transaction.
    stop_for_relaunch()



def _filter_non_gateway_concurrent_instances(
    matches: list[tuple[int, str]],
) -> list[tuple[int, str]]:
    # Historical updater hook; PM never replaces a running venv's executables.
    stop_for_relaunch()


def _log_only_write(text: str) -> None:
    """Write ``text`` to ``~/.hermes/logs/update.log`` only, never the terminal.

    During ``hermes update`` ``sys.stdout`` is an ``_UpdateOutputStream`` that
    mirrors to both the terminal and ``update.log``. Loud, low-signal
    subprocess output (pnpm installs, the Electron/vite build, the cua-driver
    installer's "Next steps" wall) should be captured and tucked into the log
    so failures stay debuggable, without flooding the user's terminal. This
    reaches past the mirroring stream straight to the underlying log handle.
    """
    if not text:
        return
    stream = _m().sys.stdout
    log_file = getattr(stream, "_log", None)
    with suppress(Exception):
        if log_file is None:
            from hermes_constants import get_default_hermes_root

            log_path = get_default_hermes_root() / "logs" / "update.log"  # the root home's tee
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
    # detached-spawn: ok — awaited update step; a cancel tree-kills its group (kill_process_tree)
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


def _source_update_channel(args=None, *, channel=None, branch_explicit=False) -> str:
    """Explicit branches win; otherwise transient channel, then this install's record."""
    if branch_explicit or getattr(args, "branch", None):
        return "main"
    transient = channel if channel is not None else getattr(args, "channel", None)
    if transient is not None:
        from hermes_cli.release_channels import validate_name
        return validate_name(transient)
    from hermes_cli.update_channel import resolve_update_channel

    from hermes_cli.config import get_config_path, require_readable_config_before_write

    config = require_readable_config_before_write(get_config_path())
    return resolve_update_channel(config, _m().PROJECT_ROOT)


def _cmd_update_check(branch: str = "main", *, branch_explicit: bool = False, channel=None):
    """Implement ``hermes update --check``: fetch and report without installing.

    ``branch`` selects which branch the check compares against. Default is
    "main"; callers can pass another branch to ask "are there new commits
    on origin/<branch>?" without performing the update.

    ``branch_explicit`` is True iff the caller passed --branch on the CLI.
    Installs that can't honor non-default branches (e.g. Docker) surface a
    one-line notice instead of silently dropping the flag.
    """
    # Shared admission gate (#91277 Phase 3): same marker-first decision as
    # the apply path, so --check can never report git state for an install
    # whose real update mechanism is an image pull.
    from hermes_cli.update_contract import (
        evaluate_update_admission,
        record_refusal_receipt,
    )

    refusal = evaluate_update_admission(_m().PROJECT_ROOT)
    if refusal is not None:
        print(refusal.message)
        record_refusal_receipt(refusal)
        sys.exit(2)

    root = _m().PROJECT_ROOT
    if not (root / ".git").exists():
        print("✗ Not a git repository — cannot check for updates.")
        sys.exit(1)

    git_cmd = _base_git_cmd()
    # The check fetches over HTTPS too; give git the same CA bundle Python
    # uses, or --check fails where the apply path succeeds (see
    # _map_ssl_cert_file_for_git).
    _map_ssl_cert_file_for_git(git_cmd)
    _check.clear_git_debris(root)

    selected_channel = _source_update_channel(channel=channel, branch_explicit=branch_explicit)
    if not branch_explicit:
        branch = _check.channel_compare_branch(selected_channel, git_cmd, root)
        if branch is None:
            return

    # Installer checkouts are shallow (`git clone --depth 1`). A plain fetch would unshallow
    # the repo (the exact cost the shallow clone avoided) and rev-list would then report a
    # huge bogus "behind" count, so fetch with --depth 1 and report presence-only.
    is_shallow = _check.is_shallow_repository(git_cmd, root)
    fetch_result, compare_branch = _check.fetch_compare_branch(
        git_cmd, root, branch, ["--depth", "1"] if is_shallow else [],
    )
    if fetch_result.returncode != 0:
        _print_fetch_failure(fetch_result.stderr, git_cmd=git_cmd, root=root)
        sys.exit(1)
    if is_shallow:
        _check.repair_shallow_grafts(root)

    if not _check.compare_ref_exists(git_cmd, root, compare_branch):
        print(f"✗ Branch '{branch}' not found on {compare_branch.split('/', 1)[0]}.")
        sys.exit(1)
    if is_shallow:
        _check.report_shallow_verdict(git_cmd, root, compare_branch)
    else:
        _check.report_rev_list_verdict(git_cmd, root, compare_branch)


def _base_git_cmd() -> list[str]:
    """``git`` argv; Windows adds ``-c windows.appendAtomically=false`` (git can fail "unable to
    write loose object file: Invalid argument" on non-atomic appends)."""
    if sys.platform == "win32":
        return ["git", "-c", "windows.appendAtomically=false"]
    return ["git"]


def _is_shallow_checkout(git_cmd) -> bool:
    return _git_run(git_cmd, ["rev-parse", "--is-shallow-repository"]).stdout.strip() == "true"


def _tip_shas(git_cmd, target_ref: str, base: str = "HEAD") -> tuple[str, str]:
    """``(<base> sha, <target_ref> sha)`` as printed by rev-parse ("" when unresolvable)."""
    return tuple(_git_run(git_cmd, ["rev-parse", ref]).stdout.strip() for ref in (base, target_ref))


def _source_completion_request(opts, plan, snapshot_id, windows_resume, desktop, gateway_mode) -> dict:
    """Freeze data before mutation; no pre-swap module objects cross the seam."""
    from copy import deepcopy
    current = _completion_receipt._current.get()
    if current is None:
        _completion_receipt.begin_update_receipt()
        current = _completion_receipt._current.get()
    return {
        "schema": 1, "source": str(_m().PROJECT_ROOT.resolve()),
        "home": str(get_hermes_home()), "branch": "main", "desktop": desktop,
        "assume_yes": opts.assume_yes, "gateway_mode": gateway_mode,
        "no_gateway_restart": getattr(opts, "no_gateway_restart", False),
        "pre_update_version": opts.pre_update_version, "snapshot_id": snapshot_id,
        "sibling_snapshots": deepcopy(_completion_config._LAST_SIBLING_SNAPSHOTS),
        "plan": plan.to_dict() if plan is not None else None,
        "receipt": deepcopy(current.data), "windows_resume": windows_resume,
    }


def _settle_windows_resume(request: dict) -> None:
    """This run's post-commit resume attempt is over (the completion child's, or the A6 branch's).

    A failure is already the ``windows_resume`` follow-up, so neither the command's ``finally``
    nor the atexit net may replay it: a replay waits again and its error would fail a committed
    update (C3).
    """
    import atexit

    from hermes_cli import update_cmd_windows

    atexit.unregister(_m()._resume_windows_gateways_after_update)
    atexit.unregister(update_cmd_windows._resume_windows_gateways_after_update)
    request["windows_resume_settled"] = True


def _resume_paused_gateways_at_exit(token: dict | None, request: dict | None) -> None:
    """The command's last resume of gateways it paused; a failure is reported, never raised.

    Raising here would replace the run's own exit (a committed update's 0, or the original
    failure) with the restart's error. Skipped when the post-commit attempt already ran.
    """
    if not token or not token.get("resume_needed") or (request or {}).get("windows_resume_settled"):
        return
    try:
        _m()._resume_windows_gateways_after_update(token)
    except Exception as exc:  # health: allow BLE001 -- a restart failure is owed, not the update's status
        from hermes_cli.update_receipt import owe_followup

        owe_followup(request["receipt"]["update_id"] if request else None, "windows_resume",
                     f"Windows gateway recovery failed: {exc}")


def _complete_source_update(request: dict | None) -> None:
    # Never "Update complete!" while this run's local patches sit unrestored in the stash (#122557).
    unrestored = _unrestored_autostash_notice()
    if request is None:
        if unrestored:
            print(unrestored)
        stop_for_relaunch(incomplete=True)
    if unrestored:
        request["completion_message"] = unrestored
    from copy import deepcopy
    _completion_receipt.record_stage(
        "apply", "skipped" if request.get("completion_message") else "success", mode=request.get("apply_mode", "git"))
    from hermes_cli.update_stage import publish_stage
    publish_stage("Updating dependencies")  # the completion below starts with the dependency sync
    current = _completion_receipt._current.get()
    if current is not None:
        request["receipt"] = deepcopy(current.data)
    # A head capture that came back empty must not arm an SHA-less record: it names no code the
    # fleet can be proven current on, so the warning could never clear (#125952).
    _write_fleet_restart_pending_marker(expected_sha=request.get("expected_sha") or _current_checkout_sha() or "")
    # Pre-swap module (imported with run_completion above): never the replacement tree's.
    from hermes_cli.update_completion import settle_lost_completion
    lost = False
    try:
        result = run_completion(request)
    except KeyboardInterrupt as interrupt:
        # Ctrl-C after the commit point: the tree is new, so the run is interrupted, never "failed".
        _completion_receipt.finalize_interrupted_update_receipt("KeyboardInterrupt: interrupted after the code was updated")
        raise SystemExit(130) from interrupt
    except Exception as exc:  # health: allow BLE001 -- after the commit point a lost completion is owed, never a failed update (C3)
        result, lost = settle_lost_completion(request, f"{type(exc).__name__}: {exc}"), True
    if not lost and result.get("receipt") is None and result["exit_code"] == 130:
        _completion_receipt.finalize_interrupted_update_receipt("the completion was interrupted after the code was updated")
    elif not lost and result.get("receipt") is None and result["exit_code"]:
        # The child crashed (OOM, SIGKILL) or never answered (run_completion maps every answer
        # without a correlated terminal receipt to non-zero): the same owed tail (review P2).
        result, lost = settle_lost_completion(
            request, result.get("error") or f"the completion process exited {result['exit_code']}"), True
    _accept_completion_pm_receipt(result.get("pm_receipt"), request["receipt"]["update_id"])
    if result.get("receipt") is not None:
        # The child closed the run: drop the stale pre-child context before anything below can
        # write it (a later follow-up amends the terminal archive), and let the command boundary
        # answer the gateway status from the receipt (C3, review regression 3).
        _completion_receipt.adopt_terminal_receipt(result["receipt"])
        current = _completion_receipt._current.get()
        if current is not None:
            _completion_receipt._current.reset(current.current_token)
    token = request["windows_resume"]
    if token is not None and result.get("windows_resume") is not None:
        resumed = dict(result["windows_resume"])
        token.clear()
        token.update(resumed)
        _settle_windows_resume(request)
    elif token and token.get("resume_needed") and not result["exit_code"]:
        # Dependencies owed (A6) or a lost completion: no child resumed paused gateways; this process can.
        try:
            _m()._resume_windows_gateways_after_update(token)
        except Exception as exc:  # health: allow BLE001 -- the code is committed (C3)
            from hermes_cli.update_receipt import owe_followup

            owe_followup(request["receipt"]["update_id"], "windows_resume", f"Windows gateway recovery failed: {exc}")
        _settle_windows_resume(request)
    if result["exit_code"]:
        raise SystemExit(result["exit_code"])
    if lost:
        return  # a retired channel is adopted after a verified completion only; the next update adopts it
    try:
        if adopt_retired_channel(request):
            print(f"→ Source subscription moved to {request['channel_retirement']['destination']}")
    except Exception as exc:  # health: allow BLE001 -- the code is committed (C3); the next update re-adopts
        from hermes_cli.update_receipt import owe_followup

        owe_followup(request["receipt"]["update_id"], "channel_adoption", str(exc) or type(exc).__name__,
                     retry="the next `hermes update` adopts it again")


#: ``args`` of the result ``_switch_branch_at_commit_point`` returns when the commit point refused.
_ARM_REFUSED = ["arm_commit_point"]
#: ... and when the branch ref moved between its resolution and the checkout (review O2).
_REF_MOVED = ["ref_moved"]


@dataclass
class _CheckoutPlan:
    """What the pre-pull checkout phase decided (see ``_prepare_checkout_for_update``)."""

    auto_stash_ref: str | None
    commit_count: int
    in_place_update: bool
    parked_branch_switched: bool
    prompt_for_restore: bool
    switch_block_reason: str | None
    upstream_checked: bool
    pre_sync_sha: str | None = None
    rollback_branch: str | None = None
    # The switch changed the running code with no new commits to count (commit_count == -1).
    switched_without_new_commits: bool = False


@dataclass
class _UpdateOptions:
    """Resolved ``hermes update`` inputs (flags, config, pre-update snapshots)."""

    pre_update_version: object
    gw_input_fn: object
    assume_yes: bool
    keep_stash: bool
    switch_branch: bool
    discard_local_changes: bool
    no_gateway_restart: bool = False


def _resolve_update_options(args, gateway_mode: bool) -> _UpdateOptions:
    """Snapshot pre-update state and resolve the flags/config ``_cmd_update_impl`` runs on."""

    # Captured before any pull so the completion line can report the transition (prime-agent#630 port).
    pre_update_version = _checkout_version()
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
        pre_update_version=pre_update_version,
        gw_input_fn=gw_input_fn, assume_yes=assume_yes, keep_stash=keep_stash,
        switch_branch=switch_branch, discard_local_changes=discard_local_changes,
        no_gateway_restart=no_gateway_restart)


def _begin_update_receipt_and_plan(args):
    """Open the receipt and snapshot the fleet before changing the checkout."""
    # Structured receipt: record what this run discovers/does/skips so silent failures are diagnosable.
    with _best_effort('Update receipt unavailable: %s'):
        # See #74973, #81193, #85753, #88848, #91277.
        from hermes_cli.update_receipt import begin_update_receipt
        begin_update_receipt()
        _record_update_initiator()

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
    _completion_receipt.record_stage("plan", "failed" if _pre_update_plan is None else "success")
    return _pre_update_plan


def _record_update_initiator() -> None:
    """We hold the update lock, so a claim naming another pid is our orchestrator's: the Desktop
    hand-off (posix shim, windows script, Tauri updater) — the metric's ``kind``. Read the marker
    raw: a liveness probe or stale-marker cleanup is lock policy, not a metrics side effect."""
    with _best_effort('Update initiator unavailable: %s'):
        from hermes_cli.update_lock import update_marker_path
        first = update_marker_path().read_text(encoding="utf-8-sig").partition("\n")[0].strip()
        if first.isdigit() and int(first) != os.getpid():
            _completion_receipt.record_fact("initiator", "desktop")


def _prepare_git_command(*, checkout_move=None) -> tuple[bool, list, bool]:
    """Return ``(use_zip_update, git_cmd, is_fork)``; ``sys.exit(1)`` when not a git repo
    on a non-Windows host (Windows falls back to ZIP: broken git file I/O, AV, NTFS filters).
    *checkout_move* binds the churn cleanups' file writes to the paused gateways' tree gate."""
    git_dir = _m().PROJECT_ROOT / ".git"
    use_zip_update = not git_dir.exists()
    if use_zip_update and sys.platform != "win32":
        print("✗ Not a git repository. Please reinstall:")
        print("  curl -fsSL https://hermes-agent.nousresearch.com/install.sh | bash")
        _record_stop("not_git_checkout")
        sys.exit(1)

    from hermes_cli._subprocess_compat import expose_pm_git

    expose_pm_git(_m().PROJECT_ROOT)
    git_cmd = _base_git_cmd()
    if sys.platform == "win32" and git_dir.exists():
        _git_run(git_cmd, ["config", "windows.appendAtomically", "false"])
    # One CA story for the whole update: git's libcurl must trust what the
    # channel read already trusts, or the fetch dies mid-update behind a
    # TLS-inspecting proxy (see _map_ssl_cert_file_for_git).
    _map_ssl_cert_file_for_git(git_cmd)
    # A broken Git-for-Windows trampoline refuses every call with a "BUG (fork bomb)" guard;
    # swap in a real binary up front so git survives instead of degrading to ZIP.
    # See #87876.
    git_cmd = _ensure_non_trampoline_git(git_cmd)

    # Before stash/branch logic: npm rewrites package-lock.json non-deterministically and
    # line-ending churn is machine-made dirt; both would otherwise force an autostash every update.
    from hermes_cli.update_cmd_stash import _track_preparation_moves
    checkout_move = _track_preparation_moves(checkout_move)
    _discard_lockfile_churn(git_cmd, _m().PROJECT_ROOT, checkout_move=checkout_move)
    _normalize_managed_eol(git_cmd, _m().PROJECT_ROOT, checkout_move=checkout_move)

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
        _commit.disarm_commit_obligations()  # nothing moved: no tail is owed (refused if HEAD did)
        _m()._resume_windows_gateways_after_update(_windows_gateway_resume)
        _record_stop("detached_head")
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
        _record_stop("unexpected_branch")
        sys.exit(1)
    return post_pull_sha


def _current_branch_name(git_cmd, *, check: bool = False) -> str:
    """``rev-parse --abbrev-ref HEAD`` (literal "HEAD" when detached)."""
    return _git_run(git_cmd, ["rev-parse", "--abbrev-ref", "HEAD"], check=check).stdout.strip()


def _handle_update_called_process_error(
    e, args, gateway_mode: bool, had_desktop_app_before_update: bool,
    *, target_sha: str | None = None, target_repository: str | None = None, completion_request=None) -> None:
    """Git/installer failure: ZIP-fallback when safe, else report and ``sys.exit(1)``."""
    if unrestored := _unrestored_autostash_notice():
        print(unrestored)  # a stash taken this run that no settle step reported (#122557)
    stage = _format_update_failure_stage(e)
    if _should_zip_fallback_on_update_error(e):
        print(f"⚠ {stage}: {e}")
        print("→ Falling back to ZIP download...")
        print()
        _update_via_zip(
            args, had_desktop_app_before_update=had_desktop_app_before_update,
            target_sha=target_sha, completion_request=completion_request,
            **({"target_repository": target_repository} if target_repository else {}))

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
        if stop := _completion_receipt.git_error_stop_class(e):
            _record_stop(stop)
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
    record = json.loads(journal_path.read_text(encoding="utf-8-sig")) if journal_path.exists() else {}
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
    git_cmd, branch: str, current_branch: str, _plan, *, gw_input_fn, completion_request: dict,
    _windows_gateway_resume=None, held_message: str | None = None) -> None:
    """"Already up to date" path: restore stash, repair the checkout, catch up the fleet.
    ``held_message`` replaces "Already up to date!" when the channel's release was held, not reached.
    ``sys.exit(1)`` when the repair is incomplete (after gateway exit code + partial receipt)."""
    # A parked branch the update switched off stays on the target: re-parking on the stale branch
    # recreates the incident. No other checkout left current_branch (in-place and release updates
    # never switch), so there is nothing to switch back to.
    if _plan.auto_stash_ref is not None:
        _m()._restore_stashed_changes(
            git_cmd, _m().PROJECT_ROOT, _plan.auto_stash_ref, prompt_user=_plan.prompt_for_restore,
            input_fn=gw_input_fn, checkout_move=_moves_for(_windows_gateway_resume))
    if _plan.parked_branch_switched:
        if _plan.switch_block_reason.startswith("unmerged:"):
            _count = _plan.switch_block_reason.split(":", 1)[1]
            print(
                f"  ✓ Checkout was parked on '{current_branch}' — switched back to {branch}; "
                f"{_count} unmerged commit(s) kept on '{current_branch}'.")
        else:
            print(f"  ✓ Checkout was parked on '{current_branch}' (fully merged) — switched back to {branch}.")

    if completion_request is not None:
        # Same code, same host obligation: an SHA-less arm would REPLACE the standing record
        # (and its restarted proof), so a sibling profile's no-op update re-kills the multiplexer.
        completion_request["expected_sha"] = _capture_head_sha(git_cmd, _m().PROJECT_ROOT) or ""
        completion_request["completion_message"] = held_message or (
            "✓ Already up to date!" if _plan.upstream_checked
            else "✓ Up to date with your fork (official repo not checked).")
    _complete_source_update(completion_request)


def _apply_fetched_immutable_update(git_cmd, branch, opts, args, *, gateway_mode,
                                    had_desktop_app_before_update, pre_update_snapshot_id,
                                    _pre_update_plan, _windows_gateway_resume):
    """Fetch-only release path. Source HEAD, files and venv are never updated."""
    from hermes_cli.immutable_releases import (
        ReleasePaths, read_pointer, release_sha, stage_release, _release_is_ready,
    )
    _record_update_step("immutable_source_untouched", True, "fetch-only release apply leaves source files and venv untouched")
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
    from hermes_cli.update_cmd_candidate import select_update_target
    fetched, selected_branch = select_update_target(source, home, current, fetched, branch, args)
    # Capture migration metadata and the source plist transactionally at activation, not at
    # staging/handoff. Promote a deferred candidate even when the remote has not advanced.
    if current is not None and current.name == fetched:
        _catch_up_immutable_release(defer=opts.no_gateway_restart, sha=fetched, source=source)
        _apply_pending_fleet_restart_catchup(defer=opts.no_gateway_restart)
        return
    # Failed-update notices may say "Nothing changed" only from this record: the active release
    # before the build, and that activation had not started. The terminal receipt adds "after".
    from hermes_cli.immutable_releases import release_identity
    from hermes_cli.update_receipt import record_fact
    immutable_proof = {"before": release_identity(home), "candidate": str(paths.release(fetched)),
                       "source": str(source), "activation_started": False}
    record_fact("immutable_update", immutable_proof)
    record_fact("staging_runtime_mutation_started", False)
    # stage_release archives the selected target (FETCH_HEAD or a local carry-forward merge),
    # builds web and its own locked environment in a build-aside directory, then publishes it.
    candidate, _ = stage_release(source, home, sha=fetched, branch=selected_branch)
    if candidate != paths.release(fetched) or not _release_is_ready(candidate, fetched):
        raise RuntimeError("staged release does not match fetched commit")
    # Activation can change runtime state from the pending marker onward. Staging's PM sync
    # separately records staging_runtime_mutation_started before it can edit shared profiles.
    record_fact("immutable_update", {**immutable_proof, "activation_started": True})
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
    git_cmd, branch, movement_baseline, _plan, *, _windows_gateway_resume, completion_request: dict) -> None:
    """Post-pull phase: verify HEAD, sync Python/Node/web/Desktop, maintenance, fleet restart.

    ``movement_baseline`` is what ``_pull_updates`` returned: the SHA HEAD had to move off."""
    post_pull_sha = _verify_head_after_pull(
        git_cmd, branch, movement_baseline, in_place_update=_plan.in_place_update,
        _windows_gateway_resume=_windows_gateway_resume)

    if completion_request is not None:
        observed = _capture_head_sha(git_cmd, _m().PROJECT_ROOT) or post_pull_sha
        pinned = completion_request.get("expected_sha")
        if pinned is not None and observed != pinned:
            print("✗ Checkout no longer matches the selected channel commit. No completion was applied.")
            _m()._resume_windows_gateways_after_update(_windows_gateway_resume)
            _record_stop("head_moved")
            sys.exit(1)
        completion_request["expected_sha"] = pinned or observed
    _complete_source_update(completion_request)


# ``store_true`` update flags the post-swap child must see exactly as the user passed them.
_POST_SWAP_FORWARDED_FLAGS = (
    ("gateway", "--gateway"), ("no_backup", "--no-backup"), ("backup", "--backup"),
    ("yes", "--yes"), ("keep_stash", "--keep-stash"), ("switch_branch", "--switch-branch"),
    ("force", "--force"), ("force_venv", "--force-venv"),
    ("no_gateway_restart", "--no-gateway-restart"),
)


def _post_swap_argv_tail(args) -> list[str]:
    tail = [flag for attr, flag in _POST_SWAP_FORWARDED_FLAGS if getattr(args, attr, False)]
    # Selection-only flags stay behind: the child runs the target's parser, which may predate them.
    branch = getattr(args, "branch", None)
    return tail + (["--branch", str(branch)] if branch else [])


def _post_swap_payload(
    *, swap: str, branch: str, opts, gateway_mode: bool, had_desktop_app_before_update: bool,
    pre_pull_sha=None, is_fork: bool = False, pre_update_snapshot_id=None, _pre_update_plan=None,
    _windows_gateway_resume=None, release=None, source=None) -> dict:
    """Everything the post-swap tail needs that only the pre-swap process could observe: the
    open receipt (detached here — the child resumes it), the pre-update fleet plan, the
    pre-update version and active features, the Windows pause token. Flags cross as argv."""
    from hermes_cli.immutable_update_handoff import detach_update_receipt

    return {
        "swap": swap, "branch": branch, "pre_pull_sha": pre_pull_sha, "is_fork": bool(is_fork),
        "release": str(release) if release else None, "candidate_sha": Path(release).name if release else None,
        "source": str(source) if source else None,
        "source_python": sys.executable if swap == "immutable" else None,
        "gateway_mode": bool(gateway_mode),
        "had_desktop_app_before_update": bool(had_desktop_app_before_update),
        "pre_update_snapshot_id": pre_update_snapshot_id,
        "pre_update_version": opts.pre_update_version,
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

    The parent detaches from the receipt and its Windows resume hook. A child that claims the
    hand-off owns both and the parent only relays its exit code (``hermes_cli/update_handoff.py``).
    """
    from hermes_cli.update_completion import _resume_receipt as resume_update_receipt

    payload = _post_swap_payload(**payload_kwargs)
    from hermes_cli.immutable_update_handoff import continue_update_in_fresh_interpreter, reclaim_handoff
    code = continue_update_in_fresh_interpreter(
        payload, argv_tail=_post_swap_argv_tail(args))
    # Exit codes cannot tell a child that resumed the receipt and then failed from one that
    # crashed while starting (an ImportError in the staged interpreter); only the claim can.
    claimed = not reclaim_handoff() and code is not None
    token = payload_kwargs.get("_windows_gateway_resume")
    if token and claimed:
        # The child got its own copy (serialized before this flip) and owns the resume; every
        # parent-side hook (atexit, the ZIP path's ``finally``) reads this flag and stays out
        # of the way. When no child claimed it, the parent still resumes what it paused.
        token["resume_needed"] = False
    if not claimed:
        # No child took over: take the receipt back so this failure is recorded, and leave the
        # install breadcrumb so the next launch finishes the dependency sync (new code, old deps).
        if payload["receipt"]:
            resume_update_receipt(payload["receipt"])
        _record_update_step(
            "post_swap_handoff", False,
            "child interpreter could not start" if code is None
            else f"child interpreter exited {code} before claiming the hand-off")
        _m()._write_update_incomplete_marker()
        if payload_kwargs.get("gateway_mode"):
            _write_gateway_update_exit_code(False)
        code = 1
    sys.exit(code)


def _run_post_swap_phase(args, gateway_mode: bool) -> None:
    """Child half of the update (``--post-swap``): resume the receipt and finish the run on the
    pulled code."""
    from hermes_cli.update_completion import _resume_receipt as resume_update_receipt

    payload = _update_handoff.read_handoff(args.post_swap)
    # Unlinking the hand-off claims the receipt and the Windows resume from the parent
    # (``immutable_update_handoff.reclaim_handoff``); a failed unlink leaves both with it.
    Path(args.post_swap).unlink()
    if payload.get("receipt"):
        resume_update_receipt(payload["receipt"])
    if payload.get("windows_gateway_resume"):
        import atexit as _atexit
        _atexit.register(_m()._resume_windows_gateways_after_update, payload["windows_gateway_resume"])
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
    # Flags and config resolve here exactly as they did pre-swap; the three pre-update
    # snapshots come from the payload (the new tree would report the NEW version).
    opts = _replace(
        _resolve_update_options(args, gateway_mode),
        pre_update_version=payload.get("pre_update_version"))
    had_desktop_app_before_update = bool(payload.get("had_desktop_app_before_update"))
    try:
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
            if opts.no_gateway_restart:
                _record_update_skip("immutable_activation", "staged; activation deferred")
            else:
                _record_update_step("immutable_activation", True,
                                    f"activated release {release.name} ({release})")
            if not opts.no_gateway_restart:
                restart = _restart_gateway_fleet_after_update(
                    _pre_update_plan, gateway_mode, acknowledged_release_root=release)
                _resume_windows_gateways_and_merge_outcome(restart, _windows_gateway_resume, gateway_mode)
                _verify_fleet_after_update(
                    restart, _pre_update_plan=_pre_update_plan, _windows_gateway_resume=_windows_gateway_resume,
                    update_complete=update_complete, expected_sha=release.name,
                    expected_root=release)
            else:
                _record_update_step("immutable_release", True, "activation deferred; fleet restart not requested")
                _finalize_receipt("success", "Immutable release staged: %s")
            return
        raise ValueError("this continuation only accepts immutable release payloads")
    except _shim_quarantine_error_type() as e:
        _refuse_update_for_contended_shims(e)
    except subprocess.CalledProcessError as e:
        _handle_update_called_process_error(
            e, args, gateway_mode, had_desktop_app_before_update,
            _windows_gateway_resume=_windows_gateway_resume)




def _pause_gateways_for_update(opts):
    """Windows pauses here (venv locks); Linux/macOS only arm a pause the commit point performs."""
    try:
        return _m()._pause_windows_gateways_for_update() or _posix_pause.arm_pause(
            no_gateway_restart=opts.no_gateway_restart)
    except RuntimeError:  # update_cmd_windows._abort_on_error / ServicePauseFailed: nothing moved
        _record_stop("gateway_pause_failed")
        raise


def _update_run_channel(args) -> str:
    """``_source_update_channel`` for this run, naming the exit when the configured channel is invalid."""
    try:
        return _source_update_channel(args)
    except ValueError:  # ChannelError: this install's configured channel is not a valid name
        _record_stop("channel_unresolved")
        raise




from hermes_cli.update_cmd_apply import (
    _run_immutable_rollback, _prepare_update_attempt, _cmd_update_impl,
)


from hermes_cli.update_cmd_checkout import (
    _reconcile_diverged_checkout, _rollback_if_pulled_syntax_error, _roll_back_broken_update,
    _landed_off_target, _refuse_unselected_head, _upstream_broken_header, _update_movement_baseline,
    _resolved_commit, _move_checkout_to, _pull_updates, _switch_branch_at_commit_point,
    _refuse_before_commit_point, _exit_after_failed_branch_switch, _missing_branch_stop,
    _apply_parked_branch_guard, _prepare_checkout_for_update,
)
