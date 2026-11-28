"""Update attempt preflight, immutable rollback and source-apply orchestration."""

from __future__ import annotations


def _update_preflight_handled(args, project_root, resolve_update_branch) -> bool:
    """Managed-install refusal, --plan, admission gate, --check. True = nothing more to do."""
    import json
    import sys

    from hermes_cli.config import is_managed, managed_error
    from hermes_cli.update_channel import handle_metadata_args
    from hermes_cli.update_cmd_common import _record_stop

    if getattr(args, "expect_release", None) is not None:
        incompatible = ("check", "plan", "list_venv_holders", "set_channel",
                        "install_id", "channel", "replace_candidate", "allow_drop_main", "no_backup")
        branch = bool(getattr(args, "branch", None))
        rollback = bool(getattr(args, "rollback", False))
        if branch == rollback or any(getattr(args, flag, False) for flag in incompatible):
            print("✗ --expect-release requires either --branch or --rollback without overrides.")
            _record_stop("commit_point_refused", without_receipt="refused")
            raise SystemExit(2)
    if handle_metadata_args(args, project_root):
        sys.exit(0)
    if is_managed():
        managed_error("update Hermes Agent")
        if not any(getattr(args, flag, False) for flag in ("plan", "check", "list_venv_holders")):
            _record_stop("managed_install", without_receipt="refused")  # an update attempt: a metrics row only
        return True

    # --plan is read-only and deployment-kind aware, so it runs BEFORE the
    # docker/nix/apt refusal gates: on an image/package-managed install the
    # plan itself reports "not updatable in place" plus the right mechanism.
    if getattr(args, "plan", False):
        # Read-only plan phase (#91277 Phase 2): inventory every running Hermes runtime across profiles, its
        # supervisor, and its running code version — without mutating anything. Safe on a live fleet.
        from hermes_cli.update_inventory import (
            collect_runtime_inventory,
            print_update_plan,
        )

        print_update_plan(collect_runtime_inventory())
        return True

    if getattr(args, "list_venv_holders", False):
        # Read-only twin of the Windows venv-holder refusal (#117246): same scan and classifiers,
        # machine-readable, exit 3 when holders remain so automation can stop those PIDs and retry.
        import json

        from hermes_cli.update_cmd_windows import VENV_HOLDERS_EXIT, list_venv_holders

        holders = list_venv_holders()
        print(json.dumps(holders, indent=2))
        if holders:
            sys.exit(VENV_HOLDERS_EXIT)
        return True

    # Image/package-managed admission gate (#91277 Phase 3): baked provenance marker first (fail-closed
    # on malformed), then docker/nix/apt heuristics; one shared decision for every mutation surface, so
    # --check never reports git state for an image-managed install. Prints the real update command,
    # records a `refused` receipt and exits 2 (refused-by-contract, distinct from exit 1 errors).
    from hermes_cli.update_contract import (
        evaluate_update_admission,
        record_refusal_receipt,
    )

    refusal = evaluate_update_admission(project_root)
    if refusal is not None:
        print(refusal.message)
        record_refusal_receipt(refusal)
        sys.exit(2)

    if getattr(args, "check", False):
        # --check honors --branch so its answer matches what update would pull.
        branch = resolve_update_branch(args)
        from hermes_cli.update_cmd import _cmd_update_check

        _cmd_update_check(
            branch=branch,
            branch_explicit=bool(getattr(args, "branch", None)),
            **({"channel": args.channel} if getattr(args, "channel", None) else {}),
        )
        return True
    return False


