# Stdio wrapper chain stays bounded

**Patch identity:** `stdio-wrapper-chain`.

Load this unit when changing `agent/process_bootstrap.py::_SafeWriter` or `_install_safe_stdio`,
`agent/thread_scoped_output.py`, or any code that rebinds `sys.stdout`/`sys.stderr` for the life of
the process.

## Required behavior

- Hermes stdio wrappers expose `_hermes_stdio_next` and resolve attributes with
  `delegate_getattr`, which walks the chain iteratively with a seen set. A deep or cyclic chain
  raises `AttributeError`, never `RecursionError`.
- Both installers collapse the current chain through `adopt_routing_proxy`: a routing proxy found
  anywhere in it goes back on top unwrapped (it guards its own writes), with its passthrough
  rebound to the resolved real stream. Without a proxy, `_install_safe_stdio` leaves exactly one
  `_SafeWriter` over the real stream, falling back to `sys.__stdout__`/`__stderr__` when the chain
  never reaches one. An existing long or cyclic chain is repaired, not just kept from growing.
- New proxies and `_SafeWriter`s bind the resolved real stream, never another wrapper. A proxy
  resolves its chain through its thread-independent passthrough, never a silenced thread's sink.
- Both installers hold `thread_scoped_output.stdio_install_lock`.

## 2026-10-05 Incident

Gateway pid 22320 on runtime `d0251975` started at 11:17 PDT. From 15:40 to 15:58 PDT,
`gateway.error.log` recorded 20 `RecursionError`s and 7 failed gateway turns ("Agent error in
session …", which users saw as "Something went wrong… Use /retry"). Every one alternated
`thread_scoped_output.py:87 __getattr__` and `process_bootstrap.py:167 __getattr__` below
`hermes_logging._line_buffer_piped_stdout`, which probes `sys.stdout.line_buffering` on every agent
build. Subagent builds (`delegate_task`) and `review_candidate` failed the same way. None occurred
after the 16:04 restart.

The chain was long, not cyclic. `_install_safe_stdio` runs on every agent build and every turn. It
wrapped the routing proxy in a `_SafeWriter`, because it only skipped `_SafeWriter`. The next
`thread_scoped_silence()` (background review, `execute_code` RPC) no longer saw a proxy on top, so
it installed a new proxy whose passthrough was that `_SafeWriter`. Each build-plus-silence cycle
added two layers. A missing attribute such as `line_buffering` (the routing proxy's `__getattr__`)
recursed through every layer, and at about 500 cycles it crossed the 1000-frame limit. Writes
failed silently before that, because `_forward` swallows the `RecursionError`.

Upstream [#120329](https://github.com/NousResearch/hermes-agent/pull/120329) (open, unreviewed)
stops `_install_safe_stdio` from wrapping the proxy. It does not bound `__getattr__`, repair an
existing long or cyclic chain, or lock the two installers against each other.

**Regression:** `scripts/run_tests.sh tests/agent/test_thread_scoped_output.py
tests/agent/test_run_agent.py -k "thread_scoped or SafeWriter or cycle"`. It covers:

- 1000 interleaved agent-build and silence cycles keeping the chain at two layers or fewer, with
  `line_buffering` still resolving and output still reaching the real stream;
- an existing alternating chain deeper than the recursion limit collapsing to one layer on the
  next agent build or silence install, with output arriving;
- a constructed `_ThreadRoutingStream` ↔ `_SafeWriter` cycle raising `AttributeError` and being
  unwrapped to the real stream by the next install;
- a real `AIAgent` build and `run_conversation` turn succeeding with that cycle installed as
  `sys.stdout`.

All of them fail on the base with `RecursionError`.

**Retire when:** upstream ships a release where repeated `_install_safe_stdio` plus
`thread_scoped_silence` keeps the chain bounded and stdio `__getattr__` cannot recurse unboundedly.
Run the regression against that release without this patch.

**Rollback:** revert this unit's commit. It touches only the two stdio modules, their tests, and
this record.
