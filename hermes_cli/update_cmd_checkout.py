"""Checkout planning, branch movement and recovery for source updates."""

from __future__ import annotations

from typing import NoReturn

def _reconcile_diverged_checkout(git_cmd, branch: str, pre_pull_sha, *, target_ref=None) -> None:
    """Reconcile a proven non-fast-forwardable checkout.

    The caller must first establish that HEAD is not an ancestor of the update target; ordinary
    fast-forward failures (locks, object transport errors) must never reach this destructive path.
    Merge on a custom branch (local commits survive) or reset --hard on the same branch after
    parking the old HEAD behind a rescue ref. ``sys.exit(1)`` on failure."""
    from hermes_cli.update_cmd import (
        UTC, _ORPHAN_RESCUE_REF_MAX_AGE_DAYS, _git_run, _m, _prune_orphan_rescue_refs, _record_stop, _time,
        sys,
    )
    # A custom branch (local commits atop origin/<branch>) also can't ff, and reset --hard
    # would discard that work: merge instead, stop on conflict.
    merge_ref = target_ref if target_ref is not None else f"origin/{branch}"
    _cur_branch = (_git_run(git_cmd, ["branch", "--show-current"]).stdout or "").strip()
    if _cur_branch and _cur_branch != branch:
        print(
            f"  ⚠ Checkout is on custom branch '{_cur_branch}' — "
            f"merging origin/{branch} instead of resetting so local commits survive...")
        # Best-effort safety tag as a recovery anchor.
        _git_run(git_cmd, ["tag", f"pre-update-{_time.strftime('%Y%m%d-%H%M%S')}"])
        if _git_run(git_cmd, ["merge", "--no-edit", merge_ref]).returncode != 0:
            _git_run(git_cmd, ["merge", "--abort"])
            print("✗ Merge conflict between local commits and upstream — update stopped, nothing was changed.")
            print(f"  Resolve manually: cd {_m().PROJECT_ROOT} && git merge origin/{branch}")
            print("  Then re-run the update. Local work is untouched.")
            _record_stop("merge_conflict")
            sys.exit(1)
        return
    # Same branch and proven non-ancestor: an upstream force-push/rebase may lose nothing,
    # while local commits on this branch lose everything. The reflog is only an expiring
    # recovery path, so park pre_pull_sha behind a rescue ref for BOTH, orphan divergence
    # (no common ancestor: corrupted HEAD, re-init) included.
    merge_base_result = _git_run(git_cmd, ["merge-base", "HEAD", merge_ref])
    has_common_ancestor = bool(
        merge_base_result.returncode == 0 and merge_base_result.stdout.strip())
    if pre_pull_sha:
        from datetime import datetime as _dt, timezone
        # SHA suffix so two updates in the same second get distinct refs.
        kind = "diverged" if has_common_ancestor else "orphan"
        rescue_ref = (
            f"refs/hermes-update-backups/{kind}-{branch}-"
            f"{_dt.now(UTC).strftime('%Y%m%d-%H%M%S')}-{pre_pull_sha[:12]}")
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
    reset_result = _git_run(git_cmd, ["reset", "--hard", merge_ref])
    if reset_result.returncode != 0:
        print(f"✗ Failed to reset to origin/{branch}.")
        if reset_result.stderr.strip():
            print(f"  {reset_result.stderr.strip()}")
        print(f"  Try manually: git fetch origin && git reset --hard origin/{branch}")
        _record_stop("checkout_move_failed")
        sys.exit(1)


def _rollback_if_pulled_syntax_error(git_cmd, pre_pull_sha, *, rollback_branch=None, _windows_gateway_resume=None) -> None:
    """Post-pull syntax guard: roll back to *pre_pull_sha* and ``sys.exit(1)`` when a critical
    file no longer compiles (a bad admin-merge past CI must not brick the CLI)."""
    from hermes_cli.update_cmd import (
        Path, _UPDATE_CRITICAL_FILES, _commit, _m, _roll_back_broken_update,
        _validate_python_files_syntax,
    )
    pyproject = Path(_m().PROJECT_ROOT) / "pyproject.toml"
    if pyproject.is_file() and _commit.requires_other_python(pyproject.read_bytes()):
        # A newer Python's syntax: judged by an installed interpreter the target admits (N15).
        broken = _commit.tree_syntax_error(_m().PROJECT_ROOT, _UPDATE_CRITICAL_FILES)
        syntax_ok, failing_path, syntax_error = (broken is None, *(broken or (None, None)))
    else:
        syntax_ok, failing_path, syntax_error = _validate_python_files_syntax(_m().PROJECT_ROOT, _UPDATE_CRITICAL_FILES)
    if not syntax_ok:
        _roll_back_broken_update(git_cmd, pre_pull_sha, failing_path, syntax_error, rollback_branch=rollback_branch,
                                 _windows_gateway_resume=_windows_gateway_resume)


