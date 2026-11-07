# CLI Interrupt Repaint Affinity

Load this when changing `HermesCLI._force_full_redraw`, `_redraw_app_now`, `_recover_terminal_after_interrupt`, or `_run_on_app_loop` in `hermes_cli/cli_terminal_mixin.py`.

## Required Behavior and Provenance

Fork patch identity: `cli-interrupt-repaint-affinity`.

Interrupt recovery runs on the input worker thread (`_tui_after_turn` calls `_recover_terminal_after_interrupt`). prompt_toolkit's renderer and output belong to the app's event loop. `_force_full_redraw` reset the renderer and wrote terminal output inline on that worker. A real-loop, real-worker regression on the unmodified baseline recorded 1 renderer reset and 2 output writes off the app loop.

When the caller is not on the running app loop, `_force_full_redraw` queues the unchanged repaint body (`_redraw_app_now`: clear/reset, history replay, pet frame, invalidate) onto it through the existing `_run_on_app_loop`. When the queued repaint runs, it rechecks `_terminal_io_broken` and app identity. Callers on the loop (Ctrl+L, `/redraw`, resize recovery) and callers with no running loop stay synchronous. Flush-before-redraw order and flush-failure independence are unchanged.

No existing unit owns this boundary. [CLI streaming preview framing](cli-stream-preview.md) owns modern-composer status rendering, not repaint thread affinity. Upstream `NousResearch/hermes-agent` main still repaints inline at `hermes_cli/cli_terminal_mixin.py` `_force_full_redraw`, and no matching upstream PR was found. No upstream contribution is claimed.

This fixes the observed thread-affinity defect only. It does not by itself establish the cause of the hosted classic-CLI `/compress` parity failure.

## Verification

Contract tests: `tests/hermes_cli/test_terminal_interrupt_recovery.py::TestInterruptRecoveryThreadAffinity`

Run `scripts/run_tests.sh tests/hermes_cli/test_terminal_interrupt_recovery.py tests/hermes_cli/test_terminal_io_broken.py tests/hermes_cli/test_cli_force_redraw.py tests/hermes_cli/test_cli_pet_pane.py tests/hermes_cli/test_interrupt_output_history_regression.py`. The affinity case drives a real prompt_toolkit Application on a running loop and a real worker, and asserts exactly 1 recovery reset with 0 off-loop renderer or output events.

## Update And Retirement

Retire when upstream marshals interrupt-recovery repaint onto the app loop with equivalent recheck semantics, or when recovery itself moves onto the loop. Revert the commits carrying `Fork-Patch: cli-interrupt-repaint-affinity` to remove it.
