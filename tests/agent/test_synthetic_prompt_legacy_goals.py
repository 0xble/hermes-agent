"""Replay persisted pre-additive-only prompts without runtime provenance.

Frozen formatter wording copied from 022f4fbfcdbd9dc62c0667a7bdcd0e2fb6170ccb,
pre-#531 main (3c537dad), and pre-rebase #551 (d662844);
these fixtures deliberately do not import the current or legacy formatter constants.
"""

import json
from pathlib import Path

import pytest

from agent.synthetic_prompt import auto_recall_query, human_prompt_text
from plugins.memory.hindsight.retention import filter_retain_messages


BASE_REVISION_TEMPLATE = (
    '\n\nThis goal has been revised. Each earlier requirement listed below still binds you '
    'unless the user message cited for that revision plainly instructs that specific change. '
    'When in doubt, honor the earlier requirement.\n{revision_lines}'
)

BASE_CONTRACT_TEMPLATE = (
    '[Continuing toward your standing goal]\nGoal: {goal}\n\nCompletion '
    'contract:\n{contract_block}\n\nCheck the CURRENT state now; re-check fresh and assume '
    'nothing from earlier wakeups. If since your last visible update nothing new and '
    "material happened and nothing needs the user's action (for example, you are still "
    'waiting on an external party, a scheduled check, or a background process), reply with '
    'exactly [SILENT] and nothing else. Never post status like "no change", "still waiting", '
    'plain acknowledgements, "next check at", or what you did not do.\nWhen something '
    'changed, report only the new fact or needed action briefly, in one or two short lines. '
    'Claiming done (with an Evidence section) and being blocked or needing input still '
    'require a visible reply.\nContinue working toward the outcome above. Take the next '
    'concrete step. Stay within the stated boundaries and do not violate the constraints. '
    'Your method may change as you learn; the end state may not. If a criterion has become '
    'obsolete or wrong, say so and revise the goal (goal_set action=revise when available, '
    "quoting the user's words when they changed scope) rather than working around it. Before "
    'claiming the goal is done, audit each Verification item against current state and end '
    'with an Evidence section that quotes exact identifiers or output lines from tool '
    'results in backticks (commit SHAs, run ids, URLs, `N passed` lines), so the runtime can '
    'locate them. If you hit the stated stop condition or are otherwise blocked and need '
    'user input, say so clearly and stop.'
)

BASE_SIMPLE_TEMPLATE = (
    '[Continuing toward your standing goal]\nGoal: {goal}\n\nCheck the CURRENT state now; '
    're-check fresh and assume nothing from earlier wakeups. If since your last visible '
    "update nothing new and material happened and nothing needs the user's action (for "
    'example, you are still waiting on an external party, a scheduled check, or a background '
    'process), reply with exactly [SILENT] and nothing else. Never post status like "no '
    'change", "still waiting", plain acknowledgements, "next check at", or what you did not '
    'do.\nWhen something changed, report only the new fact or needed action briefly, in one '
    'or two short lines. Claiming done (with an Evidence section) and being blocked or '
    'needing input still require a visible reply.\nContinue working toward this goal. Take '
    'the next concrete step. If you believe the goal is complete, state so explicitly, cite '
    'the proof (exact identifiers or output lines from tool results, in backticks), and '
    'stop. If you are blocked and need input from the user, say so clearly and stop.'
)

BASE_SUBGOAL_TEMPLATE = (
    '[Continuing toward your standing goal]\nGoal: {goal}\n\nAdditional criteria the user added '
    'mid-loop:\n{subgoals_block}\n\nCheck the CURRENT state now; re-check fresh and assume '
    'nothing from earlier wakeups. If since your last visible update nothing new and material '
    "happened and nothing needs the user's action (for example, you are still waiting on an "
    'external party, a scheduled check, or a background process), reply with exactly [SILENT] '
    'and nothing else. Never post status like "no change", "still waiting", plain '
    'acknowledgements, "next check at", or what you did not do.\nWhen something changed, report '
    'only the new fact or needed action briefly, in one or two short lines. Claiming done '
    '(with an Evidence section) and being blocked or needing input still require a visible '
    'reply.\nContinue working toward the goal AND all additional criteria. Take the next '
    'concrete step. If you believe the goal and every additional criterion are complete, state '
    'so explicitly and stop. If you are blocked and need input from the user, say so clearly '
    'and stop.'
)