def _roll_back_broken_update(git_cmd, pre_pull_sha, failing_path, syntax_error, *, rollback_branch=None,
                             header="✗ Pulled code has a syntax error in a critical file:",
                             _windows_gateway_resume=None, stop_class="syntax_rollback") -> NoReturn:
    """Restore the checkout to *pre_pull_sha* after a startup file failed the syntax check, then
    ``sys.exit(1)``."""
    from hermes_cli.update_cmd import (
        _capture_head_sha, _checkout_move, _commit, _git_run, _m, _record_stop, interrupted_pull_marker,
        shlex, subprocess, sys,
    )
    print()
    print(header)
    print(f"  {failing_path}")
    # py_compile errors can be multi-line; show enough for the SyntaxError text.
    for line in str(syntax_error).splitlines()[:6] if syntax_error else ():
        print(f"    {line}")
    print()
    if pre_pull_sha:
        # Restore the checkout the update left, never reset the update branch onto commits from
        # a parked feature branch or a detached checkout.
        root = _m().PROJECT_ROOT
        parked = rollback_branch not in (None, "HEAD")
        print(f"→ Rolling back to {rollback_branch if parked else pre_pull_sha[:10]}...")
        # HEAD and the index go back FIRST, without writing a file, then the files follow. A kill in
        # between leaves HEAD on pre_pull_sha with the broken files reading as the move's own
        # writes, so the next launch's restore lands on pre_pull_sha, never on the broken commit;
        # ``rollback=`` lets it redo the HEAD step when the kill came before it.
        mode = "branch" if rollback_branch is None else "detach"
        target_sha = _capture_head_sha(git_cmd, root)
        try:
            _commit.arm_tree_move(git_cmd, root, pre=pre_pull_sha, target=target_sha, stash=None, rollback=mode)
        except (OSError, subprocess.SubprocessError) as exc:  # its symbolic-ref can time out
            # Refusing would leave the broken tree in place: roll back unmarked, and only a tree
            # verified whole at pre_pull_sha below may report success (else the manual recipe).
            print(f"  ⚠ Could not write the interrupted-pull marker ({exc}); rolling back without it.")
        # The paused gateways' tree gate knows the rollback's target before git writes a file.
        with _checkout_move(_windows_gateway_resume, pre_pull_sha):
            added = _commit.files_added_by(git_cmd, root, pre_pull_sha, target_sha)
            if mode == "detach":  # never move the update branch onto a parked/detached commit
                rollback_args = ["update-ref", "--no-deref", "HEAD", pre_pull_sha]
                rollback_result = _git_run(git_cmd, rollback_args)
            if mode == "branch" or rollback_result.returncode == 0:
                rollback_args = ["reset", "-q", pre_pull_sha]  # HEAD (and the index) only, no file
                rollback_result = _git_run(git_cmd, rollback_args)
            if rollback_result.returncode == 0:
                rollback_args = ["reset", "--hard", pre_pull_sha]
                rollback_result = _git_run(git_cmd, rollback_args)
                if rollback_result.returncode == 0 and not _commit.drop_added_files(root, added):
                    rollback_result = subprocess.CompletedProcess(
                        rollback_args, 1, "", "could not remove the broken release's added files")
            if rollback_result.returncode == 0 and not _commit.tree_whole_at(git_cmd, root, pre_pull_sha):
                # git said yes; the tree must agree (HEAD on pre, no tracked change) before the marker goes.
                rollback_result = subprocess.CompletedProcess(
                    rollback_args, 1, "", f"the checkout is not whole at {pre_pull_sha[:10]}")
            if rollback_result.returncode != 0 and _commit.settle_failed_tree_move(root):
                rollback_result = subprocess.CompletedProcess(rollback_args, 0, "", "")  # restored in-process
            if rollback_result.returncode == 0 and parked:
                # Same commit, so only HEAD's name changes. The parked branch can be unavailable (e.g.
                # checked out in another worktree): its commit then stays checked out detached.
                if _git_run(git_cmd, ["checkout", rollback_branch]).returncode != 0:
                    print(f"  ✗ Could not check out {rollback_branch}; restoring its commit detached.")
        if rollback_result.returncode == 0:
            interrupted_pull_marker(root).unlink(missing_ok=True)
            _commit.disarm_commit_obligations()  # back on the code this run started from
            print("  ✓ Rollback complete — your install is unchanged.")
            print("  Try ``hermes update`` again later once a fix lands.")
        else:
            print("  ✗ Rollback failed. Recover manually with:")
            print(f"    cd {root} && git {shlex.join(rollback_args)}")
            if rollback_result.stderr.strip():
                print(f"    ({rollback_result.stderr.strip().splitlines()[0]})")
            if interrupted_pull_marker(root).is_file():
                _commit.owe_restore_of(root, pre_pull_sha)
                print(f"  The next `hermes` launch also finishes the rollback to {pre_pull_sha[:10]} on its own.")
    else:
        print("  Could not capture pre-pull SHA — recover manually with:")
        print(f"    cd {_m().PROJECT_ROOT} && git reflog && git reset --hard <prev-sha>")
    _record_stop(stop_class)
    sys.exit(1)


