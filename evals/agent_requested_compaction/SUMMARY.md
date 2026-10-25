# S2 Model-Behavior Eval: `compact_context`

## Result

Primary model `gpt-6.1-sol` via `custom:codex-proxy`, final description, 5 runs per scenario (`results/rev4-primary.jsonl`):

| Scenario | Expected | Called |
|---|---|---:|
| Finished sub-task | call | 4/5 |
| Unrelated topic switch | call | 4/5 |
| Long stretch past half threshold | unscored (trigger dropped) | 2/5 |
| Small context | no call | 2/5, all refused by the 30% floor |
| Mid-debug | no call | 0/5 |
| Just compacted | no call | 0/5 |

**The original bar was not met.** It required every should-call scenario to call in every run and no should-not scenario to ever call. Description wording alone could not reach it after 4 revisions. The bar used to ship is weaker: no should-not scenario produced an accepted compaction (0 of 15), and the 2 phase-boundary triggers fired in 8 of 10 runs.

The 0 of 15 depends partly on code. The model called the tool in 2 of 5 small-context runs, and the 30% floor refused both (about 36K tokens against a 61,200 token floor). A missed call falls back to today's behavior, because automatic compression still runs at the threshold.

Limits: 5 runs on 1 model. Summary quality and prompt-cache cost are not measured here.

`claude-opus-5-5` was not evaluated on the final description: its proxy returned `HTTP 503 auth_unavailable` (no Anthropic credit).

## Description History

1. Original: 3 triggers. Finished sub-task 3/3, topic switch 0/3, long stretch 3/3 (first-response harness, later found to under-measure).
2. Revision 1 ("any one of these", "do not wait for a token percentage"): topic switch 0/3 with a whole-turn harness. The model went straight to research.
3. Revision 2 ("check before any other tool"): finished 3/3, topic 1/3, long stretch 2/3, mid-debug 1/3 false positive.
4. Revision 3 (dropped the size trigger and added "never while on the same unfinished task"): every scenario 0/3. Over-corrected.
5. Final (revision 2 wording with the size trigger removed): the table above.

## Criterion 3 Visibility

Nothing in the prompt surfaces context usage. `compact_context` reports `current_context_tokens` and `auto_compress_threshold_tokens` only after a call, so a model can judge criterion 3 only from an earlier tool result. That is why the context-size trigger was dropped: size stays with automatic compression.

## Rerun

```bash
.venv/bin/python evals/agent_requested_compaction/run_eval.py \
  --model primary --scenario all --runs 5 \
  --output evals/agent_requested_compaction/results/live.jsonl
```

Use `--model attended` for Claude once its provider route has credit. The runner resolves provider keys through Hermes config and never prints or writes them.
