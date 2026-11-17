# Typed session/relay tool-choice eval

This eval uses a fresh temporary `HERMES_HOME` and a seeded fixture database for every
trial. The fixture data contains a PartyDAO Tokens owner session, an Everguide Review
Slack thread, and a relay receipt. The scoring rule observes the model's first state
lookup and fails if any recorded tool call contains `sqlite3`, `state.db`, SQL over the
sessions table, or if the agent gives up.

The tool fixtures are registered in-process, so no live Hermes state, relay ledger, or
installed config is written. `--tree` is the Hermes release/worktree under test; it must
be an installed release or a complete Hermes source tree (not the agents worktree).

## Run

Run one baseline case through the current release:

```sh
~/.local/bin/heavy-slot --label tool-choice-eval -- \
  ~/.hermes/current/.venv/bin/python evals/tool_choice_session_relay/eval.py \
  --tree ~/.hermes/current --variant baseline --model gpt-6.1-sol \
  --case topic_name --trials 1 --out /path/to/baseline.json
```

Run the fixed descriptions for four-of-five trial evidence:

```sh
~/.local/bin/heavy-slot --label tool-choice-eval -- \
  ~/.hermes/current/.venv/bin/python evals/tool_choice_session_relay/eval.py \
  --tree /path/to/hermes-agent --variant fixed --model gpt-6.1-sol \
  --trials 5 --out /path/to/fixed.json
```

Omit `--case` to run all five scenario families and their two paraphrases. The JSON
report contains per-prompt traces, per-family pass counts, first typed lookup, and the
raw-state-db failure flag. Run the unit checks from the Hermes worktree with:

```sh
python -m pytest evals/tool_choice_session_relay/test_eval.py -q
```

The `baseline` variant intentionally uses short descriptions; `fixed` uses the
production descriptions being tested. This keeps the eval from hard-coding prompt
phrasing while making the description intervention explicit.
