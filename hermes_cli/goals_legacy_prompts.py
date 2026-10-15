"""Frozen goal formatter literals for classifying persisted rows without provenance."""

# Exact pre-additive-only formatter literals: persisted rows have no structured provenance.
_LEGACY_CONTINUATION_REVISIONS_TEMPLATE = (
    '\n\nThis goal has been revised. Each earlier requirement listed below still binds you '
    'unless the user message cited for that revision plainly instructs that specific change. '
    'When in doubt, honor the earlier requirement.\n{revision_lines}'
)

_LEGACY_CONTINUATION_PROMPT_WITH_CONTRACT_TEMPLATE = (
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

_LEGACY_CONTINUATION_PROMPT_TEMPLATE = (
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

_LEGACY_CONTINUATION_PROMPT_WITH_SUBGOALS_TEMPLATE = (
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

_PRE_531_ADDITIVE_CONTRACT_TEMPLATE = (
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
