# Goal-blocker semantic probe

Run from the repository root with the configured auxiliary judge credentials:

```sh
python evals/goal_blockers/semantic_probe.py --out /tmp/goal-blockers.json
```

This is an opt-in live-model evaluation, not part of deterministic CI. It calls the real `judge_goal` entry point and records the resolved route, verdict, diagnostic directive, transport/parse failures, and per-case result. Inputs and tool-evidence examples are explicitly synthetic; the probe performs no real publishing, signing, billing, or package operations. It does not alter goal state or runtime configuration.

Cases cover partial blockers (including an agent incorrectly declaring itself blocked), reasonable recovery work, premature impossibility, independent work during a cooldown, genuine external authorization, evidence-backed inability, a running dependency, and source-backed versus unsupported completion. Blocked results must include evidence and a resumption condition. An inability may conservatively be classified as an external dependency instead of `unachievable_as_stated`; the rare impossibility label is allowed, not required.

The retained deterministic tests exercise the model-response parser through real `GoalManager` persistence in isolated SQLite, reload/resume/user-stop protection, malformed legacy diagnostics, and both Kanban handoff gates and the worker loop. Only the external model response is controlled in those tests. A live probe pass is evidence for these examples, not a guarantee that the model can identify every authorized next step or verify the truth of arbitrary supplied evidence.

## Candidate evidence

- Original seven-case verdict-only baseline: 7/7 on the existing configured `openai-codex` / `gpt-6-astra` judge. Explicit cases did not reproduce the user's false block; persistence and presentation regressions did fail before implementation.
- First structured candidate probe: 6/7 under an overly strict oracle demanding the impossibility label. The model conservatively returned an external dependency with evidence and a resume condition. The oracle was corrected to the approved rare/optional label semantics, not by changing the expected blocked verdict.
- Expanded ten-case candidate: 10/10, including three less-cooperative cases that do not volunteer the correct next action. No transport/parse failures. Run artifacts remain outside the committed source.
