"""Entrypoint drivers for the parity matrix: CLI subprocess entrypoints.

Driver contract (every ``_drive_*.py`` module follows it)::

    def drive(ph: ParityHome, srv: FakeLLMServer, prompt: str) -> DriveResult

* spawn the REAL entrypoint with ``ph.env()`` (hermetic fake HOME, no real
  credentials) — cwd ``ph.project`` unless the surface documents another cwd
  channel (then use that channel and say so in ``DriveResult.cwd_channel``);
* run exactly ONE user turn with ``prompt`` against ``srv`` (the scripted
  responder makes the model call the MCP canary tool, then answer
  ``FINAL_ANSWER``);
* return what the surface delivered to ITS client in ``final_text``;
* stop the entrypoint through its NORMAL shutdown path (exit, stdin EOF,
  SIGTERM, RPC) before returning; never SIGKILL except as a last resort after a
  bounded graceful wait (and then report it via ``graceful_exit=False``).

Finite ``hermes -z`` and ``hermes chat -q -Q`` clients lease their freshly spawned
local gateway. Normal shutdown is client exit alone: the driver verifies the owner
is gone without an operator stop. Explicit gateway stop is failure cleanup only.
"""

from __future__ import annotations

import subprocess

from tests.e2e.core.parity._helpers import (
    TURN_TIMEOUT,
    DriveResult,
    ParityHome,
    hermes_argv,
    stop_profile_gateway,
)
from tests.fakes.fake_llm_provider import FakeLLMServer


def _run_cli(ph: ParityHome, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        hermes_argv(*args), cwd=ph.project, env=ph.env(), capture_output=True, text=True,
        timeout=TURN_TIMEOUT, stdin=subprocess.DEVNULL,
        check=False,
    )


def _client_turn(ph: ParityHome, label: str, *args: str) -> DriveResult:
    from gateway.status import live_gateway_pid_for_home
    from tests.waiting import wait_until

    proc = _run_cli(ph, *args)
    try:
        assert proc.returncode == 0, f"{label} exited {proc.returncode}: {proc.stderr[-2000:]}"
        # Stronger than an operator stop: finite client exit alone must release its
        # private gateway. The matrix separately asserts MCP child/grandchild death.
        wait_until(lambda: live_gateway_pid_for_home(ph.hermes_home) is None, timeout=7,
                   what=f"{label} client-owned gateway to exit")
    except BaseException:
        stop_profile_gateway(ph)  # failure cleanup never counts as graceful client exit
        raise
    return DriveResult(
        final_text=proc.stdout.strip(), toolset="hermes-cli", graceful_exit=True,
        extra={"exit_code": proc.returncode, "stderr_tail": proc.stderr[-2000:], "gateway_stopped": True},
    )


def drive_oneshot(ph: ParityHome, srv: FakeLLMServer, prompt: str) -> DriveResult:
    return _client_turn(ph, "hermes -z", "-z", prompt)


def drive_chat_q(ph: ParityHome, srv: FakeLLMServer, prompt: str) -> DriveResult:
    return _client_turn(ph, "hermes chat -q", "chat", "-q", prompt, "-Q")