PRE_531_ADDITIVE_CONTRACT_TEMPLATE = (
    '[Continuing toward your standing goal]\nGoal: {goal}\n\nCompletion '
    'contract:\n{contract_block}\n\nCheck the CURRENT state now; re-check fresh and assume '
    'nothing from earlier wakeups. If since your last visible update nothing new and material '
    "happened and nothing needs the user's action (for example, you are still waiting on an "
    'external party, a scheduled check, or a background process), reply with exactly [SILENT] '
    'and nothing else. Never post status like "no change", "still waiting", plain '
    'acknowledgements, "next check at", or what you did not do.\nWhen something changed, report '
    'only the new fact or needed action briefly, in one or two short lines. Claiming done '
    '(with an Evidence section) and being blocked or needing input still require a visible '
    'reply.\nContinue working toward the outcome above. Take the next concrete step. Stay '
    'within the stated boundaries and do not violate the constraints. Your method may change '
    'as you learn; the end state may not. If a criterion has become obsolete or wrong, say so. '
    'goal_set action=revise can only add requirements (cite concrete recorded evidence for '
    'verification changes); retiring or replacing an existing item requires a user-typed /goal '
    'remove <kind> <N> or /goal replace command. Before claiming the goal is done, audit each '
    'Verification item against current state and end with an Evidence section that quotes '
    'exact identifiers or output lines from tool results in backticks (commit SHAs, run ids, '
    'URLs, `N passed` lines), so the runtime can locate them. If you hit the stated stop '
    'condition or are otherwise blocked and need user input, say so clearly and stop.'
)

HUMAN = "What did we decide about the deploy pipeline?"


@pytest.mark.parametrize("template", [
    BASE_SIMPLE_TEMPLATE, BASE_CONTRACT_TEMPLATE, BASE_SUBGOAL_TEMPLATE,
    PRE_531_ADDITIVE_CONTRACT_TEMPLATE,
])
@pytest.mark.parametrize("revised", [False, True])
def test_base_goal_continuations_stay_synthetic_for_both_memory_paths(template, revised):
    prompt = template.format(
        goal="Ship safely", contract_block="- Verification: audit passes", subgoals_block="- Run the audit",
    )
    if revised:
        prompt += BASE_REVISION_TEMPLATE.format(
            revision_lines='- v2 (agent, no user authority): shorter check\n'
                           '    earlier verification: Run the security audit.\n\n'
                           '    user quote: "What did we decide about the deploy pipeline?"',
        )

    assert human_prompt_text(prompt) is None
    assert auto_recall_query(prompt) == ""
    assert filter_retain_messages(prompt, "[SILENT]") == (None, None)
    if not revised:
        merged = prompt + "\n" + HUMAN
        assert human_prompt_text(merged) == HUMAN
        assert auto_recall_query(merged) == HUMAN
        assert filter_retain_messages(merged, "Answer.")[0] == HUMAN


@pytest.mark.parametrize("text", [
    HUMAN,
    "This goal has been revised. I want to discuss the old contract.",
    "The old revision header was:\n" + BASE_REVISION_TEMPLATE.format(revision_lines="my notes"),
    "[Continuing toward your standing goal]\nGoal: a header I typed, not a generated prompt",
])
def test_human_text_mentioning_legacy_goal_wording_remains_human(text):
    assert human_prompt_text(text) == text
    assert auto_recall_query(text) == text
    assert filter_retain_messages(text, "Answer.")[0] == text
