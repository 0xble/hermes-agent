# Historical upgrade compatibility

Patch identity: `nightly-upgrade-e2e`.

The release-to-fork updater begins its takeover on the historical interpreter,
not the new pinned Python. PM archive provisioning must retain containment on
unpatched Python 3.10/3.11 stdlibs without PEP 706. The shared extractor keeps
its modern data filter and validates legacy members lazily, after preceding
links have been placed; never substitute unfiltered extraction to make an
upgrade pass. The dependency remains until the supported upgrade floor no
longer includes those interpreters.

A pre-handoff updater also leaves root modules cached while purging packages.
Backup is a first post-purge import and its state/agent graph reaches new
`utils.file_signature` before the existing config bridge. Its import must run
the same stale-root bridge before importing that graph. The historical
v2026.9.14 graph-swap test, plus a fresh-process backup-first regression,
qualifies this seam independently of import order in the unit suite.

Source surfaces: `pm/store.py`, `hermes_cli/backup.py`,
`hermes_cli/gitlock.py`, `hermes_cli/_git_process_scope.py`.
Guard surfaces: `tests/pm/test_legacy_tar_bootstrap.py`,
`tests/pm/test_archive_safety.py`,
`tests/hermes_cli/test_stale_root_module_bridge.py`, and
`tests/e2e/core/upgrade/test_fresh_process_entrypoints.py`.

Contribution decision: narrow fork repair for the nightly upgrade qualifier;
no upstream publication is authorized here. Upstream disposition: not adopted
in the accepted baseline. Fork delivery: draft PR, not runtime activation.
Retire each bridge only when equivalent upstream protection or a deliberately
raised historical upgrade floor makes its guard redundant. Read
[install and scratch GC](install-and-scratch-gc.md) when changing takeover PM
runtime lock/owner identity; the PM dependency project is not the checkout
whose dependency state recovery and collection fence.

## Linux Git recovery and historical qualification

The failed index-lock update and intermittent dead temp-pack retention in the
branch qualifier share a recovery veto: a host-wide `pgrep -x git` also sees
unrelated E2E installs and smart-HTTP transfers. The PM checkout identity does
not own this Git sweep. Linux recovery now binds observed process ownership to
repository metadata (including shared worktree metadata); inaccessible or
redirected ownership still vetoes deletion. The updater's custody configuration
(disabled detached maintenance, fsmonitor, credential helpers and hooks) does
not redirect Git resource ownership. Recognize those exact key/value pairs in
argv and inherited `GIT_CONFIG_PARAMETERS`, then compare repositories normally.
Any other configuration remains a conservative veto. A live owner of the target
repository still blocks cleanup, and unresolvable symlink loops veto rather than
raise. Non-Linux probes are unchanged.
The live-writer veto, age floors and exact debris prefixes must survive any
adoption or refactor. Unknown ownership remains a global veto for **locks**:
a lock writer can close its descriptor while waiting in an editor. The specific
v-d6b regression was an HTTP backend child `git upload-pack --stateless-rpc .`:
`enter_repo` already changed its cwd to the served Git directory, so its relative
operand blocked removal of unrelated, hour-old `tmp_pack_*` after a successful
update. Neither that operand nor relative `-C` reconstructs its launch cwd.

Temp packs have a narrower exception for those launch-relative shapes and
`clone`: retain provable current/shared repository owners and explicit resource
paths, then inspect **every** process's descriptors and mappings. Device/inode
comparison protects hardlinks and closed-fd mmap holders; a pack-directory
handle protects pending outputs. Aged unheld packs may then be swept even beside
unplaced unrelated Git. Unknown configuration/output forms, rejected metadata
(including malformed `HEAD`), unreadable fd/maps of our own uid's processes or
of any Git, an unnamed process that could write the pack directory, and
unavailable `/proc` still veto. Another user's non-Git process (root's PID 1, a
daemon) is never readable by a non-root sweeper and cannot create or promote
packs; it does not block the sweep. A disappearing process/fd is not a live
holder.
Young files retain the unchanged 600-second floor. Normal Git transfer temps
stay open until promotion; visible repository owners retain packs even with
closed handles. Non-Linux retains the conservative fallback. Do not reuse
`_held_open_proc`'s fd-only mode as negative proof: it ignores fd errors and
misses mmap holders.

Guard surfaces are
`tests/hermes_cli/test_gitlock_process_scope.py`, `test_gitlock.py`,
`test_gitlock_tmp_packs.py`, and the foreign-state/truthful-outcome Linux cells.
Retire the Linux bridge when equivalent repository-scoped safety is adopted.

N-1's unfiltered pre-swap fetch is immutable from the target branch. The existing
#123254 byte-overfetch expected failure also has a slow-runner manifestation:
it hits the old 300-second cap before the byte-bound assertion. The shallow
qualifier gates that manifestation only with absent pre-fetch healing, an
unchanged historical checkout, shallow non-promisor shape, fault-free HTTP-200
transfers and a measured overfetch. Transport/authentication failures and
current-code timeouts remain hard failures. This is qualification of an existing
historical gap, not a claim that the old release now updates successfully.
`tests/hermes_cli/test_upgrade_shallow_qualifier.py` guards the classification.
Remove both expected-failure forms once N-1 carries the pre-fetch repair; do
not extend them to HEAD behavior or raise fetch deadlines to hide overfetch.

Shallow-install qualification reuses the original depth1/checked/prefetched
builders in `_shallow_install.py`, but each world has a runner-managed file
(`test_shallow_install.py`, `test_shallow_install_checked.py`, and
`test_shallow_install_prefetched.py`). The depth1 file still shares one world
between its two assertions. This removes the three-world module-fixture join
that consumed the entire file budget before any assertion ran. Historical
pinned N-1 installation, full update sequence, byte bounds and strict rejection
are unchanged; the upgrade directory's autodiscovery grants each file the
existing finite 900-second bound. Phase messages and one 840-second traceback
stay on the runner pipe before its process-tree kill. Local extraction,
collection and strict-guard tests do not prove Linux historical acceptance;
the hosted Linux lane must measure the three files and truthful-outcome cell.
Stack fit: Python stdlib, pytest and `scripts/run_tests.sh`; no dependencies or
services added.
