# Managed Browser Visibility Handoff

## Status: dormant by default

`browser.visibility_handoff` defaults to `false`. Until an operator explicitly enables it in `config.yaml`, `browser_exec` does not advertise or dispatch handoff actions.

```yaml
browser:
  use_real_profile: true
  visibility_handoff: true
```

When enabled, a named identity can request one visibility operation for an **already-running**, headed Hermes-managed browser:

```python
# Show the managed work identity window
browser_exec(code="# reveal managed browser", local=True, identity="work", handoff="reveal")

# Minimize it without changing its tabs or page state
browser_exec(code="# minimize managed browser", local=True, identity="work", handoff="minimize")
```

The caller must supply comment-only `code`, `local=True`, an explicit configured identity, and the same named browser session that Hermes already bound to that identity. Hermes verifies that binding, its managed profile CDP endpoint, and the persisted headed-mode record before it registers activity or sends CDP window commands. An unfinished or timed-out Browser Use execution blocks the operation.

## Contract and limits

- Only `reveal` and `minimize` exist. They use `Browser.setWindowBounds` and read the requested state back.
- The feature refuses ambiguous multiple page-window inventories rather than selecting an arbitrary window.
- It preserves the running browser, tabs, forms, cookies, and in-memory page state. It never calls `Browser.close`.
- It does **not** restart or relaunch a browser, inspect downloads, close tabs, replay browser work, or accept `confirm_restart`.
- It cannot attach to user-owned normal Chrome, operator-provided CDP endpoints, cloud browsers, Camofox, or a session bound to a different identity.
- Existing mode-mismatch refusal remains: a running headless managed browser cannot be revealed, and a headed browser is not silently relaunched in another mode.

Use a disposable profile with `python scripts/verify_browser_handoff.py --live` to verify the complete visibility dispatch. That script creates and terminates only its own temporary Chrome process, verifies the disabled gate, and checks that PID, target, form and JavaScript state survive. It never opens a user profile.

## Concurrency and timeout recovery

With the opt-in enabled, normal calls for one identity wait for its cross-process execution lock, bounded by the requested execution timeout. A visibility request refuses immediately while that lock is busy. Other identities are independent.

If a Browser Use invocation times out, the daemon may still be executing even though the CLI child exited. Ordinary browser work remains available, but visibility stays blocked by the uncertainty marker. A later successful call does not erase the earlier uncertainty. There is deliberately no automatic browser restart or forced reset.

To recover visibility, an operator must first verify the affected managed browser and its Browser Use daemon are stopped, with no concurrent callers. Only then remove that identity's marker at `$HERMES_HOME/browser-use/visibility-handoff/<runtime_key>.json` before a fresh managed launch. Never clear the marker merely because time elapsed, and never close or restart a browser without the required authorization. Leaving the feature disabled is also safe and does not impede ordinary browser work.

Ownership uses `SystemInfo.getProcessInfo` to identify the CDP browser PID, then checks that local process's command line against the managed profile. The feature adds no Chrome launch flags and does not require `--enable-automation` or relaunching an already-running headed browser. OS process-inspection denial causes refusal, not weaker ownership checking.
