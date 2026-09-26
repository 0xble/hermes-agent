# Cron restart survival on macOS

Patch identity: `cron-macos-detached`.

## Contract

Under a launchd-managed macOS gateway, hand each cron execution to the existing external worker in a new session, rather than running it in the gateway process. The worker adopts the durable execution claim, owns its (PID, process-start fingerprint) liveness, completes the ledger, and queues delivery for the replacement gateway. Honor `cron.require_restart_safe_scope` without requiring systemd on macOS. Foreground/desktop invocations and Linux systemd dispatch retain their current behavior.

This is a core scheduler/dispatch invariant; a plugin or skill cannot atomically own cron's claim, worker handoff and recovery. Revert this unit's dispatch selection and regression when an upstream release proves the same launchd restart-survival contract. The Linux transient-scope worker and delivery queue are upstream-owned infrastructure reused here.

## Proof and limitation

Run `tests/cron/test_restart_safe_worker.py` on macOS, including its real-process parent-process-group termination test, then `./bin/ci preflight` and the exact-SHA `gate`. The E2E uses a disposable profile, script and fake `ai.hermes-test.*` identity; never touch the live gateway job.

Until immutable per-version release directories (S2), an external worker surviving an in-place `hermes update` can lazily import modules from the new checkout after loading old ones. S1 is process survival, not an immutable code snapshot.

Related upstream PRs [#123893](https://github.com/NousResearch/hermes-agent/pull/123893) and [#123878](https://github.com/NousResearch/hermes-agent/pull/123878) concern restart identity and drain waiting; neither isolates a macOS cron worker. Roll back by reverting this patch's launchd dispatch branch and associated docs/tests, without removing the upstream Linux handoff or execution ledger.