def _run_immutable_rollback(args, gateway_mode: bool) -> None:
    """Recover or apply a rollback and verify its acknowledged fleet."""
    from hermes_cli.update_cmd import (
        Path, _await_release_acknowledgement, _finalize_receipt, _finish_pending_release_transaction,
        _record_update_step, _restart_gateway_fleet_after_update, _verify_fleet_after_update,
        _write_gateway_update_exit_code, get_hermes_home, json, sys,
    )
    from hermes_cli.immutable_releases import ReleasePaths, read_pointer, rollback
    from hermes_cli.update_receipt import begin_update_receipt
    begin_update_receipt()
    home = get_hermes_home()
    paths = ReleasePaths.for_home(home)
    pin = getattr(args, "expect_release", None)
    # Finish an interrupted rollback without deriving A from pointers already
    # swapped toward A. Otherwise a second --rollback would undo itself.
    pending_path = paths.home / "release-txn.json"
    pending_operation = None
    if pin is None and pending_path.exists():
        pending_operation = json.loads(pending_path.read_text(encoding="utf-8-sig"))["operation"]
    before = read_pointer(paths.current)
    recovered = _finish_pending_release_transaction(home) if pin is None else None
    if pending_operation in {"rollback", "first-migration-rollback"} and recovered is not None:
        result = recovered
        before = Path(recovered["previous"]) if recovered.get("previous") else before
    else:
        if before is None:
            raise RuntimeError("cannot roll back without a current release")
        from hermes_cli import gateway, gateway_launchd
        plist = gateway.get_launchd_plist_path() if sys.platform == "darwin" else None
        previous = read_pointer(paths.previous)
        if pin is not None and previous != paths.releases.resolve() / pin[1]:
            from hermes_cli.update_cmd_candidate import refuse_release_pin
            refuse_release_pin(f"expected rollback release {pin[1]}, found {previous.name if previous else 'no release'}")
        source_layout = previous is not None and previous.parent != paths.releases.resolve()
        plist_body = None
        if plist and plist.exists() and not source_layout and previous is not None:
            plist_body = gateway.generate_launchd_plist(release_target=previous).encode("utf-8")
        callback = (lambda: gateway_launchd._reload_installed_launchd_plist(
            plist, use_planned_handover=False)) if plist and plist.exists() else None
        result = rollback(home, plist_path=plist if plist and plist.exists() else None,
                          plist_body=plist_body, reload_callback=callback,
                          **({"expected_release": pin} if pin is not None else {}))
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
            last = json.loads(completed.read_text(encoding="utf-8-sig"))
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
        update_complete=not restart.incomplete,
        expected_sha=result.get("source_sha") if source_layout else Path(current_target).name,
        expected_root=Path(current_target), rollback=True,
    )
    return