def _landed_off_target(git_cmd, target_sha: str, *, merged: bool = True) -> str | None:
    """The HEAD a git move landed on when it is neither ``target_sha`` nor (``merged``: a custom
    branch's merge) a commit containing it, else None: the identity the marker and debt were armed for."""
    from hermes_cli.update_cmd import _git_run
    landed = (_git_run(git_cmd, ["rev-parse", "-q", "--verify", "HEAD"]).stdout or "").strip()
    if landed == target_sha or (merged and landed and _git_run(
            git_cmd, ["merge-base", "--is-ancestor", target_sha, landed]).returncode == 0):
        return None
    return landed or "an unreadable HEAD"


def _refuse_unselected_head(git_cmd, target_sha: str, unmoved: str | None) -> None:
    """``sys.exit(1)`` (the caller settles it as a failed move, its marker still on disk) when git
    landed on a commit other than the armed one (review O2/O3). A HEAD still on ``unmoved`` is
    ``_verify_head_after_pull``'s "code did not move" refusal."""
    from hermes_cli.update_cmd import _landed_off_target, _record_stop, sys
    if (landed := _landed_off_target(git_cmd, target_sha)) not in (None, unmoved):
        print(f"✗ The checkout landed on {landed[:10]}, not the selected commit {target_sha[:10]} "
              "(a ref moved during the update).")
        _record_stop("head_moved")
        sys.exit(1)


def _upstream_broken_header(broken) -> str:
    return f"✗ upstream/main ({broken.sha[:10]}) has a syntax error in a critical file; it was not merged:"


def _update_movement_baseline(git_cmd, pre_pull_sha, pre_sync_sha, rollback_branch, target_sha):
    """Distinguish a branch awaiting repair from an already-applied early sync."""
    from hermes_cli.update_cmd import _git_run
    if rollback_branch is not None and pre_pull_sha and target_sha:
        # Target already in the branch => checkout/early sync did the moving (even if its push
        # failed), so count from pre_sync. Anything else (rc 1, or 128 for an unresolvable SHA)
        # verifies from the branch tip, so a no-op merge fails loudly instead of passing.
        contains_target = _git_run(
            git_cmd, ["merge-base", "--is-ancestor", target_sha, pre_pull_sha])
        if contains_target.returncode != 0:
            return pre_pull_sha
    return pre_sync_sha or pre_pull_sha


def _resolved_commit(git_cmd, ref: str) -> str:
    """The commit *ref* names now, or ``""`` when it names none."""
    from hermes_cli.update_cmd import _git_run
    return (_git_run(git_cmd, ["rev-parse", "-q", "--verify", f"{ref}^{{commit}}"]).stdout or "").strip()


def _move_checkout_to(git_cmd, branch, merge_ref, target_sha, pre_pull_sha) -> None:
    """The principal pull's one checkout move onto *target_sha*; exits when it cannot be made."""
    from hermes_cli.update_cmd import (
        _git_run, _m, _park_detached_head, _reconcile_diverged_checkout, _record_stop, sys,
    )
    # merge --ff-only the already-fetched commit instead of `git pull`, which would do a
    # SECOND network fetch. Every move names ``target_sha``, never the ref: a ref that moves
    # after the arm cannot land a commit the marker and the debt do not name (review O3).
    if merge_ref != f"origin/{branch}":
        # Keep detached local commits reachable, too. Named branches are
        # untouched by checkout --detach; an autostash protects dirty files.
        _park_detached_head(git_cmd, _m().PROJECT_ROOT, branch)
        _git_run(git_cmd, ["checkout", "--detach", target_sha], check=True)
        return
    merge_result = _git_run(git_cmd, ["merge", "--ff-only", target_sha])
    if merge_result.returncode == 0:
        return
    ancestry = _git_run(git_cmd, ["merge-base", "--is-ancestor", "HEAD", target_sha])
    if ancestry.returncode == 1:
        _reconcile_diverged_checkout(git_cmd, branch, pre_pull_sha, target_ref=target_sha)
        return
    print("✗ Fast-forward failed; refusing to reset because history divergence was not proven.")
    detail = (merge_result.stderr or merge_result.stdout or "").strip()
    if detail:
        print(f"  {detail}")
    if ancestry.returncode == 0:
        print(f"  HEAD is still an ancestor of {merge_ref}.")
    else:
        print(f"  Could not verify whether HEAD is an ancestor of {merge_ref}.")
        ancestry_detail = (ancestry.stderr or ancestry.stdout or "").strip()
        if ancestry_detail:
            print(f"  {ancestry_detail}")
    print("  Resolve the Git error and re-run `hermes update`; no reset was attempted.")
    from hermes_cli.update_receipt import git_output_stop_class

    # The shared classifier: an index.lock that EXISTS is another git's; "Permission denied" on it is not.
    _record_stop(git_output_stop_class(detail, ["merge"], merge_result.returncode) or "checkout_move_failed")
    sys.exit(1)


