# Delivered steer observer

Identity: `steer-delivery-observer`

## Divergence

Ask Tracking's out-of-tree plugin needs a delivery witness for mid-turn input:
normal turn hooks contain the admitted message, while native inserted steer rows
have no stable delivery identity. Transcript capture is intentionally not a substitute.
Consumer: https://github.com/0xble/agents/pull/1043.

The fork adds `post_steer_delivered`; its public payload and batching behavior
are owned by the developer plugin guide. `agent/steer_delivery.py` owns the
observer and the unchanged requeue helper, extracted to offset the oversized
runtime-helper file's growth.

## Preserve and retire

On upstream adoption, inspect both native insertion sites and per-turn binding.
Keep requeue/fallback silent, observer failures nonfatal, and Python callbacks
bounded by the existing fail-open hook timeout. Missing turn/platform attributes
use empty-string fallbacks. Retire this patch
only when upstream exposes an equivalent insertion-time event and the consumer
can use it without transcript inference. Upstream PR 106072 was closed for an
adapter-owned acceptance hook; that is not equivalent to actual row delivery.

## Proof

Contract tests: `tests/agent/test_steer_delivered_hook.py`

Run `scripts/run_tests.sh tests/agent/test_steer_delivered_hook.py
 tests/agent/test_steer.py tests/agent/test_compression_busy_steer_anchor.py
 tests/agent/test_lock_fallback_base_semantics.py
 tests/hermes_cli/test_plugins.py` (one shell command).
The consumer's native probe additionally checks real plugin dispatch and durable
capture with disposable state; it is not provider-acceptance or live activation proof.