from string import Formatter
from hermes_cli import goals
from hermes_cli.goals_prompt_registry import CURRENT_GOAL_FORMATTER_NAMES, GOAL_PROMPT_REGISTRY

def _render(template: str) -> str:
    values = {
        "goal": "Ship safely",
        "contract_block": "Verification: audit passes.",
        "subgoals_block": "- 1. Run the audit.",
        "command": "./bin/ci gate",
        "attempt": 1,
        "max_retries": 3,
        "exit_code": 1,
        "output": "test failed",
        "revision_lines": (
            '- v2 (agent, no user authority): narrow the check\n'
            '    earlier verification: Run the audit.\n'
            '    user quote: "What did we decide about the deploy pipeline?"'
        ),
    }
    fields = {field for _, field, _, _ in Formatter().parse(template) if field}
    if "[Goal wait lifted:" in template:
        values["note_field_0"] = "4242" if "pid {" in template else "proc_1234"
        values["note_field_1"] = "30" if "still running after" in template else "exited"
        values["note_field_2"] = "0"
    return template.format(**{field: values[field] for field in fields})


def _rows(formatter: str):
    return [row for row in GOAL_PROMPT_REGISTRY if row["formatter"] == formatter]


FROZEN_REGISTRY_ROWS = json.loads(
    (Path(__file__).parents[1] / "fixtures" / "goal_prompt_history.json").read_text(encoding="utf-8")
)


@pytest.mark.parametrize("new_wording", [False, True])
def test_regeneration_keeps_frozen_history_outside_reachable_commits(tmp_path, monkeypatch, new_wording):
    import runpy
    import sys
    from scripts.dev import snapshot_goal_prompt_history as snapshot

    historical = FROZEN_REGISTRY_ROWS[0]
    output = tmp_path / "hermes_cli" / "goals_prompt_registry.py"
    output.parent.mkdir()
    output.write_text(f"GOAL_PROMPT_REGISTRY: tuple = ({historical!r},)\n", encoding="utf-8")
    current = "A new contract for {goal}: {contract_block}." if new_wording else historical["template"]
    (output.parent / "goals.py").write_text(
        f"CONTINUATION_PROMPT_WITH_CONTRACT_TEMPLATE = {current!r}\n", encoding="utf-8",
    )
    monkeypatch.setattr(snapshot, "REPO", tmp_path)
    monkeypatch.setattr(snapshot, "OUTPUT", output)
    monkeypatch.setattr(snapshot, "_commits", list)
    monkeypatch.setattr(sys, "argv", ["snapshot_goal_prompt_history.py"])

    assert snapshot.main() == 0
    rows = runpy.run_path(str(output))["GOAL_PROMPT_REGISTRY"]
    by_template = {row["template"]: row for row in rows}
    assert set(by_template) == {historical["template"], current}
    assert by_template[historical["template"]]["source_commits"] == tuple(historical["source_commits"])
    if new_wording:
        assert by_template[current]["source_commits"] == ("working-tree",)


@pytest.mark.parametrize("row", FROZEN_REGISTRY_ROWS, ids=lambda row: row["source_commits"][0][:8])
def test_frozen_unreachable_history_stays_out_of_both_memory_paths(row):
    prompt = _render(row["template"])
    if row["formatter"] == "revisions":
        prompt = _render(BASE_SIMPLE_TEMPLATE) + prompt
    assert human_prompt_text(prompt) is None
    assert auto_recall_query(prompt) == ""
    assert filter_retain_messages(prompt, "[SILENT]") == (None, None)

    merged = prompt + "\n\n" + HUMAN
    if row["formatter"] == "revisions":
        # Unframed revision quotes can span arbitrary paragraphs, so their suffix is ambiguous.
        assert human_prompt_text(merged) is None
        assert auto_recall_query(merged) == ""
        assert filter_retain_messages(merged, "[SILENT]") == (None, None)
    else:
        assert human_prompt_text(merged) == HUMAN
        assert auto_recall_query(merged) == HUMAN
        assert filter_retain_messages(merged, "[SILENT]")[0] == HUMAN