def _pull_updates(
    git_cmd, branch, auto_stash_ref, *, prompt_for_restore, gw_input_fn, discard_local_changes,
    keep_stash, target_ref=None, pre_sync_sha=None, rollback_branch=None, sync_upstream=False, assume_yes=False,
    in_place_update=False, _windows_gateway_resume=None, expected_sha=None):
    """Fast-forward onto ``origin/<branch>`` and settle the autostash. Divergence by shape:
    custom branch -> merge, same branch -> rescue ref then reset; a
    post-pull syntax error in a critical file rolls back. Exits on failure; returns the SHA HEAD had to move off."""
    from hermes_cli.update_cmd import (
        UpstreamTargetBroken, _capture_head_sha, _checkout_move, _clear_pending_autostash, _commit,
        _git_run, _m, _move_checkout_to, _moves_for, _push_synced_fork, _record_stop,
        _refuse_unselected_head, _roll_back_broken_update, _rollback_if_pulled_syntax_error,
        _update_movement_baseline, _upstream_broken_header, _verify_head_after_pull,
        interrupted_pull_marker, is_object_id, sys,
    )
    update_succeeded = False
    # Rescue refs must retain the immediate pre-pull tip, even when syntax
    # rollback needs to cross an earlier upstream sync.
    pre_pull_sha = _capture_head_sha(git_cmd, _m().PROJECT_ROOT)
    # Git moves the tree file by file and HEAD last: if this process dies in between, the next launch
    # of any entry point finds this marker and puts the old tree back (_early_recovery). The target is
    # the resolved commit, so a later `git fetch` cannot widen what that restore considers.
    pull_marker = interrupted_pull_marker(_m().PROJECT_ROOT)
    # A release update moves the tree to its tag, not the branch tip: the marker names what git writes.
    merge_ref = target_ref if target_ref is not None else f"origin/{branch}"
    # Every refusal below runs inside the try: its finally names the autostash this run took.
    try:
        target_sha = (_git_run(git_cmd, ["rev-parse", "-q", "--verify", f"{merge_ref}^{{commit}}"]).stdout
                      or "").strip()
        if not is_object_id(target_sha):
            # The marker must name the commit git moves to, or a killed move is unrecoverable.
            print(f"✗ Could not resolve {merge_ref} to a commit. No update was applied.")
            _record_stop("target_unresolved")
            sys.exit(1)
        movement_baseline = _update_movement_baseline(
            git_cmd, pre_pull_sha, pre_sync_sha, rollback_branch, target_sha)
        if expected_sha and target_sha != expected_sha:
            # Channel verification is a precondition: refuse before anything moves.
            print(f"✗ {merge_ref} resolves to {target_sha[:10]}, not the selected channel commit "
                  f"{expected_sha[:10]}. No update was applied.")
            _record_stop("head_moved")
            sys.exit(1)
        # The commit point: the tail, the fleet restart and the marker are durable BEFORE git writes.
        refused = _commit.arm_commit_point(git_cmd, _m().PROJECT_ROOT, target_sha, pre=pre_pull_sha,
                                           target=target_sha, stash=auto_stash_ref)
        if refused:
            print(f"✗ {refused}.")
            _record_stop("commit_point_refused")
            sys.exit(1)
        try:
            # The paused gateways' tree gate must know this move's target before git writes a file.
            with _checkout_move(_windows_gateway_resume, target_sha):
                _move_checkout_to(git_cmd, branch, merge_ref, target_sha, pre_pull_sha)
            _refuse_unselected_head(git_cmd, target_sha, pre_pull_sha)  # before the marker goes
        except KeyboardInterrupt:
            raise  # Ctrl-C reached git too (same process group): the tree may be torn, keep the marker
        except BaseException:
            # git exited on its own (sys.exit on conflict/reset failure, a locked or read-only file):
            # put back what it wrote; the marker stays until the tree is verified whole. Settled:
            # handed back at the run's start, else owed for the HEAD it settled on; torn: owed for
            # the commit the next launch's restore lands on.
            if _commit.settle_failed_tree_move(_m().PROJECT_ROOT):
                _commit.disarm_commit_obligations()
            else:
                _commit.owe_restore_of(_m().PROJECT_ROOT, pre_pull_sha)
            raise
        pull_marker.unlink(missing_ok=True)  # git exited 0: the tree is whole at the target
        if sync_upstream:
            # Do not let a second mutation hide a failed origin merge or move an
            # unexpected branch. Keep local edits parked through the final check.
            _verify_head_after_pull(
                git_cmd, branch, movement_baseline, in_place_update=in_place_update,
                _windows_gateway_resume=_windows_gateway_resume)
            try:
                _m()._sync_with_upstream_if_needed(
                    git_cmd, _m().PROJECT_ROOT, assume_yes=assume_yes, input_fn=gw_input_fn,
                    checkout_move=_moves_for(_windows_gateway_resume))
            except UpstreamTargetBroken as broken:  # refused before its move: the update rolls back
                _verify_head_after_pull(  # never reset an unexpected branch's ref onto pre
                    git_cmd, branch, movement_baseline, in_place_update=in_place_update,
                    _windows_gateway_resume=_windows_gateway_resume)
                _roll_back_broken_update(git_cmd, pre_sync_sha or pre_pull_sha, broken.path, broken.error,
                                         rollback_branch=rollback_branch, header=_upstream_broken_header(broken),
                                         _windows_gateway_resume=_windows_gateway_resume)
        # Refuse an unexpected branch before syntax rollback can reset its ref.
        _verify_head_after_pull(
            git_cmd, branch, movement_baseline, in_place_update=in_place_update,
            _windows_gateway_resume=_windows_gateway_resume)
        _rollback_if_pulled_syntax_error(
            git_cmd, pre_sync_sha or pre_pull_sha, rollback_branch=rollback_branch,
            _windows_gateway_resume=_windows_gateway_resume)
        if sync_upstream:
            # Only a validated update is published to the fork (#97052 sync, after the commit point).
            _push_synced_fork(git_cmd, _m().PROJECT_ROOT)
        update_succeeded = True
    finally:
        if auto_stash_ref is not None:
            # No stash restore if the update failed — tree state is unknown.
            if not update_succeeded:
                print(f"  ℹ️  Local changes preserved in stash (ref: {auto_stash_ref})")
                print("  Restore manually with: git stash apply")
                _clear_pending_autostash()  # named just above; a later boundary must not repeat it
            elif discard_local_changes:
                # Non-interactive + updates.non_interactive_local_changes: discard.
                _m()._discard_stashed_changes(git_cmd, _m().PROJECT_ROOT, auto_stash_ref)
            elif keep_stash:
                # --keep-stash (desktop updater): leave edits parked rather than re-apply silently.
                _m()._park_stashed_changes(auto_stash_ref)
            else:
                _m()._restore_stashed_changes(
                    git_cmd, _m().PROJECT_ROOT, auto_stash_ref, prompt_user=prompt_for_restore,
                    input_fn=gw_input_fn, checkout_move=_moves_for(_windows_gateway_resume))
    return movement_baseline


