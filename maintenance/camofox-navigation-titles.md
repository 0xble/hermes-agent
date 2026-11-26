# Camofox navigation titles

Load this unit when changing Camofox navigation results, tab listing, or account-bound lookup.

## Required behavior

When a successful Camofox navigation omits its page title, read `/tabs` using only the task session's owned `userId` and match its exact `tabId`. Preserve a non-empty mutation title without an extra lookup. An unavailable/malformed title lookup leaves successful navigation intact with an empty title. Do not inspect another account's tabs or expose the raw identity in model-visible results. Named accounts (`brianle`, `lpg`, `meridian`) and existing tab ownership remain unchanged.

## Provenance and patches

- Fork patch identity: `camofox-navigation-titles`.
- Archived HERMES-132, `fix(browser): return Camofox navigation titles`, documented at `browser-identities.md:144-151`. The original upstream contribution was commit `9e383117567f54e6a78ae88d25240cf2bfba4fcc`; rebased onto upstream main as `4017053e559a3cc1540cca41a5b82ababaf5f178`.
- Upstream contribution: [NousResearch/hermes-agent#106978](https://github.com/NousResearch/hermes-agent/pull/106978), still open. On upstream main `d0288be5b3330d2442e3907185b8e9d0958297bb`, the mutation response alone determines the title. This fork adapts the same read-only lookup to its named-account `_navigate_tab(..., account)` path, using the session's owned user ID rather than alias/global state.
- No exact competing upstream title fix was found in issue/PR search. The independent approach is the existing Camofox tab-list API, not a new browser engine or core tool; no private browser endpoint or account alias is included in the upstream patch.

## Verification

`scripts/run_tests.sh -j 6 tests/tools/test_browser_camofox.py tests/tools/test_browser_camofox_accounts.py tests/tools/test_browser_camofox_ensure_tab.py` exercises the missing mutation title and account isolation. Run Ruff on changed Python files, `git diff --check`, and `scripts/check_fork_patches.py --source-only --repo .`.

## Retirement and rollback

Retire the fork delta only after a selected upstream *release* includes the full contract and named-account adaptation remains covered. Revert this patch and its tests/maintenance entry to roll back; no Camofox service, credential, state, or profile migration occurs.