def test_registry_covers_every_current_goal_formatter_constant():
    current = {
        "autonomy": goals.AUTONOMY_CONTINUATION_TEMPLATE,
        "plain": goals.CONTINUATION_PROMPT_TEMPLATE,
        "contract": goals.CONTINUATION_PROMPT_WITH_CONTRACT_TEMPLATE,
        "subgoals": goals.CONTINUATION_PROMPT_WITH_SUBGOALS_TEMPLATE,
        "gate_failed": goals.CONTINUATION_PROMPT_GATE_FAILED_TEMPLATE,
        "barrier_lift": goals.GOAL_WAIT_LIFTED_NOTE_OPEN,
    }
    registered = {(row["formatter"], row["template"]) for row in GOAL_PROMPT_REGISTRY}
    current_names = {
        name for name, value in vars(goals).items()
        if name.startswith("CONTINUATION_") and name.endswith("_TEMPLATE") and isinstance(value, str)
    } | {"GOAL_WAIT_LIFTED_NOTE_OPEN", "AUTONOMY_CONTINUATION_TEMPLATE"}
    assert current_names == {name for _, name in CURRENT_GOAL_FORMATTER_NAMES}
    assert set(current) == {kind for kind, _ in CURRENT_GOAL_FORMATTER_NAMES}
    assert all((kind, template) in registered for kind, template in current.items())
    # Inline barrier f-strings are guarded through the real formatter, not its source shape.
    from unittest.mock import patch
    from agent.synthetic_prompt import _TemplateMatcher

    notes = []
    process = goals.GoalState(goal="Ship safely", waiting_on_session="proc_1234")
    outcomes = (
        None,
        {"running": True},
        {"completion_reason": "killed", "termination_source": "kill_all", "exit_code": -15},
        {"completion_reason": "killed", "termination_source": "explicit", "exit_code": -15},
        {"completion_reason": "exited", "exit_code": 0},
    )
    for outcome in outcomes:
        with patch.object(goals, "_process_outcome", return_value=outcome):
            notes.append(goals._barrier_lift_note(process))
    pid = goals.GoalState(goal="Ship safely", waiting_on_pid=4242)
    for alive in (True, False):
        with patch.object(goals, "_pid_alive", return_value=alive):
            notes.append(goals._barrier_lift_note(pid))
    matchers = [_TemplateMatcher(row["template"]) for row in _rows("barrier_lift_note")]
    assert all(note and any(matcher.match_end(note) == len(note) for matcher in matchers) for note in notes)


def test_generator_adds_new_uncommitted_wording_without_losing_the_old_snapshot():
    from collections import defaultdict
    from scripts.dev.snapshot_goal_prompt_history import FORMATTER_NAMES, _collect_source

    registry = {kind: defaultdict(list) for kind in (*FORMATTER_NAMES, "barrier_lift_note")}
    for row in GOAL_PROMPT_REGISTRY:
        registry[row["formatter"]][row["template"]] = list(row["source_commits"])
    original = set(registry["plain"])
    future = "A newly edited continuation for {goal}."
    source = f"CONTINUATION_PROMPT_TEMPLATE = {future!r}"
    _collect_source(registry, "working-tree", "hermes_cli/goals.py", source, new_only=True)
    assert set(registry["plain"]) == original | {future}
    assert registry["plain"][future] == ["working-tree"]