def _switch_branch_at_commit_point(git_cmd, branch, target_ref, *, pre, stash):
    """CP0: switch the parked checkout to *branch* under the interrupted-pull marker and the armed
    obligations, so a kill mid-switch is restored by the next launch like a killed pull."""
    from hermes_cli.update_cmd import (
        _ARM_REFUSED, _REF_MOVED, _commit, _git_run, _landed_off_target, _m, interrupted_pull_marker,
        subprocess,
    )
    root = _m().PROJECT_ROOT

    def resolve(ref):
        return (_git_run(git_cmd, ["rev-parse", "-q", "--verify", f"{ref}^{{commit}}"]).stdout or "").strip()

    # --no-guess: with no local *branch* git would otherwise create it from origin/<branch> and
    # rewrite the tree while this loop believed the checkout could not move.
    # The -B form names the resolved commit, never the ref (review O2); the local-branch form must
    # name the branch to attach HEAD to it, so its landing is verified against the armed commit.
    local, remote = resolve(f"refs/heads/{branch}"), resolve(f"origin/{branch}")
    attempts = [(["checkout", "--no-guess", branch], local),
                (["checkout", "-B", branch, remote or f"origin/{branch}"], remote)]
    result = None
    for args, target in attempts:
        if not target:
            if args is attempts[0][0]:
                continue  # no local branch: only the -B form can land on it
            _commit.disarm_commit_obligations()  # an earlier attempt's arm, its move settled
            return _git_run(git_cmd, args)  # unresolvable: git fails before touching the tree
        # Owe what this attempt lands on: a stop before CP1 must be dischargeable at *target*
        # (CP1 retargets the obligation before its own move).
        refused = _commit.arm_commit_point(git_cmd, root, target, pre=pre, target=target, stash=stash)
        if refused:  # nothing moved: the caller restores the autostash and exits
            return subprocess.CompletedProcess(_ARM_REFUSED, 1, "", refused)
        try:
            result = _git_run(git_cmd, args)
        except KeyboardInterrupt:
            raise
        except BaseException:
            if _commit.settle_failed_tree_move(root):
                _commit.disarm_commit_obligations()
            else:
                _commit.owe_restore_of(root, pre)
            raise
        if result.returncode == 0 and (landed := _landed_off_target(git_cmd, target, merged=False)) is not None:
            # The branch moved between its resolution and the checkout: the tree is whole at a commit
            # the debt does not name. Settled like a failed move: the debt follows the landed HEAD.
            print(f"  ✗ The branch moved to {landed[:10]} after {target[:10]} was selected.")
            if _commit.settle_failed_tree_move(root):
                _commit.disarm_commit_obligations()  # off the run's start: re-owed for the landed HEAD
            else:
                _commit.owe_restore_of(root, pre)
            return subprocess.CompletedProcess(_REF_MOVED, 1, result.stdout, (
                f"'{branch}' moved to {landed[:10]} during the switch; re-run `hermes update`"))
        if result.returncode == 0:
            interrupted_pull_marker(root).unlink(missing_ok=True)  # git exited 0: whole at *target*
            return result
        if not _commit.settle_failed_tree_move(root):
            _commit.owe_restore_of(root, pre)  # owed for what the next launch's restore lands on
            return result  # torn and unrestorable now: the marker stays for the next launch
    # Back at *pre*: handed back at the run's start commit, else re-owed for the HEAD it is on.
    _commit.disarm_commit_obligations()
    return result