def _prepare_update_attempt(args, gateway_mode: bool):
    """Resolve restart policy and recover pending transactions before applying an update."""
    from hermes_cli.update_cmd import (
        _check, _commit, _finalize_receipt, _finish_pending_release_transaction, _m, _record_stop,
        _record_update_step, _require_immutable_launchd, _resolve_update_options, get_hermes_home,
        git_operation_in_progress,
    )
    _commit.begin_update_attempt()  # nothing an earlier update in this process armed is ours (O5)
    pin = getattr(args, "expect_release", None)
    if pin is not None:
        if getattr(args, "post_swap", None):
            # The child claims the original receipt before checking the pin.
            # Recovering a different pending activation here would bypass it.
            return _resolve_update_options(args, gateway_mode)
        from hermes_cli.update_cmd_candidate import refuse_release_pin, require_pinned_live_release
        require_pinned_live_release(get_hermes_home(), pin[0])
        if not getattr(args, "rollback", False) and not (_m().PROJECT_ROOT / ".git").exists():
            refuse_release_pin("the owning Git checkout is unavailable")
    from hermes_cli.update_proxy_env import prefer_dotenv_proxy_env
    prefer_dotenv_proxy_env()  # the .env loaded at import beats a system proxy the Desktop injected
    # A user's active Git operation owns the checkout, including its index.
    # Refuse before release recovery, snapshots or branch switching can mutate it.
    git_operation = (None if getattr(args, "rollback", False)
                     else git_operation_in_progress(_m().PROJECT_ROOT))
    if git_operation:
        root = _m().PROJECT_ROOT
        print(f"✗ Cannot update while a Git {git_operation} is in progress in {root}.")
        print(f"  Finish it or run `git {git_operation} --abort`, then re-run `hermes update`.")
        _record_stop("git_in_progress", without_receipt="failed")  # before the receipt opens: a metrics row only
        raise SystemExit(1)
    if not getattr(args, "rollback", False):
        # Self-heal abandoned .git/*.lock files (a crashed fetch) and aborted-transfer pack temps
        # before anything else touches the checkout. Run at START, not only in the apply path
        # below: a run that dies between here and the fetch would otherwise leave the next run to
        # fail with "File exists" until an operator removed the lock by hand (#132089). Idempotent,
        # and _sweep_stale skips everything while a git process holds it. A killed update's
        # index.lock is younger than the sweep's age floor, so it goes here once its git is proven
        # dead (we hold the update lock: no other update's git can own it).
        from hermes_cli.gitlock import release_dead_index_lock
        if release_dead_index_lock(_m().PROJECT_ROOT):
            print("  (removed .git/index.lock left by a git that was killed)")
        _check.clear_git_debris(_m().PROJECT_ROOT)
    # Rollback does not fetch, prompt, inspect dependency options or mutate the checkout.

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

    # Self-heal abandoned .git/*.lock files (a crashed fetch) and aborted-transfer pack temps
    # before anything else touches the checkout. Run at START, not only in the apply path
    # below: a run that dies between here and the fetch would otherwise leave the next run to
    # fail with "File exists" until an operator removed the lock by hand (#132089). Idempotent,
    # and _sweep_stale skips everything while a git process holds it. A killed update's
    # index.lock is younger than the sweep's age floor, so it goes here once its git is proven
    # dead (we hold the update lock: no other update's git can own it). Its killed fetch's ref
    # and shallow locks likewise, or every later fetch fails ("reference already exists").
    from hermes_cli.gitlock import release_dead_fetch_locks, release_dead_index_lock
    if release_dead_index_lock(_m().PROJECT_ROOT):
        print("  (removed .git/index.lock left by a git that was killed)")
    for lock in release_dead_fetch_locks(_m().PROJECT_ROOT):
        print(f"  (removed {lock} left by a git that was killed)")
    _check.clear_git_debris(_m().PROJECT_ROOT)
    return opts


def _fetch_source_update(git_cmd, branch, release_sha, target_is_head, target_ref, windows_gateway_resume) -> None:
    """Repair shallow history and fetch the selected target before checkout preparation."""
    from hermes_cli.update_cmd import (
        _check, _m, _heal_stale_shallow_checkout, _git_run, _print_fetch_failure, _record_stop, subprocess,
        sys,
    )
    _check.report_pack_tidy(_m().PROJECT_ROOT)
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

    # A shallow checkout's plain fetch drags in ~the whole history and cannot finish inside
    # the network cap (#123254); its grafts also make merge-base report orphan divergence
    # (#123346, #124645). Unshallow it (commits only) first.
    _heal_stale_shallow_checkout(_m().PROJECT_ROOT, branch)

    if release_sha and target_is_head:
        # Forward-only default: the checkout already contains the release; nothing to fetch.
        fetch_args = None
    elif release_sha:
        fetch_args = ["fetch", "--no-tags", "origin", target_ref]
    else:
        fetch_args = ["fetch", "origin", _check.tracking_refspec("origin", branch)]
        if branch != "main":
            # A side-branch update must refresh the comparison history too.
            fetch_args.append("+refs/heads/main:refs/remotes/origin/main")
    from hermes_cli.gitlock import (
        fetch_with_partial_clone_recovery, is_partial_clone_pack_objects_crash,
    )
    # Marking the unmarked packs clears the git 2.53+ partial-clone pack-objects crash (#124272).
    if fetch_args is None:
        fetch_result = subprocess.CompletedProcess([], 0)
    else:
        print("→ Fetching updates...")
        fetch_result = fetch_with_partial_clone_recovery(
            lambda gc, a: _git_run(gc, a, network=True), git_cmd, fetch_args, _m().PROJECT_ROOT)
    if fetch_result.returncode != 0:
        if is_partial_clone_pack_objects_crash(fetch_result.stderr or ""):
            print("✗ git still crashed after marking this checkout's packs. See 'Fetch fails with"
                  " should_include_obj' in https://hermes-agent.nousresearch.com/docs/getting-started/updating")
        _print_fetch_failure(fetch_result.stderr, git_cmd=git_cmd, root=_m().PROJECT_ROOT)
        _m()._resume_windows_gateways_after_update(windows_gateway_resume)
        _record_stop("git_timeout" if fetch_result.returncode == 124 else "disk_full"
                     if "No space left on device" in (fetch_result.stderr or "") else "fetch_failed")
        sys.exit(1)


