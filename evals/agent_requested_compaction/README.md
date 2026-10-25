# `compact_context` S2 Behavior Eval

This eval measures whether real models call Hermes's agent-requested `compact_context` tool at the intended phase boundaries. It uses the production `AIAgent` loop, production system prompt assembly, and production tool definitions. The local configured proxy is used for both models.

## Scenarios

- `finished_subtask`, should call: a large completed sub-task whose raw output is no longer needed verbatim.
- `topic_switch`, should call: a large session changes to an unrelated topic.
- `long_stretch`, should call: a long multi-step phase is about to begin and the transcript contains a real prior context-usage measurement above half the threshold.
- `small_context`, should not call.
- `mid_debug`, should not call because exact recent output still matters.
- `just_compacted`, should not call because compaction just completed.

Should-call scenarios are a finished sub-task and an unrelated topic switch. Should-not scenarios are a small context, mid-debug, and just compacted. The long-stretch scenario is recorded but unscored because the context-size trigger was dropped. A pass requires no accepted compaction in any should-not run (the 30% code floor refuses small contexts) and calls in at least 80% of should-call runs. See `SUMMARY.md` for why.

## Run

From the repository root:

```bash
HERMES_PYTHON=.venv/bin/python .venv/bin/python \
  evals/agent_requested_compaction/run_eval.py \
  --model primary --scenario all --runs 5 \
  --output evals/agent_requested_compaction/results/live.jsonl
```

The provider key is resolved through Hermes's configured `key_env` using Hermes's dotenv-aware resolver. The runner never prints or writes the key. Raw JSONL results are ignored by Git. `SUMMARY.md` contains the committed result of the completed run.