def _refuse_before_commit_point(git_cmd, target_ref, _windows_gateway_resume) -> None:
    """Preconditions that used to fail AFTER the swap: refuse with the install untouched."""
    from hermes_cli.update_cmd import _UPDATE_CRITICAL_FILES, _commit, _m, _record_stop, sys
    reason = _commit.preflight_refusal(git_cmd, _m().PROJECT_ROOT, target_ref, _UPDATE_CRITICAL_FILES)
    if reason is None:
        from hermes_cli.update_downgrade_guard import downgrade_refusal
        reason = downgrade_refusal(git_cmd, _m().PROJECT_ROOT, target_ref)
        if reason is not None:
            _record_stop("downgrade_refused")
    if reason is None:
        return
    print(reason)
    print("  No update was applied; your install is unchanged.")
    _m()._resume_windows_gateways_after_update(_windows_gateway_resume)
    sys.exit(1)


def _exit_after_failed_branch_switch(
        git_cmd, branch, track_result, auto_stash_ref, gw_input_fn, _windows_gateway_resume=None) -> NoReturn:
    """CP0 did not land: say which of refused / torn / missing branch it was, then ``sys.exit(1)``.

    A torn switch keeps its marker for the next launch's restore, so the autostash stays parked:
    re-applying it onto a half-written tree would mix the user's edits into git's partial writes.
    """
    from hermes_cli.update_cmd import (
        _ARM_REFUSED, _REF_MOVED, _clear_pending_autostash, _m, _missing_branch_stop, _moves_for,
        _record_stop, interrupted_pull_marker, sys,
    )
    detail = (track_result.stderr or "").strip()
    if interrupted_pull_marker(_m().PROJECT_ROOT).is_file():
        print(f"✗ Switching to '{branch}' stopped part-way; the next `hermes` launch puts the checkout back.")
        if detail:
            print(f"  {detail.splitlines()[0]}")
        if auto_stash_ref is not None:
            print(f"  ℹ️  Local changes preserved in stash (ref: {auto_stash_ref})")
            print("  Restore manually with: git stash apply")
            _clear_pending_autostash()
        _record_stop("checkout_move_failed")
        sys.exit(1)
    # Nothing moved: restore the stash before bailing so the user isn't stranded.
    if auto_stash_ref is not None:
        _m()._restore_stashed_changes(
            git_cmd, _m().PROJECT_ROOT, auto_stash_ref, prompt_user=False, input_fn=gw_input_fn,
            checkout_move=_moves_for(_windows_gateway_resume))
    if track_result.args in (_ARM_REFUSED, _REF_MOVED):
        print(f"✗ {detail}.")
        _record_stop("commit_point_refused" if track_result.args is _ARM_REFUSED else "head_moved")
    else:
        print(f"✗ Branch '{branch}' does not exist locally or on origin.")
        if detail:
            print(f"  {detail.splitlines()[0]}")
        _record_stop(_missing_branch_stop(git_cmd, branch))
    sys.exit(1)