def _cmd_update_impl(args, gateway_mode: bool):
    """Body of ``cmd_update`` — kept separate so the wrapper can always restore stdio even on
    ``sys.exit``. Self-lock deferral deliberately does NOT run here (pre-fetch it stranded users
    on the OLD checkout in an exit-2 loop); it runs right before the dependency sync."""
    from hermes_cli.update_cmd import (
        _apply_fetched_immutable_update, _apply_pulled_update, _begin_update_receipt_and_plan, _check,
        _commit, _completion_receipt, _current_branch_name, _finish_already_up_to_date,
        _guard_against_main_drop, _handle_update_called_process_error, _immutable_release_enabled, _m,
        _moves_for, _pause_gateways_for_update, _prepare_checkout_for_update, _prepare_git_command,
        _prepare_update_attempt, _pull_updates, _record_pre_update_backup_outcome, _record_snapshot_stage,
        _refuse_before_commit_point, _resume_paused_gateways_at_exit,
        _resume_windows_gateways_after_update, _run_immutable_rollback, _run_post_swap_phase,
        _source_completion_request, _update_run_channel, _update_via_zip, subprocess,
    )
    _hermes_current_updater_frame = True
    opts = _prepare_update_attempt(args, gateway_mode)

    if getattr(args, "rollback", False):
        _run_immutable_rollback(args, gateway_mode)
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
    _record_pre_update_backup_outcome(args, pre_update_snapshot_id)
    _record_snapshot_stage(args, pre_update_snapshot_id)
    pin = getattr(args, "expect_release", None)
    if pin is not None and not pre_update_snapshot_id:
        from hermes_cli.update_cmd_candidate import refuse_release_pin
        refuse_release_pin("a successful pre-update quick snapshot is required")
    from hermes_cli.update_stage import publish_stage
    publish_stage("Downloading updates")

    _windows_gateway_resume = _pause_gateways_for_update(opts)
    if _windows_gateway_resume:
        import atexit as _atexit
        _atexit.register(_m()._resume_windows_gateways_after_update, _windows_gateway_resume)
    desktop_dir = _m().PROJECT_ROOT / "apps" / "desktop"
    # An installed Hermes.app only this update refreshes counts even with no release/ build
    # beside it: without one it was never rebuilt, so it never got newer (#52339).
    had_desktop_app_before_update = (
        _m()._desktop_packaged_executable(desktop_dir) is not None
        or _m()._desktop_dist_exists(desktop_dir)
        or bool(_m()._installed_desktop_apps()))

    use_zip_update, git_cmd, is_fork = _prepare_git_command(checkout_move=_moves_for(_windows_gateway_resume))

    completion_request = _source_completion_request(
        opts, _pre_update_plan, pre_update_snapshot_id, _windows_gateway_resume,
        had_desktop_app_before_update, gateway_mode)
    branch = _m()._resolve_update_branch(args)
    completion_request["branch"] = branch
    target_ref, release_sha, target_is_head, target_repository = _check.select_apply_target(
        args, branch, completion_request, git_cmd=None if use_zip_update else git_cmd,
        stop=lambda: _m()._resume_windows_gateways_after_update(_windows_gateway_resume))
    branch = completion_request["branch"]

    if pin is not None and (use_zip_update or not _immutable_release_enabled()):
        from hermes_cli.update_cmd_candidate import refuse_release_pin
        refuse_release_pin("the immutable Git update path is unavailable")
    if use_zip_update:
        try:
            _update_via_zip(
                args, had_desktop_app_before_update=had_desktop_app_before_update,
                target_sha=release_sha, completion_request=completion_request,
                **({"target_repository": target_repository} if target_repository else {}))
        finally:
            _resume_paused_gateways_at_exit(_windows_gateway_resume, completion_request)

        return

    try:
        _fetch_source_update(
            git_cmd, branch, release_sha, target_is_head, target_ref, _windows_gateway_resume)

        _check.fetch_release_tag(git_cmd, _m().PROJECT_ROOT, completion_request)
        if not isinstance(release_sha, str):
            _guard_against_main_drop(
                args, git_cmd, target_ref, target_is_head=target_is_head,
                resume_gateways=lambda: _resume_windows_gateways_after_update(_windows_gateway_resume),
            )

        if _immutable_release_enabled():
            _apply_fetched_immutable_update(
                git_cmd, branch, opts, args, gateway_mode=gateway_mode,
                had_desktop_app_before_update=had_desktop_app_before_update,
                pre_update_snapshot_id=pre_update_snapshot_id, _pre_update_plan=_pre_update_plan,
                _windows_gateway_resume=_windows_gateway_resume)
            return

        # The fetch-only proof must not survive into a phase that may stash or rewrite files.
        _completion_receipt.record_step(
            "local_changes_stash", False, "unverified: checkout preparation started", replace=True)
        current_branch = _current_branch_name(git_cmd, check=True)
        _commit.record_run_start(git_cmd, _m().PROJECT_ROOT)
        held_message = None
        if release_sha and not target_is_head:
            channel = _update_run_channel(args)
            held = _check.hold_channel_release_behind_runtime(git_cmd, target_ref, channel)
            if held:
                target_ref = held
                held_message = f"✓ Holding at this build until a newer {channel} release ships."
        _refuse_before_commit_point(git_cmd, target_ref, _windows_gateway_resume)
        _plan = _prepare_checkout_for_update(
            git_cmd, branch, current_branch, is_fork=is_fork, assume_yes=assume_yes,
            gateway_mode=gateway_mode, gw_input_fn=gw_input_fn, switch_branch=opts.switch_branch,
            target_ref=target_ref, _windows_gateway_resume=_windows_gateway_resume)
        commit_count = _plan.commit_count

        if commit_count == 0:
            _finish_already_up_to_date(
                git_cmd, branch, current_branch, _plan, gw_input_fn=gw_input_fn,
                completion_request=completion_request, _windows_gateway_resume=_windows_gateway_resume,
                held_message=held_message)
            return

        if release_sha:
            print(f"→ Switching to source commit {release_sha[:10]}")
        elif commit_count > 0:
            print(f"→ Found {commit_count} new commit(s)")
        elif _plan.switched_without_new_commits:
            print(f"→ Switched the running checkout to {branch} (no new commits to count)")
        else:
            # Shallow, exact count unrecoverable — but the tips differ, so there IS an update.
            print("→ Updates available (commit count unknown on this shallow checkout)")

        print("→ Pulling updates...")
        movement_baseline = _pull_updates(
            git_cmd, branch, _plan.auto_stash_ref, prompt_for_restore=_plan.prompt_for_restore,
            gw_input_fn=gw_input_fn, discard_local_changes=opts.discard_local_changes,
            keep_stash=opts.keep_stash, target_ref=target_ref, pre_sync_sha=_plan.pre_sync_sha,
            rollback_branch=_plan.rollback_branch,
            sync_upstream=is_fork and branch == "main" and not release_sha, assume_yes=assume_yes,
            in_place_update=_plan.in_place_update, _windows_gateway_resume=_windows_gateway_resume,
            expected_sha=completion_request.get("expected_sha"))
        _apply_pulled_update(
            git_cmd, branch, movement_baseline, _plan,
            _windows_gateway_resume=_windows_gateway_resume, completion_request=completion_request)
    except subprocess.CalledProcessError as e:
        try:
            _handle_update_called_process_error(
                e, args, gateway_mode, had_desktop_app_before_update, target_sha=release_sha,
                target_repository=target_repository, completion_request=completion_request)
        finally:
            _resume_paused_gateways_at_exit(_windows_gateway_resume, completion_request)