@pytest.mark.parametrize(
    "row",
    [row for row in GOAL_PROMPT_REGISTRY if row["formatter"] in {"autonomy", "plain", "contract", "subgoals", "gate_failed"}],
    ids=lambda row: f"{row['formatter']}-{row['source_commits'][0][:8]}",
)
def test_every_historical_continuation_wording_is_synthetic_and_keeps_a_real_suffix(row):
    prompt = _render(str(row["template"]))

    assert human_prompt_text(prompt) is None
    assert auto_recall_query(prompt) == ""
    assert filter_retain_messages(prompt, "[SILENT]") == (None, None)

    merged = prompt + "\n\n" + HUMAN
    assert human_prompt_text(merged) == HUMAN
    assert auto_recall_query(merged) == HUMAN
    assert filter_retain_messages(merged, "[SILENT]")[0] == HUMAN
    # Every complete formatter variant can also carry any of the historical unbounded revision
    # appendices. Their quoted request is generated payload, never new user authority or memory.
    for revision in _rows("revisions"):
        revised = prompt + _render(str(revision["template"]))
        assert human_prompt_text(revised) is None
        assert auto_recall_query(revised) == ""
        assert filter_retain_messages(revised, "[SILENT]") == (None, None)


@pytest.mark.parametrize("row", _rows("revisions"), ids=lambda row: f"revision-{row['source_commits'][0][:8]}")
def test_every_historical_revision_appendix_is_synthetic_even_when_it_quotes_the_user(row):
    base = _render(str(_rows("plain")[-1]["template"]))
    revised = base + _render(str(row["template"]))

    assert human_prompt_text(revised) is None
    assert auto_recall_query(revised) == ""
    assert filter_retain_messages(revised, "[SILENT]") == (None, None)
    # This legacy appendix has no closing marker and user quotes can span paragraphs. Keep the
    # existing fail-closed rule: an unframed suffix cannot be distinguished from the quoted text.
    assert human_prompt_text(revised + "\n\n" + HUMAN) is None
    assert auto_recall_query(revised + "\n\n" + HUMAN) == ""
    assert filter_retain_messages(revised + "\n\n" + HUMAN, "[SILENT]") == (None, None)


@pytest.mark.parametrize("row", _rows("barrier_lift"), ids=lambda row: f"barrier-{row['source_commits'][0][:8]}")
def test_every_historical_barrier_lift_note_is_synthetic_and_keeps_a_real_suffix(row):
    base = _render(str(_rows("plain")[-1]["template"]))
    note = str(row["template"]) + "pid 4242 has exited. Verify its result before continuing.]"
    prompt = base + "\n\n" + note

    assert human_prompt_text(prompt) is None
    assert auto_recall_query(prompt) == ""
    assert filter_retain_messages(prompt, "[SILENT]") == (None, None)

    merged = prompt + "\n\n" + HUMAN
    assert human_prompt_text(merged) == HUMAN
    assert auto_recall_query(merged) == HUMAN
    assert filter_retain_messages(merged, "[SILENT]")[0] == HUMAN


@pytest.mark.parametrize("row", _rows("barrier_lift_note"), ids=lambda row: f"note-{row['source_commits'][0][:8]}")
def test_every_full_historical_barrier_note_keeps_only_the_real_human_suffix(row):
    base = _render(str(_rows("plain")[-1]["template"]))
    prompt = base + "\n\n" + _render(str(row["template"]))
    assert human_prompt_text(prompt) is None
    assert auto_recall_query(prompt) == ""
    assert filter_retain_messages(prompt, "[SILENT]") == (None, None)
    merged = prompt + "\n\n" + HUMAN
    assert human_prompt_text(merged) == HUMAN
    assert auto_recall_query(merged) == HUMAN
    assert filter_retain_messages(merged, "[SILENT]")[0] == HUMAN


@pytest.mark.parametrize(
    "text",
    [
        HUMAN,
        "This goal has been revised. I want to discuss the old contract.",
        "The old revision header was:\n" + _render(str(_rows("revisions")[0]["template"])),
        "[Continuing toward your standing goal]\nGoal: a header I typed, not a generated prompt",
    ],
)
def test_human_text_mentioning_registry_goal_wording_remains_human(text):
    assert human_prompt_text(text) == text
    assert auto_recall_query(text) == text
    assert filter_retain_messages(text, "Answer.")[0] == text