def _missing_branch_stop(git_cmd, branch) -> str:
    """``branch_missing`` unless the branch resolves (then git refused the switch). Never raises."""
    from hermes_cli.update_cmd import _resolved_commit
    try:
        return "checkout_move_failed" if any(
            _resolved_commit(git_cmd, ref) for ref in (f"refs/heads/{branch}", f"origin/{branch}")) else "branch_missing"
    except Exception:  # health: allow BLE001 -- metrics only: the exit it names must not change
        return "checkout_move_failed"


def _apply_parked_branch_guard(
    git_cmd, branch, current_branch, *, switch_branch, _windows_gateway_resume
) -> tuple[bool, bool, str | None]:
    """Decide how a checkout parked on another branch is brought to *branch* (stash-switch-pull-
    switch-back used to "update" main while the running code stayed behind).

    By branch contents + updates.parked_branch_strategy: fully merged -> switch back;
    unmerged -> "switch" (default; loud "kept" notice) or "update_in_place" (merge origin/<target>
    INTO the branch, checkout never moves; --switch-branch overrides once); dirty/unverifiable ->
    touch nothing, warn, ``sys.exit(1)`` with the code update SKIPPED (also when the target is
    missing). Returns ``(parked_branch_switched, in_place_update, switch_block_reason)``.
    """
    from hermes_cli.update_cmd import (
        _best_effort, _branch_head_suffix, _git_run, _m, _record_stop, _updates_config, sys,
    )
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
        _record_stop("parked_branch_blocked")
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
        _record_stop("branch_missing")
        sys.exit(1)
    print(
        f"  ℹ On branch '{current_branch}' — updating it in place from "
        f"origin/{branch} (no branch switch; local commits preserved).")
    return False, True, switch_block_reason


