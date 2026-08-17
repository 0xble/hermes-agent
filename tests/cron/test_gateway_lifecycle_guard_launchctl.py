"""Regression tests for launchctl verb coverage in the gateway lifecycle guard.

Background: on 2026-08-16 a gateway-side agent session was asked to reconcile
the fork and restart Hermes. Its `git reset --hard` and `hermes gateway
restart` attempts were both correctly blocked, so it fell back to
`launchctl bootout` — which the guard did not recognize. The gateway took
SIGTERM and stayed down until a human re-bootstrapped it by hand.

The verb list had exactly the wrong coverage. `unload`/`stop`/`kickstart`
only signal the job, so launchd's `KeepAlive` respawns the gateway within
`ThrottleInterval`; those were blocked. `bootout`/`disable`/`remove` take the
job out of the domain (or mark it unloadable) so `KeepAlive` has nothing left
to respawn; those were allowed. The unrecoverable verbs must be blocked at
least as strictly as the recoverable ones.
"""

import pytest

from cron.lifecycle_guard import (
    contains_gateway_lifecycle_command_or_referenced_script as contains_lifecycle_cmd,
)


# Verbs whose effect launchd's KeepAlive cannot undo on its own.
UNRECOVERABLE_VERB_COMMANDS = [
    "launchctl bootout gui/501/ai.hermes.gateway",
    "launchctl bootout system/ai.hermes.gateway",
    "launchctl bootout gui/501 ~/Library/LaunchAgents/ai.hermes.gateway.plist",
    "launchctl disable gui/501/ai.hermes.gateway",
    "launchctl remove ai.hermes.gateway",
]

# Verbs that merely signal the job; KeepAlive respawns it. Already covered,
# asserted here so the two groups can never drift apart again.
RECOVERABLE_VERB_COMMANDS = [
    "launchctl unload ai.hermes.gateway",
    "launchctl stop ai.hermes.gateway",
    "launchctl kickstart -k gui/501/ai.hermes.gateway",
]


@pytest.mark.parametrize("command", UNRECOVERABLE_VERB_COMMANDS)
def test_unrecoverable_launchctl_verbs_blocked(command):
    """bootout/disable/remove strand the gateway with no supervisor left."""
    assert contains_lifecycle_cmd(command) is True, command


@pytest.mark.parametrize("command", RECOVERABLE_VERB_COMMANDS)
def test_recoverable_launchctl_verbs_still_blocked(command):
    assert contains_lifecycle_cmd(command) is True, command


@pytest.mark.parametrize(
    "command",
    [
        # The gateway identifier anchor must still scope the guard: other
        # Hermes-adjacent launchd jobs are legitimately managed from a
        # gateway session.
        "launchctl bootout gui/501/ai.hermes.update-checker",
        "launchctl bootout gui/501/com.brianle.hindsight-hermes",
        "launchctl disable gui/501/com.example.unrelated",
        # Read-only inspection is diagnostics, not lifecycle control.
        "launchctl list | grep hermes",
        "launchctl print gui/501/ai.hermes.gateway",
    ],
)
def test_unrelated_or_readonly_launchctl_not_blocked(command):
    assert contains_lifecycle_cmd(command) is False, command


def test_hermes_gateway_label_spelling_also_blocked():
    """Both `ai.hermes.gateway` and `hermes-gateway` label shapes are real."""
    assert contains_lifecycle_cmd("launchctl bootout gui/501/hermes-gateway") is True
