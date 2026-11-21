# Cold-cache proactive prune

Fork patch identity: `cold-cache-prune`

Backport upstream cold-cache proactive tool-result pruning and the warm-off default without changing the fork's release baseline. Preserve the existing SessionDB archive and rewrite fences. Retire the backport after the accepted release contains equivalent behavior; keep the fork's local request telemetry and role policy until their owners can replace them.

Verify with `scripts/run_tests.sh tests/agent/test_proactive_prune_cold_cache.py tests/agent/test_request_history_rewrite_telemetry.py`. No live settings are needed to exercise either path.
