# Local kernel memory ceiling

- **Patch identity:** `kernel-memory-ceiling`.
- **Behavior:** Local persistent code-execution kernels default to an 8192 MiB
  process-tree ceiling (`code_execution.kernel_memory_limit_mb`). Positive integer
  overrides, including integer-valued floats, are supported; invalid/nonpositive
  values retain the safety default and log a warning.
  A host-side watchdog in the existing cell wait loop samples once per second and
  before accepting the reply. It sums each live process's RSS, or the larger of RSS
  and Darwin `proc_pid_rusage(RUSAGE_INFO_V0).ri_phys_footprint` on macOS, so
  compressed memory is not hidden by a small resident set. Over-budget cells return
  an explicit error and state loss, reuse registry discard/tree teardown, and log
  `kernel_lifecycle` retirement with `reason=memory_ceiling`, the measured tree
  footprint, and the ceiling. Descendants are discovered before the death pipe is
  closed (accepting the kill-before-pipe-close shutdown latency). Unreadable
  descendants are skipped individually, preserving all readable samples. Linux
  children install only inherited-limit-preserving `RLIMIT_DATA` before user code,
  without unsafe threaded-parent `preexec_fn`. Linux 4.7+ charges private writable
  mappings; no virtual-address cap is installed, preserving Chromium/JVM reservations.
  A cell `MemoryError` with an active rlimit retains its traceback and names the
  effective ceiling, OS allocation refusal, and the DATA backstop. The kernel stays
  alive because the failed allocation was refused and the cell has unwound; prior
  mutations persist.
- **Source surfaces:** `tools/code_kernel.py`, `tools/code_kernel_memory.py`,
  `hermes_cli/config_defaults.py`, the code-execution user guide, and
  `tests/tools/test_code_kernel_memory.py`.
- **Upstream status:** Related issue
  [NousResearch/hermes-agent#136403](https://github.com/NousResearch/hermes-agent/issues/136403)
  and closed, unmerged [PR #142249](https://github.com/NousResearch/hermes-agent/pull/142249)
  propose a Windows Job Object/POSIX address-space cap. This patch independently
  targets the macOS enforcement gap with a host-side tree-footprint watchdog; no
  equivalent macOS process-tree watchdog was found at upstream main
  `273fb853acdabfdcf60be44435051d554f124647`.
- **Focused regression:** `h=$(mktemp -d) && export HERMES_HOME="$h" &&
  python -m pytest tests/tools/test_code_kernel_memory.py tests/tools/test_code_kernel.py
  tests/tools/test_code_kernel_lifetime.py tests/tools/test_code_kernel_remote.py
  tests/tui_gateway/test_session_finalize_reaps_kernels.py -q`.
  The new tests cover actual config loading, equality/below/above-budget behavior,
  retirement logs and recovery, bounded real 256 MiB allocations under a 200 MiB
  ceiling, descendant teardown with the watchdog isolated from Linux rlimits,
  native macOS footprint sampling, and deterministic active/unavailable rlimit
  `MemoryError` reporting and state reuse. Deterministic platform/resource tests
  assert that the runner never installs a virtual-address limit, preserves inherited
  DATA limits, and samples readable descendants after an `AccessDenied` sibling.
  Config tests cover integer-valued floats and warnings for ignored values.
- **Limits/follow-ups:** Sampling is best-effort, not an OS-enforced aggregate
  reservation; bursts within one second can overshoot. Background allocation while
  idle, aggregate pressure across many kernels, remote kernels, standalone fallback,
  and other long-lived children need separate policy/hooks. RSS can double-count
  shared pages. Linux backstops may return `MemoryError` before watchdog retirement;
  raising their spawn-time ceiling requires an explicit kernel reset.
- **Incident:** Fleet Resources task `b20db17f`: a local kernel reached approximately
  94 GB virtual/compressed memory and 57 GB swap on 2026-11-12, and only its five-minute
  timeout retired it; a 47 GB swap spike also occurred the day before.
- **Retirement:** Remove when upstream provides an equivalent default-on configurable
  local process-tree ceiling effective on macOS, explicit state-loss reporting,
  lifecycle evidence, and Linux resource-limit backstops.
- **Rollback:** Revert the commit carrying `Fork-Patch: kernel-memory-ceiling`.
  Raising the positive config value is an operator override, not a full rollback.

Contract tests: `tests/tools/test_code_kernel_memory.py`, `tests/tools/test_code_kernel.py`, `tests/tools/test_code_kernel_lifetime.py`, `tests/tools/test_code_kernel_remote.py`, `tests/tui_gateway/test_session_finalize_reaps_kernels.py`.
