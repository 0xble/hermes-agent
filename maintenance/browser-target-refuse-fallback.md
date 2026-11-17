# Browser target fail-closed selection

Fork patch identities: `browser-target-refuse-fallback`, `task-external-cdp-attachment`.

The active profile may pin `browser.target` and set `browser.fallback: refuse`.
Every browser lane must use that endpoint or return a structured
`browser_target_unavailable` error; it must never silently select Camofox, a
cloud provider, local Chromium, or another CDP endpoint. The legacy
`task-external-cdp-attachment` identity on the introducing commit is retained as
an ownership alias; subsequent commits use the browser-target-specific identity.

The target is validated as an absolute HTTP(S)/WS(S) endpoint. Unix sockets are
not guessed or selected implicitly. Camofox remains available only when it is
the explicitly pinned target, and failures in target-policy lookup must not
turn into an alternative backend selection.

Contract tests: `tests/tools/test_browser_target_refuse.py`.

## Verification

Run:

```sh
HERMES_HOME="$PWD/.ci/pr1339-home" env -u __HERMES_ACTIVATED \
  python -m pytest tests/tools/test_browser_target_refuse.py -q
```

The focused tests must prove that refused targets win over cloud, local and
operator-CDP selection, invalid targets produce structured errors, and the
legacy allow policy still selects local backends.

Retire when upstream preserves the complete fail-closed target and fallback
contract. Roll back by reverting commits carrying the identities above; do not
change the target to a fallback as a recovery step.