def _prepare_checkout_for_update(
    git_cmd, branch, current_branch, *, is_fork, assume_yes, gateway_mode, gw_input_fn,
    switch_branch, target_ref=None, _windows_gateway_resume):
    """Parked-branch guard, land on the target, stash, count new commits. Exits when the
    checkout is unsafe to move or the target is missing. ``commit_count`` is 0 when up to
    date, -1 when tips differ but the shallow count is unrecoverable."""
    from hermes_cli.update_cmd import (
        UpstreamTargetBroken, _CheckoutPlan, _apply_parked_branch_guard, _capture_head_sha, _checkout_move,
        _clear_pending_autostash, _count_commits_between, _exit_after_failed_branch_switch, _git_run,
        _is_shallow_checkout, _m, _moves_for, _park_detached_head, _posix_pause, _resolved_commit,
        _roll_back_broken_update, _switch_branch_at_commit_point, _target_would_move_head, _tip_shas,
        _upstream_broken_header, sys,
    )
    if target_ref is None:
        target_ref = f"origin/{branch}"
    release_tag = target_ref != f"origin/{branch}"
    if release_tag:
        # A release lands detached at its exact commit, never merges into or
        # rewrites the user's branch. Branch-policy machinery is main-only.
        parked_branch_switched, in_place_update, switch_block_reason = False, True, None
    else:
        parked_branch_switched, in_place_update, switch_block_reason = _apply_parked_branch_guard(
            git_cmd, branch, current_branch, switch_branch=switch_branch,
            _windows_gateway_resume=_windows_gateway_resume)

    if not release_tag and not in_place_update and current_branch == "HEAD" != branch:
        print(f"  ⚠ Currently on detached HEAD — switching to {branch} for update...")
        # Before the stash: its refs/stash would contain HEAD until it is dropped.
        _park_detached_head(git_cmd, _m().PROJECT_ROOT, branch)
    # A local terminal answers "keep local changes?" before anything moves (declining discards,
    # so nothing is stashed); a no-op update never asks. Forks stay unscoped: the upstream sync
    # lands on a ref fetched only later.
    prompt_for_restore = not assume_yes and (gateway_mode or (sys.stdin.isatty() and sys.stdout.isatty()))
    ask_first = prompt_for_restore and not gateway_mode and (
        (not release_tag and not in_place_update and current_branch != branch)
        or _target_would_move_head(git_cmd, _m().PROJECT_ROOT, target_ref))
    auto_stash_ref = _m()._stash_local_changes_if_needed(
        git_cmd, _m().PROJECT_ROOT, checkout_move=_moves_for(_windows_gateway_resume), ask_first=ask_first,
        untracked_targets=() if is_fork else (target_ref, f"origin/{branch}", f"refs/heads/{branch}"))
    moved_from_sha = None
    rollback_branch = None
    if not release_tag and not in_place_update and current_branch != branch:
        # Both checkout forms can land on the fetched tip before the count runs.
        # Keep the running code AND its branch identity for syntax rollback.
        moved_from_sha = _capture_head_sha(git_cmd, _m().PROJECT_ROOT)
        rollback_branch = current_branch
        # The switch (or its -B fallback) is this update's first tree write: bind both commits first,
        # on the POSIX pause's baseline (its refusal, sticky, reaches the switch's arm_commit_point).
        _posix_pause.pause_at_commit_point()
        with _checkout_move(_windows_gateway_resume, *(_resolved_commit(git_cmd, ref) for ref in (
                f"refs/heads/{branch}", f"origin/{branch}"))):
            track_result = _switch_branch_at_commit_point(
                git_cmd, branch, target_ref, pre=moved_from_sha, stash=auto_stash_ref)
        if track_result.returncode != 0:
            _exit_after_failed_branch_switch(
                git_cmd, branch, track_result, auto_stash_ref, gw_input_fn, _windows_gateway_resume)

    prompt_for_restore = auto_stash_ref is not None and prompt_for_restore and not ask_first

    if release_tag:
        # An ancestor release still needs applying when switching channels.
        head_sha = _capture_head_sha(git_cmd, _m().PROJECT_ROOT)
        return _CheckoutPlan(
            auto_stash_ref=auto_stash_ref, commit_count=0 if head_sha == target_ref else -1,
            in_place_update=True, parked_branch_switched=False,
            prompt_for_restore=prompt_for_restore, switch_block_reason=None, upstream_checked=True)

    # On shallow checkouts `rev-list --count` can report the entire remote ancestry. The
    # zero/nonzero gate is still sound; treat the shallow NUMBER as unknown and recover it
    # via the GitHub compare API when possible.
    base = moved_from_sha or "HEAD"
    result = _git_run(git_cmd, ["rev-list", f"{base}..{target_ref}", "--count"], check=True)
    commit_count = int(result.stdout.strip())

    apply_is_shallow = _is_shallow_checkout(git_cmd)
    if commit_count > 0 and apply_is_shallow:
        from hermes_cli.source_check import _github_compare_behind
        counted = _github_compare_behind(*_tip_shas(git_cmd, target_ref, base))
        # counted == 0 means local-ahead: falls through to the up-to-date path.
        commit_count = counted if counted is not None else -1

    # Switching away from a locally-ahead commit can change the running tree
    # without adding ancestors. It still needs the post-update pipeline.
    switched_without_new_commits = False
    if commit_count == 0 and moved_from_sha:
        landed_sha = _capture_head_sha(git_cmd, _m().PROJECT_ROOT)
        if landed_sha and landed_sha != moved_from_sha:
            commit_count = -1
            switched_without_new_commits = True

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
    if commit_count == 0 and is_fork and branch == "main" and not release_tag:
        pre_sync_sha = _capture_head_sha(git_cmd, _m().PROJECT_ROOT)
        try:
            upstream_checked = _m()._sync_with_upstream_if_needed(
                git_cmd, _m().PROJECT_ROOT, assume_yes=assume_yes, input_fn=gw_input_fn,
                checkout_move=_moves_for(_windows_gateway_resume))
        except UpstreamTargetBroken as broken:  # nothing moved; local edits stay parked, like a rollback
            if auto_stash_ref is not None:
                print(f"  ℹ️  Local changes preserved in stash (ref: {auto_stash_ref})")
                print("  Restore manually with: git stash apply")
                _clear_pending_autostash()
            _roll_back_broken_update(git_cmd, pre_sync_sha, broken.path, broken.error,
                                     header=_upstream_broken_header(broken),
                                     _windows_gateway_resume=_windows_gateway_resume,
                                     stop_class="target_syntax_error")
        post_sync_sha = _capture_head_sha(git_cmd, _m().PROJECT_ROOT)
        if pre_sync_sha and post_sync_sha and pre_sync_sha != post_sync_sha:
            synced_count = _count_commits_between(
                git_cmd, _m().PROJECT_ROOT, pre_sync_sha, post_sync_sha)
            # HEAD moving is proof of an update even if the count can't be read.
            commit_count = max(1, synced_count)
            moved_from_sha = moved_from_sha or pre_sync_sha

    return _CheckoutPlan(
        auto_stash_ref=auto_stash_ref, commit_count=commit_count, in_place_update=in_place_update,
        parked_branch_switched=parked_branch_switched, prompt_for_restore=prompt_for_restore,
        switch_block_reason=switch_block_reason, upstream_checked=upstream_checked,
        pre_sync_sha=moved_from_sha, rollback_branch=rollback_branch,
        switched_without_new_commits=switched_without_new_commits)
