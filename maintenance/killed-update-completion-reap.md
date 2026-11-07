# Killed-update completion reap

Load when changing POSIX update completion spawn, the checkout lock it inherits,
or the upgrade e2e sandbox's host-gateway isolation.

## Required behavior

Fork patch identity: `killed-update-completion-reap`.

A SIGKILL of `hermes update` (power cut, closed lid) must not leave the
new-session completion child holding the checkout lock. Windows already puts
that child in a kill-on-close job. POSIX `start_new_session` does not, so the
orphan used to make the next launch refuse with "an update is still running"
and leave the gateway the update had paused draining. A parent-death watch
kills the child's session when the updater dies. A normal exit dismisses the
watch and does not kill a child the parent is already reaping.

Upgrade e2e sandboxes keep a private `HERMES_GATEWAY_LOCK_DIR` under the
sandbox root. The host-gateway record is cross-profile, not keyed by
`HERMES_HOME`; its fallback is `$XDG_STATE_HOME` or `$HOME/.local/state`.
An explicit lock namespace prevents inherited defaults or sibling gateways
from claiming the sandbox's `default` profile. Both the home and rendezvous
namespace are private to the sandbox.

Upstream has no equivalent parent-death fence on the POSIX completion child
(the Windows job is the intended contract). The private lock dir is test
isolation, not a product change.

## Verification and retirement

Contract tests: `tests/hermes_cli/test_update_completion_death_watch.py`
and `tests/e2e/core/upgrade/test_sandbox_isolation.py`.

Retire the reap when POSIX completion children die with a killed updater the
way the Windows job already does. Retire the lock-dir pin when upgrade e2e no
longer starts a real gateway.
