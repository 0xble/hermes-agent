"""Generated goal formatter wording registry; do not edit by hand.

Regenerate with:
    python scripts/dev/snapshot_goal_prompt_history.py
"""

from typing import Final, TypedDict


class GoalPromptEntry(TypedDict):
    formatter: str
    template: str
    source_commits: tuple[str, ...]


GOAL_PROMPT_REGISTRY: Final[tuple[GoalPromptEntry, ...]] = ({'formatter': 'autonomy',
  'template': '[Continuing toward your standing goal] Goal: {goal}\n'
              '\n'
              'Completion contract:\n'
              '{contract_block}\n'
              '{subgoals_block}\n'
              '\n'
              'Manage this goal autonomously. Choose the next concrete authorized step. If a route fails, try a new '
              'route. For an external dependency use a timed wait with an automatic wake, rather than stopping for '
              'input or approval. Preserve every constraint, hard limit and verification criterion when revising or '
              'replacing. Never use complete or replace to escape constraints, failing checks or unsolved work. Use '
              'goal_set complete only with an evidence mapping from EVERY verification criterion to concrete recorded '
              'tool output. Never clear a goal. If nothing material changed, reply exactly [SILENT].',
  'source_commits': ('a106bbe2aaeca9dcb56e7fd5ef2303c901c4070b', '0295c3b67d17fc24939cdb695083bfd0fbc3d3c7')},
 {'formatter': 'plain',
  'template': '[Continuing toward your standing goal]\n'
              'Goal: {goal}\n'
              '\n'
              'Continue working toward this goal. Take the next concrete step. If you believe the goal is complete, '
              'state so explicitly and stop. If you are blocked and need input from the user, say so clearly and stop.',
  'source_commits': ('b1ccce898b5f7c4112658f5c0aebb731c7be080f', '265bd59c1d9f8dea658f243b257d4fae3685af53')},
 {'formatter': 'plain',
  'template': '[Continuing toward your standing goal]\n'
              'Goal: {goal}\n'
              '\n'
              'Continue working toward this goal. Take the next concrete step. If you believe the goal is complete, '
              'state so explicitly, cite the proof (exact identifiers or output lines from tool results, in '
              'backticks), and stop. If you are blocked and need input from the user, say so clearly and stop.',
  'source_commits': ('57a44a82a9de48b5be53b59a9d84d8bc47c620ea', '8a5d8f76f7288e0de4fff910400f27d28f22b444')},
 {'formatter': 'plain',
  'template': '[Continuing toward your standing goal]\n'
              'Goal: {goal}\n'
              '\n'
              'Check the CURRENT state now; re-check fresh and assume nothing from earlier wakeups. If since your last '
              "visible update nothing new and material happened and nothing needs the user's action (for example, you "
              'are still waiting on an external party, a scheduled check, or a background process), reply with exactly '
              '[SILENT] and nothing else. Never post status like "no change", "still waiting", plain acknowledgements, '
              '"next check at", or what you did not do.\n'
              'When something changed, report only the new fact or needed action briefly, in one or two short lines. '
              'Claiming done (with an Evidence section) and being blocked or needing input still require a visible '
              'reply.\n'
              'Continue working toward this goal. Take the next concrete step. If you believe the goal is complete, '
              'state so explicitly, cite the proof (exact identifiers or output lines from tool results, in '
              'backticks), and stop. If you are blocked and need input from the user, say so clearly and stop.',
  'source_commits': ('a106bbe2aaeca9dcb56e7fd5ef2303c901c4070b', '11aa16592efeec426e9869e8fc5b3b23f2d9a400')},
 {'formatter': 'plain',
  'template': '[Continuing toward your standing goal]\n'
              'Goal: {goal}\n'
              '\n'
              'Check the CURRENT state now; re-check fresh and assume nothing from earlier wakeups. If since your last '
              "visible update nothing new and material happened and nothing needs the user's action (for example, you "
              'are still waiting on an external party, a scheduled check, or a background process), reply with exactly '
              '[SILENT] and nothing else. Never post status like "no change", "still waiting", plain acknowledgements, '
              '"next check at", or what you did not do.\n'
              'When something changed, report only the new fact or needed action briefly, in one or two short lines. '
              'The FIRST time you claim the goal is done, include an Evidence section with exact proof and make that '
              'claim visible. Make a done claim visible again only when its substance changed because new evidence, '
              'new state, or a changed blocker appeared. If you already reported done and nothing material changed '
              'since that report, reply with exactly [SILENT] and nothing else; the runtime will still re-check '
              'completion. The FIRST time you are blocked or need user input, say so visibly. While the same blocker '
              'or input need is unchanged, reply with exactly [SILENT] and nothing else.\n'
              'Continue working toward this goal. Take the next concrete step. If you believe the goal is complete, '
              'state so explicitly, cite the proof (exact identifiers or output lines from tool results, in '
              'backticks), and stop. If you are blocked and need input from the user, say so clearly and stop.',
  'source_commits': ('e07a10459b74a2edc93b79c1617608d34fdfe13f', '3e4f2d53c3b5dc681a31904dac89b6d472f4ea1a')},
 {'formatter': 'plain',
  'template': '[Continuing toward your standing goal]\n'
              'Goal: {goal}\n'
              '\n'
              'Check the CURRENT state now; re-check fresh and assume nothing from earlier wakeups. If since your last '
              "visible update nothing new and material happened and nothing needs the user's action (for example, you "
              'are still waiting on an external party, a scheduled check, or a background process), reply with exactly '
              '[SILENT] and nothing else. Never post status like "no change", "still waiting", plain acknowledgements, '
              '"next check at", or what you did not do.\n'
              'When something changed, report only the new fact or needed action briefly, in one or two short lines. '
              'The FIRST time you claim the goal is done, include an Evidence section with exact proof and make that '
              'claim visible above it. Evidence may be a collapsed <details><summary>Evidence</summary>...</details> '
              'block where the platform renders toggles; keep exact identifiers and output lines verbatim in '
              'backticks. Make a done claim visible again only when its substance changed because new evidence, new '
              'state, or a changed blocker appeared. If you already reported done and nothing material changed since '
              'that report, reply with exactly [SILENT] and nothing else; the runtime will still re-check completion. '
              'The FIRST time you are blocked or need user input, say so visibly. While the same blocker or input need '
              'is unchanged, reply with exactly [SILENT] and nothing else.\n'
              'Continue working toward this goal. Take the next concrete step. If you believe the goal is complete, '
              'state so explicitly, cite the proof (exact identifiers or output lines from tool results, in '
              'backticks), and stop. If you are blocked and need input from the user, say so clearly and stop.',
  'source_commits': ('c1bc50da497da9d430ab71dbe723ce19fb92e6ad', '5dc1a7f693d97c02e73e6f291d222c693ff60515')},
 {'formatter': 'plain',
  'template': '[Continuing toward your standing goal]\n'
              'Goal: {goal}\n'
              '\n'
              'Check the CURRENT state now; re-check fresh and assume nothing from earlier wakeups. If since your last '
              "visible update nothing new and material happened and nothing needs the user's action (for example, you "
              'are still waiting on an external party, a scheduled check, or a background process), reply with exactly '
              '[SILENT] and nothing else. Never post status like "no change", "still waiting", plain acknowledgements, '
              '"next check at", or what you did not do.\n'
              'When something changed, report only the new fact or needed action briefly, in one or two short lines. '
              'The FIRST time you claim the goal is done, include an Evidence section with exact proof and make that '
              'claim visible above it. Evidence may be a collapsed <details><summary>Evidence</summary>...</details> '
              'block where the platform renders toggles; keep exact identifiers and output lines verbatim in '
              'backticks. Make a done claim visible again only when its substance changed because new evidence, new '
              'state, or a changed blocker appeared. If you already reported done and nothing material changed since '
              'that report, reply with exactly [SILENT] and nothing else; the runtime will still re-check completion. '
              'A restart, update, or new release by itself is not news, and re-running a check that returns the same '
              'result as your last visible report counts as unchanged; reply with exactly [SILENT] and nothing else. '
              'The FIRST time you are blocked or need user input, say so visibly. While the same blocker or input need '
              'is unchanged, reply with exactly [SILENT] and nothing else.\n'
              'Continue working toward this goal. Take the next concrete step. If you believe the goal is complete, '
              'state so explicitly, cite the proof (exact identifiers or output lines from tool results, in '
              'backticks), and stop. If you are blocked and need input from the user, say so clearly and stop.',
  'source_commits': ('a106bbe2aaeca9dcb56e7fd5ef2303c901c4070b', '511d3b72b438b4a9005c8b9d171fd6b5d649175e')},
 {'formatter': 'contract',
  'template': '[Continuing toward your standing goal]\n'
              'Goal: {goal}\n'
              '\n'
              'Completion contract:\n'
              '{contract_block}\n'
              '\n'
              'Continue working toward the outcome above. Take the next concrete step. Stay within the stated '
              'boundaries and do not violate the constraints. Before claiming the goal is done, satisfy the '
              'Verification criterion and show the concrete evidence (command output, file contents, test result). If '
              'you hit the stated stop condition or are otherwise blocked and need user input, say so clearly and '
              'stop.',
  'source_commits': ('b1ccce898b5f7c4112658f5c0aebb731c7be080f', '2ba1cfeb2e28c77a3ae2323772e5a6bca43844cb')},
 {'formatter': 'contract',
  'template': '[Continuing toward your standing goal]\n'
              'Goal: {goal}\n'
              '\n'
              'Completion contract:\n'
              '{contract_block}\n'
              '\n'
              'Continue working toward the outcome above. Take the next concrete step. Stay within the stated '
              'boundaries and do not violate the constraints. Your method may change as you learn; the end state may '
              'not. If a criterion has become obsolete or wrong, say so and revise the goal (goal_set action=revise '
              "when available, quoting the user's words when they changed scope) rather than working around it. Before "
              'claiming the goal is done, audit each Verification item against current state and end with an Evidence '
              'section that quotes exact identifiers or output lines from tool results in backticks (commit SHAs, run '
              'ids, URLs, `N passed` lines), so the runtime can locate them. If you hit the stated stop condition or '
              'are otherwise blocked and need user input, say so clearly and stop.',
  'source_commits': ('57a44a82a9de48b5be53b59a9d84d8bc47c620ea', '8a5d8f76f7288e0de4fff910400f27d28f22b444')},
 {'formatter': 'contract',
  'template': '[Continuing toward your standing goal]\n'
              'Goal: {goal}\n'
              '\n'
              'Completion contract:\n'
              '{contract_block}\n'
              '\n'
              'Check the CURRENT state now; re-check fresh and assume nothing from earlier wakeups. If since your last '
              "visible update nothing new and material happened and nothing needs the user's action (for example, you "
              'are still waiting on an external party, a scheduled check, or a background process), reply with exactly '
              '[SILENT] and nothing else. Never post status like "no change", "still waiting", plain acknowledgements, '
              '"next check at", or what you did not do.\n'
              'When something changed, report only the new fact or needed action briefly, in one or two short lines. '
              'Claiming done (with an Evidence section) and being blocked or needing input still require a visible '
              'reply.\n'
              'Continue working toward the outcome above. Take the next concrete step. Stay within the stated '
              'boundaries and do not violate the constraints. Your method may change as you learn; the end state may '
              'not. If a criterion has become obsolete or wrong, say so and revise the goal (goal_set action=revise '
              "when available, quoting the user's words when they changed scope) rather than working around it. Before "
              'claiming the goal is done, audit each Verification item against current state and end with an Evidence '
              'section that quotes exact identifiers or output lines from tool results in backticks (commit SHAs, run '
              'ids, URLs, `N passed` lines), so the runtime can locate them. If you hit the stated stop condition or '
              'are otherwise blocked and need user input, say so clearly and stop.',
  'source_commits': ('a106bbe2aaeca9dcb56e7fd5ef2303c901c4070b', '11aa16592efeec426e9869e8fc5b3b23f2d9a400')},
 {'formatter': 'contract',
  'template': '[Continuing toward your standing goal]\n'
              'Goal: {goal}\n'
              '\n'
              'Completion contract:\n'
              '{contract_block}\n'
              '\n'
              'Check the CURRENT state now; re-check fresh and assume nothing from earlier wakeups. If since your last '
              "visible update nothing new and material happened and nothing needs the user's action (for example, you "
              'are still waiting on an external party, a scheduled check, or a background process), reply with exactly '
              '[SILENT] and nothing else. Never post status like "no change", "still waiting", plain acknowledgements, '
              '"next check at", or what you did not do.\n'
              'When something changed, report only the new fact or needed action briefly, in one or two short lines. '
              'Claiming done (with an Evidence section) and being blocked or needing input still require a visible '
              'reply.\n'
              'Continue working toward the outcome above. Take the next concrete step. Stay within the stated '
              'boundaries and do not violate the constraints. Your method may change as you learn; the end state may '
              'not. If a criterion has become obsolete or wrong, say so. goal_set action=revise can only add '
              'requirements (cite concrete recorded evidence for verification changes); retiring or replacing an '
              'existing item requires a user-typed /goal remove <kind> <N> or /goal replace command. Before claiming '
              'the goal is done, audit each Verification item against current state and end with an Evidence section '
              'that quotes exact identifiers or output lines from tool results in backticks (commit SHAs, run ids, '
              'URLs, `N passed` lines), so the runtime can locate them. If you hit the stated stop condition or are '
              'otherwise blocked and need user input, say so clearly and stop.',
  'source_commits': ('a106bbe2aaeca9dcb56e7fd5ef2303c901c4070b', '1ceabf68cabce7f42d80f4d77e86b259fb7d4ce2')},
 {'formatter': 'contract',
  'template': '[Continuing toward your standing goal]\n'
              'Goal: {goal}\n'
              '\n'
              'Completion contract:\n'
              '{contract_block}\n'
              '\n'
              'Check the CURRENT state now; re-check fresh and assume nothing from earlier wakeups. If since your last '
              "visible update nothing new and material happened and nothing needs the user's action (for example, you "
              'are still waiting on an external party, a scheduled check, or a background process), reply with exactly '
              '[SILENT] and nothing else. Never post status like "no change", "still waiting", plain acknowledgements, '
              '"next check at", or what you did not do.\n'
              'When something changed, report only the new fact or needed action briefly, in one or two short lines. '
              'The FIRST time you claim the goal is done, include an Evidence section with exact proof and make that '
              'claim visible. Make a done claim visible again only when its substance changed because new evidence, '
              'new state, or a changed blocker appeared. If you already reported done and nothing material changed '
              'since that report, reply with exactly [SILENT] and nothing else; the runtime will still re-check '
              'completion. The FIRST time you are blocked or need user input, say so visibly. While the same blocker '
              'or input need is unchanged, reply with exactly [SILENT] and nothing else.\n'
              'Continue working toward the outcome above. Take the next concrete step. Stay within the stated '
              'boundaries and do not violate the constraints. Your method may change as you learn; the end state may '
              'not. If a criterion has become obsolete or wrong, say so and revise the goal (goal_set action=revise '
              "when available, quoting the user's words when they changed scope) rather than working around it. Before "
              'claiming the goal is done, audit each Verification item against current state and end with an Evidence '
              'section that quotes exact identifiers or output lines from tool results in backticks (commit SHAs, run '
              'ids, URLs, `N passed` lines), so the runtime can locate them. If you hit the stated stop condition or '
              'are otherwise blocked and need user input, say so clearly and stop.',
  'source_commits': ('e07a10459b74a2edc93b79c1617608d34fdfe13f', '3e4f2d53c3b5dc681a31904dac89b6d472f4ea1a')},
 {'formatter': 'contract',
  'template': '[Continuing toward your standing goal]\n'
              'Goal: {goal}\n'
              '\n'
              'Completion contract:\n'
              '{contract_block}\n'
              '\n'
              'Check the CURRENT state now; re-check fresh and assume nothing from earlier wakeups. If since your last '
              "visible update nothing new and material happened and nothing needs the user's action (for example, you "
              'are still waiting on an external party, a scheduled check, or a background process), reply with exactly '
              '[SILENT] and nothing else. Never post status like "no change", "still waiting", plain acknowledgements, '
              '"next check at", or what you did not do.\n'
              'When something changed, report only the new fact or needed action briefly, in one or two short lines. '
              'The FIRST time you claim the goal is done, include an Evidence section with exact proof and make that '
              'claim visible above it. Evidence may be a collapsed <details><summary>Evidence</summary>...</details> '
              'block where the platform renders toggles; keep exact identifiers and output lines verbatim in '
              'backticks. Make a done claim visible again only when its substance changed because new evidence, new '
              'state, or a changed blocker appeared. If you already reported done and nothing material changed since '
              'that report, reply with exactly [SILENT] and nothing else; the runtime will still re-check completion. '
              'The FIRST time you are blocked or need user input, say so visibly. While the same blocker or input need '
              'is unchanged, reply with exactly [SILENT] and nothing else.\n'
              'Continue working toward the outcome above. Take the next concrete step. Stay within the stated '
              'boundaries and do not violate the constraints. Your method may change as you learn; the end state may '
              'not. If a criterion has become obsolete or wrong, say so and revise the goal (goal_set action=revise '
              "when available, quoting the user's words when they changed scope) rather than working around it. Before "
              'claiming the goal is done, audit each Verification item against current state and end with an Evidence '
              'section (plain or collapsible) that quotes exact identifiers or output lines from tool results in '
              'backticks (commit SHAs, run ids, URLs, `N passed` lines), so the runtime can locate them. If you hit '
              'the stated stop condition or are otherwise blocked and need user input, say so clearly and stop.',
  'source_commits': ('46824125f365d6220cc391378bb4398faa13b1cb', '5dc1a7f693d97c02e73e6f291d222c693ff60515')},
 {'formatter': 'contract',
  'template': '[Continuing toward your standing goal]\n'
              'Goal: {goal}\n'
              '\n'
              'Completion contract:\n'
              '{contract_block}\n'
              '\n'
              'Check the CURRENT state now; re-check fresh and assume nothing from earlier wakeups. If since your last '
              "visible update nothing new and material happened and nothing needs the user's action (for example, you "
              'are still waiting on an external party, a scheduled check, or a background process), reply with exactly '
              '[SILENT] and nothing else. Never post status like "no change", "still waiting", plain acknowledgements, '
              '"next check at", or what you did not do.\n'
              'When something changed, report only the new fact or needed action briefly, in one or two short lines. '
              'The FIRST time you claim the goal is done, include an Evidence section with exact proof and make that '
              'claim visible above it. Evidence may be a collapsed <details><summary>Evidence</summary>...</details> '
              'block where the platform renders toggles; keep exact identifiers and output lines verbatim in '
              'backticks. Make a done claim visible again only when its substance changed because new evidence, new '
              'state, or a changed blocker appeared. If you already reported done and nothing material changed since '
              'that report, reply with exactly [SILENT] and nothing else; the runtime will still re-check completion. '
              'The FIRST time you are blocked or need user input, say so visibly. While the same blocker or input need '
              'is unchanged, reply with exactly [SILENT] and nothing else.\n'
              'Continue working toward the outcome above. Take the next concrete step. Stay within the stated '
              'boundaries and do not violate the constraints. Your method may change as you learn; the end state may '
              'not. If a criterion has become obsolete or wrong, say so. goal_set action=revise can only add '
              'requirements (cite concrete recorded evidence for verification changes); retiring or replacing an '
              'existing item requires a user-typed /goal remove <kind> <N> or /goal replace command. Before claiming '
              'the goal is done, audit each Verification item against current state and end with an Evidence section '
              'that quotes exact identifiers or output lines from tool results in backticks (commit SHAs, run ids, '
              'URLs, `N passed` lines), so the runtime can locate them. If you hit the stated stop condition or are '
              'otherwise blocked and need user input, say so clearly and stop.',
  'source_commits': ('c1bc50da497da9d430ab71dbe723ce19fb92e6ad', 'c4ed80e35fa6b561df01aa7b9adb41e9bfd27cd5')},
 {'formatter': 'contract',
  'template': '[Continuing toward your standing goal]\n'
              'Goal: {goal}\n'
              '\n'
              'Completion contract:\n'
              '{contract_block}\n'
              '\n'
              'Check the CURRENT state now; re-check fresh and assume nothing from earlier wakeups. If since your last '
              "visible update nothing new and material happened and nothing needs the user's action (for example, you "
              'are still waiting on an external party, a scheduled check, or a background process), reply with exactly '
              '[SILENT] and nothing else. Never post status like "no change", "still waiting", plain acknowledgements, '
              '"next check at", or what you did not do.\n'
              'When something changed, report only the new fact or needed action briefly, in one or two short lines. '
              'The FIRST time you claim the goal is done, include an Evidence section with exact proof and make that '
              'claim visible above it. Evidence may be a collapsed <details><summary>Evidence</summary>...</details> '
              'block where the platform renders toggles; keep exact identifiers and output lines verbatim in '
              'backticks. Make a done claim visible again only when its substance changed because new evidence, new '
              'state, or a changed blocker appeared. If you already reported done and nothing material changed since '
              'that report, reply with exactly [SILENT] and nothing else; the runtime will still re-check completion. '
              'The FIRST time you are blocked or need user input, say so visibly. While the same blocker or input need '
              'is unchanged, reply with exactly [SILENT] and nothing else.\n'
              'Continue working toward the outcome above. Take the next concrete step. Stay within the stated '
              'boundaries and do not violate the constraints. Your method may change as you learn; the end state may '
              'not. If a criterion has become obsolete or wrong, revise a verification item through goal_set '
              'action=revise only with concrete recorded evidence. All other revisions are additive-only, including '
              'verification changes without evidence. Destructive changes require a user-typed /goal replace or /goal '
              'remove command. Before claiming the goal is done, audit each Verification item against current state '
              'and end with an Evidence section that quotes exact identifiers or output lines from tool results in '
              'backticks (commit SHAs, run ids, URLs, `N passed` lines), so the runtime can locate them. If you hit '
              'the stated stop condition or are otherwise blocked and need user input, say so clearly and stop.',
  'source_commits': ('70a1b4ab682b03fbdfaa988a6fde1f01b79c47f4', 'd25d181f8d7a93a882301668dd76d281f6d6dce5')},
 {'formatter': 'contract',
  'template': '[Continuing toward your standing goal]\n'
              'Goal: {goal}\n'
              '\n'
              'Completion contract:\n'
              '{contract_block}\n'
              '\n'
              'Check the CURRENT state now; re-check fresh and assume nothing from earlier wakeups. If since your last '
              "visible update nothing new and material happened and nothing needs the user's action (for example, you "
              'are still waiting on an external party, a scheduled check, or a background process), reply with exactly '
              '[SILENT] and nothing else. Never post status like "no change", "still waiting", plain acknowledgements, '
              '"next check at", or what you did not do.\n'
              'When something changed, report only the new fact or needed action briefly, in one or two short lines. '
              'The FIRST time you claim the goal is done, include an Evidence section with exact proof and make that '
              'claim visible above it. Evidence may be a collapsed <details><summary>Evidence</summary>...</details> '
              'block where the platform renders toggles; keep exact identifiers and output lines verbatim in '
              'backticks. Make a done claim visible again only when its substance changed because new evidence, new '
              'state, or a changed blocker appeared. If you already reported done and nothing material changed since '
              'that report, reply with exactly [SILENT] and nothing else; the runtime will still re-check completion. '
              'A restart, update, or new release by itself is not news, and re-running a check that returns the same '
              'result as your last visible report counts as unchanged; reply with exactly [SILENT] and nothing else. '
              'The FIRST time you are blocked or need user input, say so visibly. While the same blocker or input need '
              'is unchanged, reply with exactly [SILENT] and nothing else.\n'
              'Continue working toward the outcome above. Take the next concrete step. Stay within the stated '
              'boundaries and do not violate the constraints. Your method may change as you learn; the end state may '
              'not. If a criterion has become obsolete or wrong, say so. goal_set action=revise can only add '
              'requirements (cite concrete recorded evidence for verification changes); retiring or replacing an '
              'existing item requires a user-typed /goal remove <kind> <N> or /goal replace command. Before claiming '
              'the goal is done, audit each Verification item against current state and end with an Evidence section '
              'that quotes exact identifiers or output lines from tool results in backticks (commit SHAs, run ids, '
              'URLs, `N passed` lines), so the runtime can locate them. If you hit the stated stop condition or are '
              'otherwise blocked and need user input, say so clearly and stop.',
  'source_commits': ('deb018da2e4c71906ec9f532cdf74b15feaca6c2', '511d3b72b438b4a9005c8b9d171fd6b5d649175e')},
 {'formatter': 'contract',
  'template': '[Continuing toward your standing goal]\n'
              'Goal: {goal}\n'
              '\n'
              'Completion contract:\n'
              '{contract_block}\n'
              '\n'
              'Check the CURRENT state now; re-check fresh and assume nothing from earlier wakeups. If since your last '
              "visible update nothing new and material happened and nothing needs the user's action (for example, you "
              'are still waiting on an external party, a scheduled check, or a background process), reply with exactly '
              '[SILENT] and nothing else. Never post status like "no change", "still waiting", plain acknowledgements, '
              '"next check at", or what you did not do.\n'
              'When something changed, report only the new fact or needed action briefly, in one or two short lines. '
              'The FIRST time you claim the goal is done, include an Evidence section with exact proof and make that '
              'claim visible above it. Evidence may be a collapsed <details><summary>Evidence</summary>...</details> '
              'block where the platform renders toggles; keep exact identifiers and output lines verbatim in '
              'backticks. Make a done claim visible again only when its substance changed because new evidence, new '
              'state, or a changed blocker appeared. If you already reported done and nothing material changed since '
              'that report, reply with exactly [SILENT] and nothing else; the runtime will still re-check completion. '
              'The FIRST time you are blocked or need user input, say so visibly. While the same blocker or input need '
              'is unchanged, reply with exactly [SILENT] and nothing else.\n'
              'Continue working toward the outcome above. Take the next concrete step. Stay within the stated '
              'boundaries and do not violate the constraints. Your method may change as you learn; the end state may '
              'not. If a criterion has become obsolete or wrong, say so and revise the goal (goal_set action=revise '
              "when available, quoting the user's words when they changed scope) rather than working around it. Before "
              'claiming the goal is done, audit each Verification item against current state and end with an Evidence '
              'section (plain or collapsible) that quotes exact identifiers or output lines from tool results in has '
              'become obsolete or wrong, say so and revise the goal (goal_set action=revise when available, quoting '
              "the user's words when they changed scope) rather than working around it. Before claiming the goal is "
              'done, audit each Verification item against current state and end with an Evidence section that quotes '
              'exact identifiers or output lines from tool results in backticks (commit SHAs, run ids, URLs, `N '
              'passed` lines), so the runtime can locate them. If you hit the stated stop condition or are otherwise '
              'blocked and need user input, say so clearly and stop.',
  'source_commits': ('665b47ab0a2c25689fc222e52f8957f4f71b6711', '47afd1c2e0009b4b519180e55ef0b0a572b0c848')},
 {'formatter': 'contract',
  'template': '[Continuing toward your standing goal]\n'
              'Goal: {goal}\n'
              '\n'
              'Completion contract:\n'
              '{contract_block}\n'
              '\n'
              'Check the CURRENT state now; re-check fresh and assume nothing from earlier wakeups. If since your last '
              "visible update nothing new and material happened and nothing needs the user's action (for example, you "
              'are still waiting on an external party, a scheduled check, or a background process), reply with exactly '
              '[SILENT] and nothing else. Never post status like "no change", "still waiting", plain acknowledgements, '
              '"next check at", or what you did not do.\n'
              'When something changed, report only the new fact or needed action briefly, in one or two short lines. '
              'The FIRST time you claim the goal is done, include an Evidence section with exact proof and make that '
              'claim visible above it. Evidence may be a collapsed <details><summary>Evidence</summary>...</details> '
              'block where the platform renders toggles; keep exact identifiers and output lines verbatim in '
              'backticks. Make a done claim visible again only when its substance changed because new evidence, new '
              'state, or a changed blocker appeared. If you already reported done and nothing material changed since '
              'that report, reply with exactly [SILENT] and nothing else; the runtime will still re-check completion. '
              'A restart, update, or new release by itself is not news, and re-running a check that returns the same '
              'result as your last visible report counts as unchanged; reply with exactly [SILENT] and nothing else. '
              'The FIRST time you are blocked or need user input, say so visibly. While the same blocker or input need '
              'is unchanged, reply with exactly [SILENT] and nothing else.\n'
              'Continue working toward the outcome above. Take the next concrete step. Stay within the stated '
              'boundaries and do not violate the constraints. Your method may change in the plan without changing the '
              'contract. goal_set action=revise is additive only. If an enrolled criterion is contradictory, obsolete, '
              'or already falsified by history, name the conflict and propose prospective verification through '
              'goal_set action=replace, preserving all unrelated requirements and hard limits. Use the latest real '
              'user instruction verbatim when it authorizes replacement, otherwise request the precise session-control '
              'approval card. Do not ask the user to clear the goal or compose a slash command. Explicit /goal remove '
              '<kind> <N> remains available for user-directed removal. Before claiming the goal is done, audit each '
              'Verification item against current state and end with an Evidence section that quotes exact identifiers '
              'or output lines from tool results in backticks (commit SHAs, run ids, URLs, `N passed` lines), so the '
              'runtime can locate them. If you hit the stated stop condition or are otherwise blocked and need user '
              'input, say so clearly and stop.',
  'source_commits': ('b2628bda1a8279dbb74e7bd6ecc82fb2e077e865',)},
 {'formatter': 'contract',
  'template': '[Continuing toward your standing goal]\n'
              'Goal: {goal}\n'
              '\n'
              'Completion contract:\n'
              '{contract_block}\n'
              '\n'
              'Check the CURRENT state now; re-check fresh and assume nothing from earlier wakeups. If since your last '
              "visible update nothing new and material happened and nothing needs the user's action (for example, you "
              'are still waiting on an external party, a scheduled check, or a background process), reply with exactly '
              '[SILENT] and nothing else. Never post status like "no change", "still waiting", plain acknowledgements, '
              '"next check at", or what you did not do.\n'
              'When something changed, report only the new fact or needed action briefly, in one or two short lines. '
              'The FIRST time you claim the goal is done, include an Evidence section with exact proof and make that '
              'claim visible above it. Evidence may be a collapsed <details><summary>Evidence</summary>...</details> '
              'block where the platform renders toggles; keep exact identifiers and output lines verbatim in '
              'backticks. Make a done claim visible again only when its substance changed because new evidence, new '
              'state, or a changed blocker appeared. If you already reported done and nothing material changed since '
              'that report, reply with exactly [SILENT] and nothing else; the runtime will still re-check completion. '
              'A restart, update, or new release by itself is not news, and re-running a check that returns the same '
              'result as your last visible report counts as unchanged; reply with exactly [SILENT] and nothing else. '
              'The FIRST time you are blocked or need user input, say so visibly. While the same blocker or input need '
              'is unchanged, reply with exactly [SILENT] and nothing else.\n'
              'Continue working toward the outcome above. Take the next concrete step. Stay within the stated '
              'boundaries and do not violate the constraints. Your method may change in the plan without changing the '
              'contract. goal_set action=revise can only add requirements. If an enrolled criterion is contradictory, '
              'obsolete, or already falsified by history, name the conflict and propose prospective verification '
              'through goal_set action=replace, preserving all unrelated requirements and hard limits. Use the latest '
              'real user instruction verbatim when it authorizes replacement, otherwise request the precise '
              'session-control approval card. Do not ask the user to clear the goal or compose a slash command. '
              'Explicit /goal remove <kind> <N> remains available for user-directed removal. Before claiming the goal '
              'is done, audit each Verification item against current state and end with an Evidence section that '
              'quotes exact identifiers or output lines from tool results in backticks (commit SHAs, run ids, URLs, `N '
              'passed` lines), so the runtime can locate them. If you hit the stated stop condition or are otherwise '
              'blocked and need user input, say so clearly and stop.',
  'source_commits': ('a106bbe2aaeca9dcb56e7fd5ef2303c901c4070b',)},
 {'formatter': 'subgoals',
  'template': '[Continuing toward your standing goal]\n'
              'Goal: {goal}\n'
              '\n'
              'Additional criteria the user added mid-loop:\n'
              '{subgoals_block}\n'
              '\n'
              'Continue working toward the goal AND all additional criteria. Take the next concrete step. If you '
              'believe the goal and every additional criterion are complete, state so explicitly and stop. If you are '
              'blocked and need input from the user, say so clearly and stop.',
  'source_commits': ('b1ccce898b5f7c4112658f5c0aebb731c7be080f', '8f19078c6ad72300676376f5824fcf50cd9b693b')},
 {'formatter': 'subgoals',
  'template': '[Continuing toward your standing goal]\n'
              'Goal: {goal}\n'
              '\n'
              'Additional criteria the user added mid-loop:\n'
              '{subgoals_block}\n'
              '\n'
              'Check the CURRENT state now; re-check fresh and assume nothing from earlier wakeups. If since your last '
              "visible update nothing new and material happened and nothing needs the user's action (for example, you "
              'are still waiting on an external party, a scheduled check, or a background process), reply with exactly '
              '[SILENT] and nothing else. Never post status like "no change", "still waiting", plain acknowledgements, '
              '"next check at", or what you did not do.\n'
              'When something changed, report only the new fact or needed action briefly, in one or two short lines. '
              'Claiming done (with an Evidence section) and being blocked or needing input still require a visible '
              'reply.\n'
              'Continue working toward the goal AND all additional criteria. Take the next concrete step. If you '
              'believe the goal and every additional criterion are complete, state so explicitly and stop. If you are '
              'blocked and need input from the user, say so clearly and stop.',
  'source_commits': ('a106bbe2aaeca9dcb56e7fd5ef2303c901c4070b', '11aa16592efeec426e9869e8fc5b3b23f2d9a400')},
 {'formatter': 'subgoals',
  'template': '[Continuing toward your standing goal]\n'
              'Goal: {goal}\n'
              '\n'
              'Additional criteria the user added mid-loop:\n'
              '{subgoals_block}\n'
              '\n'
              'Check the CURRENT state now; re-check fresh and assume nothing from earlier wakeups. If since your last '
              "visible update nothing new and material happened and nothing needs the user's action (for example, you "
              'are still waiting on an external party, a scheduled check, or a background process), reply with exactly '
              '[SILENT] and nothing else. Never post status like "no change", "still waiting", plain acknowledgements, '
              '"next check at", or what you did not do.\n'
              'When something changed, report only the new fact or needed action briefly, in one or two short lines. '
              'The FIRST time you claim the goal is done, include an Evidence section with exact proof and make that '
              'claim visible. Make a done claim visible again only when its substance changed because new evidence, '
              'new state, or a changed blocker appeared. If you already reported done and nothing material changed '
              'since that report, reply with exactly [SILENT] and nothing else; the runtime will still re-check '
              'completion. The FIRST time you are blocked or need user input, say so visibly. While the same blocker '
              'or input need is unchanged, reply with exactly [SILENT] and nothing else.\n'
              'Continue working toward the goal AND all additional criteria. Take the next concrete step. If you '
              'believe the goal and every additional criterion are complete, state so explicitly and stop. If you are '
              'blocked and need input from the user, say so clearly and stop.',
  'source_commits': ('e07a10459b74a2edc93b79c1617608d34fdfe13f', '3e4f2d53c3b5dc681a31904dac89b6d472f4ea1a')},
 {'formatter': 'subgoals',
  'template': '[Continuing toward your standing goal]\n'
              'Goal: {goal}\n'
              '\n'
              'Additional criteria the user added mid-loop:\n'
              '{subgoals_block}\n'
              '\n'
              'Check the CURRENT state now; re-check fresh and assume nothing from earlier wakeups. If since your last '
              "visible update nothing new and material happened and nothing needs the user's action (for example, you "
              'are still waiting on an external party, a scheduled check, or a background process), reply with exactly '
              '[SILENT] and nothing else. Never post status like "no change", "still waiting", plain acknowledgements, '
              '"next check at", or what you did not do.\n'
              'When something changed, report only the new fact or needed action briefly, in one or two short lines. '
              'The FIRST time you claim the goal is done, include an Evidence section with exact proof and make that '
              'claim visible above it. Evidence may be a collapsed <details><summary>Evidence</summary>...</details> '
              'block where the platform renders toggles; keep exact identifiers and output lines verbatim in '
              'backticks. Make a done claim visible again only when its substance changed because new evidence, new '
              'state, or a changed blocker appeared. If you already reported done and nothing material changed since '
              'that report, reply with exactly [SILENT] and nothing else; the runtime will still re-check completion. '
              'The FIRST time you are blocked or need user input, say so visibly. While the same blocker or input need '
              'is unchanged, reply with exactly [SILENT] and nothing else.\n'
              'Continue working toward the goal AND all additional criteria. Take the next concrete step. If you '
              'believe the goal and every additional criterion are complete, state so explicitly and stop. If you are '
              'blocked and need input from the user, say so clearly and stop.',
  'source_commits': ('c1bc50da497da9d430ab71dbe723ce19fb92e6ad', '5dc1a7f693d97c02e73e6f291d222c693ff60515')},
 {'formatter': 'subgoals',
  'template': '[Continuing toward your standing goal]\n'
              'Goal: {goal}\n'
              '\n'
              'Additional criteria the user added mid-loop:\n'
              '{subgoals_block}\n'
              '\n'
              'Check the CURRENT state now; re-check fresh and assume nothing from earlier wakeups. If since your last '
              "visible update nothing new and material happened and nothing needs the user's action (for example, you "
              'are still waiting on an external party, a scheduled check, or a background process), reply with exactly '
              '[SILENT] and nothing else. Never post status like "no change", "still waiting", plain acknowledgements, '
              '"next check at", or what you did not do.\n'
              'When something changed, report only the new fact or needed action briefly, in one or two short lines. '
              'The FIRST time you claim the goal is done, include an Evidence section with exact proof and make that '
              'claim visible above it. Evidence may be a collapsed <details><summary>Evidence</summary>...</details> '
              'block where the platform renders toggles; keep exact identifiers and output lines verbatim in '
              'backticks. Make a done claim visible again only when its substance changed because new evidence, new '
              'state, or a changed blocker appeared. If you already reported done and nothing material changed since '
              'that report, reply with exactly [SILENT] and nothing else; the runtime will still re-check completion. '
              'A restart, update, or new release by itself is not news, and re-running a check that returns the same '
              'result as your last visible report counts as unchanged; reply with exactly [SILENT] and nothing else. '
              'The FIRST time you are blocked or need user input, say so visibly. While the same blocker or input need '
              'is unchanged, reply with exactly [SILENT] and nothing else.\n'
              'Continue working toward the goal AND all additional criteria. Take the next concrete step. If you '
              'believe the goal and every additional criterion are complete, state so explicitly and stop. If you are '
              'blocked and need input from the user, say so clearly and stop.',
  'source_commits': ('a106bbe2aaeca9dcb56e7fd5ef2303c901c4070b', '511d3b72b438b4a9005c8b9d171fd6b5d649175e')},
 {'formatter': 'gate_failed',
  'template': '[Continuing toward your standing goal — a quality gate failed]\n'
              'Goal: {goal}\n'
              '\n'
              'The quality gate command below must pass before this goal can be declared done, and it just failed '
              '(attempt {attempt}/{max_retries}):\n'
              '  $ {command}\n'
              'Exit code: {exit_code}\n'
              'Output (tail):\n'
              '```\n'
              '{output}\n'
              '```\n'
              '\n'
              'Fix the underlying problem so this gate passes, then re-run it to confirm. Do not declare the goal '
              'complete while any gate fails. If the gate itself is wrong or cannot pass, say so clearly and stop.',
  'source_commits': ('a106bbe2aaeca9dcb56e7fd5ef2303c901c4070b', '6e041d524439b69ddfec73398816f8a7b01edfa0')},
 {'formatter': 'revisions',
  'template': '\n'
              '\n'
              'This goal has been revised. Each earlier requirement listed below still binds you unless the user '
              'message cited for that revision plainly instructs that specific change. When in doubt, honor the '
              'earlier requirement.\n'
              '{revision_lines}',
  'source_commits': ('a106bbe2aaeca9dcb56e7fd5ef2303c901c4070b', 'c6721afec7b627430f6b43c322539cb85eb176a6')},
 {'formatter': 'revisions',
  'template': '\n'
              '\n'
              'This goal has been revised. Requirements marked superseded (no longer binding) are audit history only, '
              'not obligations for the current goal. Each remaining earlier requirement still binds you unless the '
              'user message cited for that revision plainly instructs that specific change. When in doubt, honor the '
              'earlier requirement.\n'
              '{revision_lines}',
  'source_commits': ('46824125f365d6220cc391378bb4398faa13b1cb', '0316e624e2b0f34898769878c32e2a2ba2ef502c')},
 {'formatter': 'revisions',
  'template': '\n'
              '\n'
              'This goal has been revised. Requirements marked superseded (no longer binding) are audit history only, '
              'not obligations for the current goal. Each remaining earlier requirement still binds you unless the '
              'user message cited for that revision plainly instructs that specific change, or the revision records '
              'concrete evidence that the requirement is obsolete or impossible. An evidence-backed change counts only '
              'if your recorded tool results support that evidence. When in doubt, honor the earlier requirement.\n'
              '{revision_lines}',
  'source_commits': ('0c592eb2d9fc0177acd803ab73295c48932a159c', 'd832cf5c079640c6d2de0d450c1b4f4f979c317c')},
 {'formatter': 'revisions',
  'template': '\n'
              '\n'
              'This goal has been revised. Requirements marked superseded (no longer binding) are audit history only, '
              'not obligations for the current goal. Each remaining earlier requirement still binds you unless the '
              'user message cited for that revision plainly instructs that specific change, or the revision records '
              'concrete evidence that the requirement is obsolete or impossible. Evidence-backed changes remain '
              'superseded unless later recorded tool results contradict that evidence. When in doubt, honor the '
              'earlier requirement.\n'
              '{revision_lines}',
  'source_commits': ('733e8786e4e1b30f6f4f29e084b7341b1b09a1b3', 'b1eaccd9e235fdece7d602f525f7d7fb6e89923b')},
 {'formatter': 'revisions',
  'template': '\n'
              '\n'
              'This goal has been revised. Requirements marked superseded (no longer binding) are audit history only, '
              'not obligations for the current goal. Each remaining earlier requirement still binds you unless the '
              'user message cited for that revision plainly instructs that specific change, or (for verification text '
              'only) the revision records concrete evidence that the requirement is obsolete or impossible. An '
              'evidence-backed change counts only if your recorded tool results support that evidence. Removing an '
              "additional criterion or subgoal always requires the user's explicit quoted instruction. When in doubt, "
              'honor the earlier requirement.\n'
              '{revision_lines}',
  'source_commits': ('07ff4afe2c23f111b696bb040176716d7978d26b', '39266896ca575f94c3e7c62aad756e9425cbaf6f')},
 {'formatter': 'revisions',
  'template': '\n'
              '\n'
              'This goal has been revised. Requirements marked superseded (no longer binding) are audit history only, '
              'not obligations for the current goal. Each remaining earlier requirement still binds you unless the '
              'user message cited for that revision plainly instructs that specific change, or (for verification text '
              'only) the revision records concrete evidence that the requirement is obsolete or impossible. An '
              'evidence-backed change counts only if your recorded tool results support that evidence. Removing an '
              "additional criterion or subgoal always requires the user's explicit quoted instruction. When in doubt, "
              'honor the earlier requirement.\n'
              '\n'
              '\n'
              'This goal has been revised. Each earlier requirement listed below still binds you unless the user '
              'message cited for that revision plainly instructs that specific change, or (for verification text only) '
              'the revision records concrete evidence that the requirement is obsolete or impossible. An '
              'evidence-backed change counts only if your recorded tool results support that evidence. Removing an '
              "additional criterion or subgoal always requires the user's explicit quoted instruction. When in doubt, "
              'honor the earlier requirement.\n'
              '{revision_lines}',
  'source_commits': ('915777a8321b4186faad8e87e7fc00077a6bcca9', '47afd1c2e0009b4b519180e55ef0b0a572b0c848')},
 {'formatter': 'barrier_lift',
  'template': '[Goal wait lifted: ',
  'source_commits': ('a106bbe2aaeca9dcb56e7fd5ef2303c901c4070b', '847864a02221e4cbbe6f722db9c09af51dcf4d9f')},
 {'formatter': 'barrier_lift_note',
  'template': '[Goal wait lifted: pid {note_field_0} has exited. Verify its result before continuing.]',
  'source_commits': ('a106bbe2aaeca9dcb56e7fd5ef2303c901c4070b', 'd3c3d03f1709538a380dd9071621e6bedc912f79')},
 {'formatter': 'barrier_lift_note',
  'template': '[Goal wait lifted: pid {note_field_0} is still running after Check its progress before waiting again.]',
  'source_commits': ('a106bbe2aaeca9dcb56e7fd5ef2303c901c4070b', '5022441c66a78d8c7a563c241f9a315695fed752')},
 {'formatter': 'barrier_lift_note',
  'template': '[Goal wait lifted: background process {note_field_0} is still running after Check its progress before '
              'waiting again.]',
  'source_commits': ('a106bbe2aaeca9dcb56e7fd5ef2303c901c4070b', '5022441c66a78d8c7a563c241f9a315695fed752')},
 {'formatter': 'barrier_lift_note',
  'template': '[Goal wait lifted: pid {note_field_0} is still running after {note_field_1} minutes. Check its progress '
              'before waiting again.]',
  'source_commits': ('eb37323b4dbcd047211c1aa1565ea95b63c91579', 'd3c3d03f1709538a380dd9071621e6bedc912f79')},
 {'formatter': 'barrier_lift_note',
  'template': '[Goal wait lifted: background process {note_field_0} is still running after {note_field_1} minutes. '
              'Check its progress before waiting again.]',
  'source_commits': ('eb37323b4dbcd047211c1aa1565ea95b63c91579', 'd3c3d03f1709538a380dd9071621e6bedc912f79')},
 {'formatter': 'barrier_lift_note',
  'template': '[Goal wait lifted: background process {note_field_0} finished ({note_field_1}, exit {note_field_2}). '
              'Read its output with the process tool before continuing.]',
  'source_commits': ('a106bbe2aaeca9dcb56e7fd5ef2303c901c4070b', 'd3c3d03f1709538a380dd9071621e6bedc912f79')},
 {'formatter': 'barrier_lift_note',
  'template': '[Goal wait lifted: background process {note_field_0} is no longer tracked and its outcome is unknown. '
              'Check its output or artifacts and verify the real state before rerunning; do not assume it succeeded.]',
  'source_commits': ('a106bbe2aaeca9dcb56e7fd5ef2303c901c4070b', '2cdfc5934d369ed84736915b7da12795d4cfaeae')},
 {'formatter': 'barrier_lift_note',
  'template': '[Goal wait lifted: background process {note_field_0} is no longer tracked, most likely because the '
              'gateway restarted. Its outcome is unknown; verify the real state before relying on it and do not assume '
              'it succeeded.]',
  'source_commits': ('582e6912aeb2cbf902d4f4047efa56d5830d1db4', 'd3c3d03f1709538a380dd9071621e6bedc912f79')},
 {'formatter': 'barrier_lift_note',
  'template': '[Goal wait lifted: background process {note_field_0} {note_field_1} before it finished (exit '
              '{note_field_2}). Its result is incomplete; check it with the process tool and verify the real state '
              'before deciding whether to rerun that work.]',
  'source_commits': ('a106bbe2aaeca9dcb56e7fd5ef2303c901c4070b', '2cdfc5934d369ed84736915b7da12795d4cfaeae')},
 {'formatter': 'barrier_lift_note',
  'template': '[Goal wait lifted: background process {note_field_0} was killed by {note_field_1} before it finished '
              '(exit {note_field_2}). Its result is incomplete; check it with the process tool and verify the real '
              'state before deciding whether to restart that work.]',
  'source_commits': ('582e6912aeb2cbf902d4f4047efa56d5830d1db4', 'd3c3d03f1709538a380dd9071621e6bedc912f79')})

# Current formatter names are the guard-test contract: a wording change must first be added to
# the generated history snapshot before the classifier can be considered complete.
CURRENT_GOAL_FORMATTER_NAMES: Final[tuple[tuple[str, str], ...]] = (
    ("autonomy", "AUTONOMY_CONTINUATION_TEMPLATE"),
    ("plain", "CONTINUATION_PROMPT_TEMPLATE"),
    ("contract", "CONTINUATION_PROMPT_WITH_CONTRACT_TEMPLATE"),
    ("subgoals", "CONTINUATION_PROMPT_WITH_SUBGOALS_TEMPLATE"),
    ("gate_failed", "CONTINUATION_PROMPT_GATE_FAILED_TEMPLATE"),
    ("barrier_lift", "GOAL_WAIT_LIFTED_NOTE_OPEN"),
)

GOAL_PROMPT_REGISTRY_COUNTS: Final[dict[str, int]] = {'autonomy': 1, 'plain': 6, 'contract': 12, 'subgoals': 5, 'gate_failed': 1, 'revisions': 6, 'barrier_lift': 1, 'barrier_lift_note': 10}

# All historical goal formatter versions contributing to the frozen registry. Individual rows
# name their newest and oldest source commits instead of repeating this complete list per row.
SCANNED_GOAL_PROMPT_COMMITS: Final[tuple[str, ...]] = ('9cb5068945488ffbdfa2d481c84c1243a0a45e1b',
 'ecf9e5a9eb7026ecc63708ee04af9965a36171e6',
 '48e3d0b325efa74934483f73925a93a9826cfb9a',
 'fc8eeea4a6fcc16b84d68db4dc9c089bcd902568',
 '511d3b72b438b4a9005c8b9d171fd6b5d649175e',
 'c1bc50da497da9d430ab71dbe723ce19fb92e6ad',
 '130983f173cf9a981b9fcc5bc561acee56f216dd',
 '3157a564eb15dee45072541a15d36770ce03d5a7',
 '3ff30c8d0029699d8a1f130d5e338d515694111a',
 '4f17526d5bf144a218ded092b7421857e4e0f01a',
 '09e54a7a733fc8e232cf3de489e0f318cdc374e6',
 'd0bb43fba40f826a29969b70441b477ef8bcc1c7',
 '4ea88827c9770b81e4c525aebc134b1b04fb8fc9',
 '1b6d840f3a03b39ef6b8e4b01912479b6e031107',
 '7f5a3330295a4f6512539881670f8a8d2873090b',
 '9c6ef8ccb99390e5aef5066886eea55503ed946c',
 '9cd7f22756d1ef74fa7575a6bafbc4b6e8b60fbb',
 'd28c84fff07bf6d0463eba619071d1ec5422d0ca',
 'ee6026a2dd0ed5e861d3160dfa837580d900d6a5',
 'fabcf2535aefe638fae398a0c6385cc6e7cc9868',
 'f929801a5dbd349149d11ce6322f79c9ae6701aa',
 '7c80503be82665f3a81b1bd53c9e5ee9ba6ae584',
 'a95b94dba01b33cd8f6a8edd9d7bc54626b50d53',
 '27fcf8d4d51a0b71ddb7e809a5cc48975fd1cd6f',
 '245b92100507b1a352991801ee0d1641f0d7f793',
 '512f39e0092eba69f87d7ff41a780e05ba2ac5e9',
 '02219d1937e92536b6b0ddbc14da12896788375d',
 'b4671f5e7cda0a4792eab493ce4e6915dcba405d',
 '66046715b503e9408e312e1be904fe31871c64d2',
 'dfd4965bc9bf346d50ce390e30f53e53c540818a',
 '426388b1599b789a4551da6a3946254373822cdf',
 '534a7b27db35d490f2f53e868825ffd521118c26',
 '91584695827dda5c426ff83990c71efd05bd81b2',
 '2313c09e1f6dfa5f3624e1df161cb430c25a4289',
 '53bea4d3bd78cc335a454836a71b7b4d604def02',
 '0c821f35251b070f28117188cfa7fd9cb86a3917',
 'e7264b6e18dcbee36063df35c932af5d63439a7c',
 'c0229915718ab873587ae04ccf9ecadbd1527ad9',
 '10cc2599359e6dd559351b8e88946eb2f1e06743',
 '13aa69b0350e5d61cd762153c53e4ce4f5392de7',
 '7a7ab568ca484988b73bb518f37da2dcfa7b45ed',
 'c4ed80e35fa6b561df01aa7b9adb41e9bfd27cd5',
 '1ceabf68cabce7f42d80f4d77e86b259fb7d4ce2',
 'ab7eec2f94aa2c0060087013cc0648c183b06b48',
 '9a1dee83b916429b3c4e7f79ad306c782653e35a',
 '1b6d17e52791b5ee87e81bf5da6304ad5601668a',
 'b36b3f5252f5e17c51c19f1e8b11bc5e8a639301',
 '2f1dfe4b45911c5c539b92c0f9831287ea4ba3a4',
 '7da99493b4a88cb76ee9d7c09485531074e43ece',
 'd8fd1b2712c7ccedec78ffc6b0f3a1ec76cb48a2',
 '9cc06055cd15082d201534604828c3f438071f0f',
 'de7c33e7fb3ab594d96f712f159e54f69cd0d89f',
 '80070e69182bbc616ee078a2ffecb53f1b26b10a',
 '6456990999d343123fce654e8a86b6232e0996e9',
 'f916cd27e841fc8411e51930527d6a1732ed9afe',
 'f3fc390f330d5f8cd9341b09b5362344b1547f56',
 '24fc80728daaee5dd2dd071d4d3a4f395ac5e4cd',
 'c8fff968c4d4ad445ba2eae1de1d74a4ea003d7d',
 '996fe9f91f30bb6da1e37cd6af3e5a3296c42dd2',
 'df74936df3049b18ac6a948af3c42f9a0db34ada',
 '6cf7983a46116fcaf3305417e1f26a527094d49c',
 'b5c111693a6e0551cc3ebca28605ffdb6d1d3dfe',
 'e971a4e4d8ff4d3954239eb0b7edc09c605e1cda',
 '0660d87a2b39ee4ea115e8b2780ff976bc198fe4',
 '085117881f2839d1ae6b8e303ae2bd4c37bf2057',
 '82c2601729f24b1e98146baad0c71126e6c99d4a',
 '08244ee11c5387fce5bc9dc30170c39816e2ae18',
 '510c0c3b536b7a56d3d2c4cd59503bd63f694c4d',
 '13ef41dcab8812a048727eed5d8f6f795d88717f',
 'c119867cc60e14c18db038a3c78e0643e2552337',
 '5d98dddcf17c6ffff6b18213c2e47d3909965e7c',
 '904b59f3b8e496bb4438e8e08e51423936fe50eb',
 '0c804b5706561b3234e7ab0a5cc8c53e6b71cc7e',
 '2e0de25e6580ffe95ae43b0cb1d90f7c00e3ae14',
 '4d5d1014819020b3b2232668f5ee0d61e0215e29',
 'c6db70a647cd732629e6b9db482b5996a6e10b9c',
 'a439f0c496939b09507b8327142949e84e951079',
 '4cbedd35eef99afe69aa33d195790f481b43ebfb',
 'fefbbad9167931668613e7b4289aa465e9e38b6d',
 '476354634f398411054fa97ba493699827306efb',
 '0cc32b9becb6fc8d6940e85114d9faea063a1ffb',
 '63619a31dcfa79842c9b1f5a8c2e607ea9727ce4',
 'ab57206f8a5716ae9fe4d5ca93a5f798c835f2bb',
 'cc4fb282216bc7b84ab8d6bd57cbb086e61607c6',
 '86017cdc7ba8c42cd26bd6aa714e4b3ab8c908d5',
 'a68a75aa5309daffdef8784d4642c0afcacc2764',
 'f1ad213b4c21334852e52b2d4d08b0cbfea9cf46',
 'a0ed752984ec0dec5e07d5dff944b3262dd91b47',
 '2cdfc5934d369ed84736915b7da12795d4cfaeae',
 '582e6912aeb2cbf902d4f4047efa56d5830d1db4',
 'cc32ef99e0603ff0c79a6a49292abcb2ecf500a4',
 'aaa646b747182adc4c2abe6176e0b31bb5ed9ff7',
 'ef2beecd1d327dc48b71d3c70f849400e8b130fa',
 '11aa16592efeec426e9869e8fc5b3b23f2d9a400',
 '46824125f365d6220cc391378bb4398faa13b1cb',
 'ae63f0cc701f51a3504dbac9ca94a29647814341',
 '291731741e67d8e440079f6c9d496491070a1dea',
 '4629286149fb5d2c7a1d4abb878a55851e9246aa',
 '21790d00f5d1e39ad3dc6000f3d118643360ae44',
 'ece0e9db0676e37669a35f8dca391ee55049514c',
 'e5bc781b7b3b1c7cd3d65457d831051c539d6731',
 '7784f02228f98ad431f47b53e99bbc72879d6f49',
 '6d6245e3006eb91b7d182f4127aafde361c758c6',
 '4f015563a80e0cf8a17e942e8588833144ff9c1b',
 '5022441c66a78d8c7a563c241f9a315695fed752',
 'eb37323b4dbcd047211c1aa1565ea95b63c91579',
 '7a54d7b6aa79f77a66c8f49a72634ba6fb0df7bb',
 'b7c6bcfde13749551312194ba5d8884e55b2b21a',
 '8a4849cb3e44964b55bdc35b09261a14620d653f',
 '3a743bdd9b17a0968677217d1ba29a68904fd9c7',
 '5ccc051dbaeb06aecd8514e47c1008d4bfb387a8',
 'b5e61fff88d6c08a01a956318b2a433edc124571',
 'f253b0a1e6186a02038338882761cdc3fe59ffec',
 '7b17d8838e77955dcef51e9c0c9bccf73a041d3e',
 '140ed73f258ccfb09ac37675155133a28a2145c7',
 '62c31c67cc966dbc065033d5a780e1dd1bc0c886',
 '3e64756cc28ec8b49fe01c28a1b7fcdb8a01a291',
 'e8d6c1fb5fbfbefd333221dae8d30a85efa2407c',
 'd0f7761cf12fb5433829153aaa535c9805d2189d',
 '0eba014271497247765691b7b8a29a23c4565352',
 '8b2b55a8bf532d510ed493484ffab32613df03f6',
 '8df46227d3e081d4c36bad5fae5cee3b1baed769',
 'f7b06b8e71508c2d99ed9562fd98a4dbd111c74e',
 '1dba1be6866d788d0e32b6decc3c5a3d1fb4fb2c',
 '79a9a976b5a84c55f7e26b3b99709887786fe73c',
 '10e3b865c27eb01d3bc77eb2a5a4ae030e4205e8',
 'a1cfbd8c05bae4a06038e1ca5afa544b2b7914e0',
 '15c32ccee01bb5f49c1caaaa8b9c34b2587ae750',
 '36229c2a25a2936da726cc8500246712ca3cbb3f',
 'eb1da1251b75efa4b4c3f9543fd828d2128193d5',
 'da8ca94f0a168631901f128472f0bb77a9b47a66',
 '9e50af4fc489694b9ddeef094b77197835dd1b1f',
 'a019495e1356a970867eb20388a55f30e60f50fe',
 'b1a04d992c2fba972e569ad4eae62e20c6b090f9',
 '800f2295f15fbf4376a92b7ad114208829807a21',
 '0c02087a8e5252e86de2ff735e038ace508f6465',
 '987081e7990902ad0183cf7abe46fea79579dce1',
 '19254e69b26184bb152af51738d940a1d3130d45',
 'd5c844b6cef3cdda287f9b93fae6421f8acf964b',
 '1aa0d7e7c0ecbce5e5036b415a36aa7d074009d3',
 '0316e624e2b0f34898769878c32e2a2ba2ef502c',
 'c66ae73dff88560658286501316e672129a6454a',
 'f6190a90c85479f8a9abc4aceb83bc8efe383d8c',
 '6122d7fc4061df652326d9e30d5d4125136a9cc3',
 '974399844654de4db9411f8cf01dd437b8b194c5',
 '48a0797097293f5a50e4a83225389415261f5540',
 'b168c831a486660de2a7c14ba3b16877ef130159',
 '05ac7885fe9781498723a64726febe9d2daa400f',
 '22e4b23fea48cee719ea2adf298c84c63dec26a3',
 '186bb661aaa9a9f5344fc0e8a53748c27a29602f',
 '3d8900187b21d1fb8af850438430be9843a83cce',
 'f41bf93c9ddc93cbad79e13b60302b17fb365440',
 'af8491a0fd78f97d7f125a58ec9a9e892f0eddf0',
 'ad9f46770d619e19925e7d75fc6918ee519136b7',
 '5b9448345321162cfe380aa6eb13703dc95ed1be',
 '5dc1a7f693d97c02e73e6f291d222c693ff60515',
 'e07a10459b74a2edc93b79c1617608d34fdfe13f',
 '6a67e5f73372bed8098549c30b188f65de3e0544',
 'c5183ef5c1e53f011f3ff9d34df5125d6c1e9e39',
 '1c325bfcfec9bfbd41e943f5114f09def1440e98',
 'ba06babef17077bc8e3d2ac243e5c6f3b82aa255',
 'dc5c20a8b8c9de4755425f5061ce7f2044c6e91a',
 'd7bc0e2c04cb986885b02dd7473255279fb4818f',
 '05132528ac722c97f152db8f1909095915929149',
 '7e1f69faa7924baf15dd4f46fe01b3490e30c306',
 '402507d468871ffd5c0fac6384a4c6b988640192',
 '37976efec372fa6c5c170e01df4bd974968fdbd7',
 '69c46c47c7af87dd8bb7b689bae2e2bcfeac4df9',
 '94dbbb143de6151f2e53076d94af190c59b0c57b',
 '3e4f2d53c3b5dc681a31904dac89b6d472f4ea1a',
 'b1ccce898b5f7c4112658f5c0aebb731c7be080f',
 '9456670d2f9e6936d3b53e23cecc6a702bd0a365',
 '0006a5e229aedbe6144d6b6140e2471dba8b738d',
 '408cc0d12ee32758e67aef9934176e9e3960408a',
 'cf23a1a5cc3e02b2a2d4526b4a1ac66ffaac614b',
 '4b30ad428235560f3ff672b07452c54a10a15d7e',
 '040dbb9053da214c1babef0024d1a1fd7fccfdd8',
 '2ff58849f4c4cc27253588ef03d7aed940d6b7da',
 '9eab688b429b0088229868ee681cf8838ddef92a',
 'ac6e7cc1c23c83d3c1d08ab727546125b946f9e0',
 '607a23e858a2d5c050d7645d0f24d09dc6c849fb',
 '8ac5c74432d1f217033993c8370e911b2c91b04a',
 '65bec86e37aef5a3058bff58ce9eaedd9e3796ef',
 '74f1f17a7ef4653beaa2344eb90daaf541f3e2a5',
 '302c5d9f679333ae957c256d76a3dff35120f4e0',
 'd76cce4f1161bdac895f7e373ce41cbd04e7c2f2',
 '1e500ebce2a9ba5951ee82e7b93795bb39b3ffb1',
 '3724ace0c881a583c56f49760e754e7e0848f3fa',
 '242244bf6861c86f49b2d3e61ad647c7b4c0cc06',
 '238967f797778a1bce2d87afcb16bd568ce8e2e5',
 'f42f579cf8bac4918ac9599bece71618afadd846',
 'c0c6fbaed171ec7251bfa1fc0f6448b865752f6b',
 '8cd3d8149ead2b86aad825a539b1efabf7781963',
 '42369fba971574f9d8bfa544693063e9efbc8657',
 '71e971b8cee3cae49ba1e3214b1adafbb5963690',
 '7bbca1b8ffa4c1504b551fff8d40ee3f05555376',
 '2d9e11b874a029529646cc209c838d67aca58e61',
 '3e04170780f39914db857db072917c073e9f9d2a',
 '532cedda2a214d4ed5e0ccd5c103a1b550c8f7b5',
 '4b623780571ae058346a94914a989cc898bfb01a',
 'f5448560e9543a862d6a078f8521cbd33c2c172b',
 'c3a45cd54263a39628dbc47305de4a940e5193fb',
 'dcced85d36750d47181a2e703c61baef32974cb7',
 '9423f0770bf94bbf5b161945e436b9ae7d8bf5fe',
 '7707c83eb59588d9893a2f34d539be0d06e3ffe1',
 'd3c3d03f1709538a380dd9071621e6bedc912f79',
 '9afdc96125aff6d4df0a2c78bc28c73686708d90',
 '3d96dcbf1078c3c914795d181d33a1ef59a9d764',
 'e6c8ffc6809a48c3b5c7e64d6634b0db005121a4',
 '37220fb789af162d3257aaba3084c0207367e428',
 'b4bb6d62c8c62d74e84a283c0be99796edd7f2b8',
 'c0a9187b89a0da093dce8a026c1d6a0039a21be6',
 '11c6309d26e78c2e74118e84e2272d1423ccc4e4',
 '7e6dc0aebda812e3f57319586be488679d349a68',
 '8a6efb7bdd8021301cd98c9a043f0c94d0dfc720',
 '24a7b943d941af630a7a97e48c11f3ef8845151f',
 '1ffa8d28fc01faa4cff7fa22b20d9cd1bd0d8de0',
 'b23e72bd7cf495908f6fe2a15d66524fb26a9049',
 'db2e5de6f58f638421c848a3986c7a52045a6fce',
 'c374d7e44f834ab7d56ef0f01882bed0c818a632',
 '382a080ff1bde848a7cb55bd31c97fc7939f1ed6',
 'ba280ac2425aea25683d3d0a7d493fbc024727f7',
 '5762789b25fc68789d94e3baafcae21e58d8e386',
 'd690ef03a2ff9bfa58b7e69fce53ca5dffef467b',
 'e2a52052e2d1263f1719876afe8d50b14a7cb2f9',
 '47ce9c0097b99b1b67ab4981dfe6b1372ea1d34f',
 '8753d1e980d1cef1fb40aa345726b3717dcb1088',
 '7de8728cba339065329f141cf92686bf06d2c171',
 '189220cb42610c5c3c74aacee14aff1c65a6c281',
 '444dd0af3b56e90f259890e3f2c1a0e0fe120b2c',
 '97de4b4ef8a1fca59fac2e6d860ee12fcbae81ee',
 '85cb540e6dfbcbbabc78a5b93ef7e57d206a5aba',
 '00c45c084ddbd8f2fd868908616ecf04c2260e11',
 'eec5bd667387b4237c7b4a993371e178973501a3',
 '2dcfb8b9c88fe48d13d1c3c4f713c43442aa196d',
 'e89f8bca6c8b8258702f4cc79cc2ff4e9f3b3116',
 '7381ad36ae43a6c9e04b10daa08a975eb2eecb7d',
 '176392b0cb3c06b4d5d99579e6ca1b500eaae40b',
 '7cbc54d44a470fe6a9ba77c7cdc44a52323405eb',
 '7444ec340448eba53b1c9ac3fdda29dd8ba709e1',
 '3225f7c3f2dd63ab5a279da1d1ba4c1f194393bf',
 'aeab29e22d049b82ea8143bb1eca96132e513721',
 '53403f150f7818971e17cdefa32b35524988b50f',
 'a6ae6ace518f3d27571f0c333fcf665801e2ab42',
 'b1be493b41f721681eff091a9d644fc115d51564',
 '2c78f88666c054f74d82bf8667921d5b89cff199',
 '6b457953f61c9e9bd7e9122329df2ef9253e4f50',
 '9ee7da83fa2156bd10f744334299eb4bb2ddffb1',
 '41fe679d96ee640ae35beaca264b2fba70c3eb5a',
 'b4a294fff944aacf33886a7f882545150811bfbe',
 '4ff64a1d06eb8e1e35717362ad2ee826c46c3abe',
 '1539a1a9dabd1fcc80404fbd7d8db4b1a504c2f3',
 '1a4d8761769f22f2930bdee65d23a977d7296dfe',
 '43c477571e9b759be2926505afcd42a04ba5dfe2',
 'bb745a0e9bf0c055132ff1163ad844ecf1615355',
 'b55767be82b4f42fc5d748baf7a47c182df7b6b0',
 'ee07fcd4d716959f1701ec95cc52bce5b4f3b8f1',
 'c70db196eb89035827045cbd5aaafd6fafaaecf2',
 'e4f591d1b0ffa996e81161bace5de8b9de1b25ba',
 '30d78cd85eef490262f4272f81c712b523a28e62',
 '52cf71d037f30826a006517000adb3ab94766685',
 '612d542281bde96a033898736cb58b06650fc7db',
 '2d23cfcf698dd413ed47ac5a67cf94bec516542a',
 '72617cecfc02791dbee4588be40cb6493e2cb1c6',
 '84f59e2064d77a205efc92fae81a4e76bcb2d39d',
 'b17aa114d2d0be8a5cc96b76ffa6df91686d3f3e',
 'e8af4c06107fe63e2fe45f46d039b3c26b43d304',
 'cc01ae9d44a099eb7e3297b442ba78b18a020076',
 '6806a3808453f4a8374f4d5692060f3bf556fa46',
 'b3bfc3afe5af843afa0fc8106f6c9b879f2e5393',
 '8d79c2ff57bba4b07e5b37ed90387b16541aef53',
 '05d705dd695d1084388529124dc2ffe5ce919e89',
 '3868ee9a128362867ba1a8d4f1fe477163b4e905',
 '7e2c7e15814df08b29e725846b126460e6dc7cea',
 'aeecb110f81b9b1af14adcc2fbcacc9c97de2552',
 'd47adec28fa5bcc48d71cbd5e6293792275fd36d',
 '15860dfe9f3e6398ca207741396b8eccfa962fea',
 '32ed2061bbda073298e8ed0276764136c009cf9d',
 '171f284f8d1fcf2cbc9cab6cf9b626d456fdf2b9',
 '37650f810c5f71ea137ac54eb1fc2f3b9b388499',
 '0b30c2484df0f95eb7cca80eaf1a8bd0c63eba5e',
 'a27cd5902a446eb4f1e457e09904d75edc616c2e',
 'dec1e87833d00e6133a89ef94cc352756f95e238',
 'd2c14f4becf94f981cd0c0d0d1c0ff608f4f50b3',
 'd0090a147c3cc6d5b70663e4c1f012ae4cac4888',
 '28747b4034cf6934bb7506e760d82baf5457b5c2',
 '6d9c16645599122d2dbac7ae689b6f73c4cd5fda',
 'fdd140557b65609ce19095916398f72e1ce8fc1b',
 'cd658b6a46595624ebc82fe12e2a5e4b62b7019e',
 '634109b7aa948f22f00c7bbca1ef8d0efe641bec',
 'afb4e080f640b313153d832e7e14fbc96577fd54',
 '94779d502b70fcd21754bd571d77cec59b8b204f',
 'd3fdcef13232d01a42e63a90873e783d285e6266',
 '8477292f0f4e3ee5e9095fa6faf64482790d5621',
 '50a13d53eb8792829adf20bb7457607a66c6dc69',
 '05a18c1333e77615abaef8d0177a1777ea8721c8',
 'f8b87f5637653ca64391e70248d497ab5ec7efc6',
 'e8fcb007b9225b3d8e99d5eebaa36eaa666d071e',
 'd3630f853239e8c41ce7201e09fbdf39bcbc5431',
 '642579db60bd2a244ffb192e3d571d3390f1191b',
 'e83816a4d1998245968949e88fa15f26d89800c0',
 '022785a5413a645d8b74730bd24cc808a7131593',
 'd38bb1b3aa0202a992d0d446d214f24a67935795',
 '8ae52dd93c4c8d4690938b9d373a30667e7b8bb5',
 '93cf6b3dce6763d47e366c794a07c9d8a4fa5eb0',
 'e73598a3c377484fcecc8ca3121b3e09ae9bacff',
 '8c4012fac0bf60f91483dbc3718c624765f83cd1',
 'e010327ff7b93490070cf5d1f66ae8b82170fc06',
 '3bff2a581dc0d8dac0b940ca147e1321c9c578bc',
 '0071ba99658492b977fe1ec86e7da702b1d323bb',
 '75934b89c872ca8b924f224189aecd0a8be16cd8',
 '6b393f70eb732acef70907e503f8beb6a2301779',
 '61df062534fe968e7641382b478847c19225491e',
 '0d336d055ac83f32751d56a41240a4b9882df052',
 '011bbf0cd6b4c820fa5368a83b9c6bd0045efcb8',
 '063ca416b3ec7ff09dd9a980524f873a910b5a5b',
 '4b0eaf0e54df75c12d180a931353211d2b8a2717',
 '6a1d96c3464e4326f416baf68f2bb889bc2caa4d',
 'ea6083dbfc51cfb6614df6d54fe9fdc680c9896a',
 '5f9a4d3f02741a3f6c9eb137865e91a607f84be9',
 '1b8da6e5eb6aa5eadd98b7b6f1d80e9fa9aba7e6',
 '74e41df6e7164e9cacf6bc2fde1802da78a537b9',
 'e52b0ba9308a400868f0818b8975fc85ac2e8574',
 'db070745fc3fad3a67c30343aec3ee17d023ba84',
 '4dc7737ab3305a8ca7c40eed2d4e89b5a49acd5a',
 'c2f889310cce305564d082f4ff18823c6f9555e3',
 '894352b96f8d702cbddb0e760673e7bf5d578d0f',
 'e6c2f81c51d6887fdb8fcf4d94ee92952372a2c6',
 '9bb09ec596817e8b5eb283bbc748d8650e7ab8eb',
 'e7be554f6520f1a4612e06794b53f678ff692c27',
 '725ec21b282790022cca74cac5d532a513a43d6d',
 '06c8ececb7f5906b349aa63d3b5ddb06eef21755',
 '8b21165e8e4b5b2f35bd5e447b50cb37dddac8cb',
 '0e8aca67ac6eb1874edc46c9003ece2efa7f4327',
 '4ad49247e129de4c616b720a1b6076b6bf5f2d74',
 'e88ebcb0c79cb01bf8f03b262838f25762bafa4e',
 '3bda2a263dec13f39cb4e8909581304dcdc5bfec',
 '120b328961a1ab79faddef5ec4f25d9c9eb0eae5',
 '249638876d03c67442680edea18b23339450a76c',
 '962276913f3b4fae546c08db2ec8a895b524e845',
 '17436dd28f67040f48838ce37deeead6419a8ffa',
 'c684ab83c3e28dcd32009724db705e6ca538701b',
 '0a3bd72fdcc320ac4052b04c546a3b13cc2171cd',
 '5b220869a785247b5876e93c46283afcea27ea58',
 '72a8b3bbc8ee5e16a884335fb784848d5c793e40',
 'c32eb2c458550cbe9d1035865ced2e79319ecf99',
 '3b46faca73f812451bcbb98fb009e0441cd747c5',
 '7198da2eaf1ad24feeb5ca73aca2eac507bc131d',
 '034b72beaf4567e4d743a14de178258cbd3bf9a1',
 '47a0636bcf128e0a1c3a7f1df864898019b70822',
 '115495da4a3c5fbcd2be60bf57faf4dc612dce9b',
 'ce8ae9e3362d00b1f30f579bb3c249dde98f6c89',
 '6168a29783a6ffbe1b8eb8da3c2fddc3a96c41a2',
 '48700c5d9da6dd1097ccc1e2bb070f12a54b64a0',
 '0b8ffe8c7fa05f3a2f7af3959631758a8f9fee58',
 '0a38f6783b8e62251f33624c07b0dd50fc3b3ffc',
 '0152c3840d3532f6af4ff22abb5fd51264682854',
 '539c03b96b4c68ce3ad9a6556add81f64a99dfac',
 'de168dc3cc9556b7620c783345ad3edd07a6ff70',
 'a6faffadd898deb86849e53534cd3586dd290455',
 '0d1ee539959ccbbcbc748e18ca3019731cd1a986',
 'da1288a6b1baf0572d0ea1637c902c1ca59b15d0',
 '4b5887993de3a05df5b2c9171dcadd557f36438e',
 'c1ffedf4382d477c6a8178b470f31b6f0f7a1183',
 'c190cf7943687e04638562965b56eddea4d39ebe',
 'fc5e5a1491c2ef7c00fc66130c76afad274610b8',
 'ecc23d5c32fbc58f5c035f79e910f0fd7d2944d3',
 'eeaf3e9a11509685492800897029186e3fc1f5da',
 '58b4cefa3dddf4f6cb168166696912e331c0cb16',
 'c3cdb4d8bfebe367eb78b8cdd2e4d7d65cce411a',
 '635aaed14130ef41d339e8c44b2af9557650e17b',
 'cebbc9ef97c7d6622b9062cf6ff5a239a85ac8ec',
 '8d4a925c9882916fb5a7de014e5c16736eb66f3a',
 'b6d9686faf5680480798912a9bf3d68ee41cec72',
 'a72c355438617ae4b414a2cc58c4079f2d4ddedc',
 '946b993243bcff42882bb816736473d85874b691',
 '8fb6eea22e6126215dea648a9353846024864f71',
 '3a5edf407a915cf064dce8a0c7d1413c794dac8a',
 'e73c88d170efae1485205b05c773808ace1bde03',
 '3c79d092910a5a31c752e655cb8ae5c14ca9ff36',
 '5b67e3169962eb390eb63dd28e22bfe88a4abb33',
 'ac6fdd34fdca8cbb13e5d6e8c6e4512f72bcdc52',
 '412a8c5ee14f9805d2a5b2fce90ced2495131e7b',
 '93df1caac3f05f846201a206ed20b3678c16d16b',
 'b42431f652e9088111e2de13a8b79bfb32a36717',
 '168cb6f4aac1c67adbdcf5f3317db883fa8f379d',
 '6a779136c173ed9ff2e8ee8099df2639d2ff583c',
 '14c03a65acb37e9c7e5459d50ba0da30b0e5dd8b',
 'cea2ed1bb559558c51c431dd7f8d1b352c0f9e00',
 '6a4564a4e8b7da7611abd74a1abda3378bb90f32',
 'eb5e20cc81a8c9fc0645e1dd79d4285af2e712a4',
 '579b1ded22a08b14e98a83511cd47b16afb80c02',
 '33ded28c5a5a500c80c4b1ac5fe242304adaee10',
 '2510ca5e9d1a9c103fcc6280f654055fbc6fcdf0',
 '92036756b58391849c83d67f03905a512bd85495',
 'db1a30b55ab1b69eb882f5c5de5fa7c9e40b4c96',
 '3caa7af1382a035b69f5515ce1a9157016c3baff',
 '60817f77573b3b8fc9f45e7d19b6f4cfb183c4e3',
 '6f661b4da6983ff6239fe0f9ef998c5c14531e78',
 '3c3511d872a6e40c135665f4a04bbe6b67599304',
 'fc886784790af0b896cdba3a0d9519d4f45434ce',
 'f3dad9a749944f8ee898ef9bc2fdeccdef8a28bc',
 '7e3227902fb2a775c54a8a2015a03517f78c90c8',
 'e3985460f38adeff80f37f579b5a05ca5e0147b9',
 '515b9e7bad11f11274f8bc165fc71490927603ee',
 'f8d81d55cf63b89a19096e8b65a9a4903c64ce58',
 '86347cbdd79a33a100d6dac02bfb749a6d94884b',
 'd764ca6a43107bf4d278247a1d00d606b002158c',
 'baef2c5eeeed967d6ed66715a63bdfb616a47d48',
 '73d36e7024b982a9c11efc4b26dc6174bf27f6d0',
 '840f1e504fcaf1a78b7777b3627ce7f279f9cfae',
 'f3c355d034056d06c7a89ecf3fad3aa2e0bd416c',
 '6d4af697b0675cf788dca63a7e7d789007aa8df5',
 '78382063000ba8131f4051067445bc914f63267f',
 '79b2055b979be54adbe92cd03d4ceabf5dc2d021',
 '0b29bfa3ac1ebe6eccb63d35a907f8162a64af53',
 '9a2ff942e3b026814e70f4f5054d61e7aab71b87',
 '634a20956c9d8f15411cd728c4e028c0e41f1861',
 'e46fb5a692a4d0d5cdc39b9f3a22f81d91eea00f',
 '6bb45e45523e76fcb0c203e8825609be598be322',
 '307199097494012f3295a59780eac97a72c74b63',
 'e049334dec1fc7c98350dbda2333f5e9f2455deb',
 '8bca362dac6b4ac59e067af8c323fe323fc0224f',
 'f5b8ed8d420b0631689e9609bc8ecba73302b267',
 '81431a773881a0613180463727067682fdf0d76c',
 '3a3e19e80a7c3564e318a527f6b9c7ffc839c58a',
 '060f1cc3eee605a8502e2e2de836f4ac20e76474',
 '5ac260d9411c84be8cd25cc1feaaec15949e53ea',
 'f8edc321f8241a997efcd00d506ecdd326bf0c33',
 'e12b25ff0bbe2dbcde18322e248db59dbeda933a',
 '55309b6a9e71f45beffb1ef945b4fdc29aab9864',
 '03a9e20e3d0798fd02797e5b027b459307273347',
 'dac6f12e374ebf2830ad02f9277cc08ea3663f2a',
 'ddd5d1256bc3e0cf2d88688857b94a8f02b1d0e4',
 '7f7ddac16f9a2504ae5cc48ac5013ac003931589',
 'c489f8a245bfbe57f94749cababf2ce872b3d18c',
 '99b0e0da38c0f0c8185f55de90f171a5b550f106',
 '0a6b5370e5bfad63dc77b5e44833e3a3e4e9eac2',
 '992fd64386d13d4c2692872ca2588ab80162f861',
 'e23dbb5492ddbddfe976c56b558338234447ea35',
 'ebd158800bc8cad5dcd50c0622457200011d6ccf',
 'b33efc5386cbfbcf00f84d47513ee082ab4af20d',
 'bab76177f06d074dca60d8ac603cdcd25eacedb5',
 'd8d3704e4e916f8a794a1c7714dc2422725f8854',
 'd2c1a06be5cd485510a0a8a1f9ca01e1282e4911',
 '2306c3d735cc4f19fc19cb3f61d6d59865cafdc8',
 '5ddae70b36f49dc93161baaa351b71959b89d53f',
 '94a8cf5d7984d4af4dbb879ea96663f9398d304f',
 '15aa5fb203a0fa729462a907b20a6558ce42c5b5',
 '5e205792784ce1eef45b6d59720eb783974955ff',
 '69ea9ca578264af6f9204fcfa547df094b6105a6',
 'd8d62e9176b746174d2dcef7452a9d8584ab1222',
 'd96d9f80714f6e50141963f08eb2fb09a7a4e965',
 '42e46f512f988d49ca29882032e3d3cf07b0404e',
 'dffbe435f72449c87c7675d130abe7441b245ea2',
 '65fce48e0fdbb10ea59d40e4ac83322d67735e42',
 'a82a5d1a8edf15dfb99490301eff6c3e8c9077ec',
 '7fcf112e91b02b65bf62e857054b7f05cb77c2ab',
 '842e8ccb22a8273973262ce16ef0c23fad38ef23',
 '9140bf0ad123c98a5cc50acfd8c8a62f4c979be4',
 'bf4d96c97cc49f7931483bf0bef8767718635725',
 'eba116e23b0823ee7b05e98d85092dc7cbb1fc23',
 'e64edbf1f5fe84db9aa0bd1167366b037f8cdaf9',
 'eae7ad5a18cdf53ddc37dde2c569a1df0adfe5ba',
 '38b9bbae0c2d30fd5df91d2c3e8751cdbc63eba1',
 '148819221946f6ba3c1ec937a71e19838b3be288',
 '0dccda54fb7d73d6fe2a14c3f68e08a356733f50',
 '0d49c701b40e62df28c1bb09d32cd4117dfc3b20',
 '1230b84757ae7393c3b4247a2a6c7fe812606a45',
 '0f3d855ba293a0ad771efd0bfab229c3f52bd56d',
 'bb98f74d562bedb9299dfed9a1b3f6b298036894',
 '890bd02261dbdd7b1f7b6a96045c353a0cf65c0a',
 '37be68a480c2cf2052228d4424bec5b6a19c6bba',
 '51d75efddc7a18e4d9a8065224cc58cdf836dff4',
 '34c80ff74b252210e970b176ffea90098e1bf96e',
 '8bf3ff36b27aa6d7688e09fafa88142b0c8bb59f',
 'da10dfd5590bc1903f9b60ba289808e2a898c575',
 '5ad52f6cf280939c6e758f356ea9861f376c5f23',
 '4cfdc4376805f7587493809ccd45b082f682b47e',
 '8c15fe5cf02d2edd1a19828559fd753902501848',
 '1b2d83894756fa7b565c32ba4b227cf932f8e63b',
 'c1383dbbdf010d9499ca96973d1a9de79ef21b2f',
 '12777ccb63f351421a0264e43b142c3dd6c0beda',
 '5811ac3dd73afdc4980cd9845a2552957ce4ea02',
 '0fb23818b9fc7019392e8e652c31990064f7f5d3',
 'caab718f410560b190abfb6163782f6f3e0e7010',
 '7eaf97f527c174d1503f5d507f2cf542e5ed878b',
 'd9a8c7ee5b78328717caf2a5407bde02d87e19ca',
 'e1913008ac492f2c8d0d1019417322925ebcde26',
 '6ac20ece36d2e0607488a5a4c2691e778ffc013c',
 '4e1b6519cb7c971a776cbd9e8e10a2d542f67b1b',
 'e8838fe1f978d14333872375417f0df87ab52035',
 '38925d893c0b77ab5c927d43ed1dadd3f27644bb',
 'de2a09d82c9ed598d35728c7e1c1c0f14c7c779a',
 'a95cd29ec182f7f32e56f325bc1a2232a7c66c7c',
 '455f4d23ae5998ee891fa8fe980ce1af6b62235e',
 'ce2bf88fe0b1bf12ccdd3bf7a48ad6c2d3792177',
 'bfa0d765c0b1283889bb32ee5d5d510d7f76413c',
 '860f50044015be5ceaf8e78393bbab1731a7bc21',
 '9beca940cdf06941142e8900f5d39fc3345789c7',
 '17a41b5f4b6407302f575f84820eab92b46c3f13',
 'a2a19078838e10acbfe4348ededa0c70acf8cf7b',
 '19962291bd9b7b0cc1c7e91aff79296a1ae562b0',
 '249f50b688530d73903d0bef4f60a797d0c17848',
 '0a80a7696e29473359faaf81b675bae4fb74c6dd',
 '7d23e6bb2c359eebb15a628f58dc34a5195e3b8e',
 '7b4977bfa5bf436622b498f7b7a168c1cd4e9888',
 '4dcf8ce7cf23c84bdcc7cdf8c1d35320f35779a8',
 '21ab5b1c9f8312c3f99b31a78c41fcbaa31decec',
 '617d54f3a19703c5e5c671945a534d40369bc390',
 'e531b093ac3bf4855fe4343dfd8a9b162e768326',
 'd5c07a8037d30efdd2e6902ae2e6b28621419030',
 'd86ffe80d4a1a0bc2a563a08afa72b9be0f800d9',
 '23161ad3b9ef76dd7972852c396600440635b757',
 'e31c63fc37e20788a328362f1b82bfc48861fed6',
 '3bb1e74cd9453955491f71d112e04c0a87df9282',
 'e5eeb492cfbfbb1884e4190a5c0791ecbbf0b64f',
 '229b84f0f1e285615786c68ba7ce8492d227f29d',
 '45330436a0e784ec28e81ab2b3bcf2a2b91ec7be',
 '2198087049ee87477e2b0147858d0c4a9b32056b',
 'f5a5ea2f0a6f0a1d693fd1c8a520ee8c676313a5',
 '85ec7ede7ebdaa66f60e4dfb23e9ef39f7176fc9',
 '24f50dff25a5f2af623e224d94b946127c0fb5ad',
 'e408219d13fb30bd2e82dec668b8309e4f017439',
 'cc9eaea52b5cc585d59aa6e3e253084a1dd55f4f',
 '5564749d86e73f847111638ccf754c420fb13e45',
 'c1c1e2f90a9737b174a420ea406d19a5700703a1',
 'b3053edee7a5e201aa8f42f6b0e1cbbbde1040ee',
 '37696eabfc8aa2041dfb74e02fd10aa956836520',
 '58fd600933a1a88297a0ade7953db93fdeb207ae',
 '4db0aa8ee0f134bcffcd1057c9316f86ba6fe4ab',
 'e4dbad91ce27e4dddfb7a00d44aff5506b7985c4',
 '0657c03fa4899c7943d6634ee7623fb9092f642c',
 '90346929e824893425d20f0c4d00265b12722a4c',
 '0e0de99c614601b70fa3451fa3dcdfad02ebfd0d',
 '8576fab631923f70036828ee2c4f28ec9bccf250',
 '914993143d4b0bf612e3c588c2344be71fdf43b8',
 '2e1e62fd813be94176c90c6ff4149f9f9ecea39f',
 '8d779674417bdeb198e371e06d18dfceb6733d02',
 '0615725d45555d3439a73c9a56902e6300ba08b8',
 'af068126776bf57feaa1f040e336b31e5bb7c73b',
 '850c775ee6f8c80f5256d478ff779c373b60209e',
 'c0fc572ba53c53820724ceb6cee67835c95f9d81',
 'ad8ac57c71fa3cec8b30c22ffdde98eab9262729',
 '128aa19c933f50e0141c2f893a79dc00356930d3',
 '9334b1718f2224ac4a9d429c8921480ceaf8c6d9',
 'd9090dce5c6f709c6d420d5077354edee9e8756c',
 'aa4e92dc3b320409a72f67a1b9c3947e9a41ecc5',
 'f047a62dc3e9da81698bbf38313efe81617a01bd',
 'a074476b6aba1abf715ab5e43d7852086ccdd985',
 'a3cb4e9fb3a5f231ec1c57b71272a730203b9da7',
 '670a8259e3ff77d9d9a26e808e65695f06298bb6',
 '0b1332eff34523784996b841e0c3e93d46c59442',
 'd6fbf44e9073ec3c806f665648e24117796e15f7',
 '4520a07504df34f0933c1e6c10002bb172a02d9f',
 '9cd8aaf6c24ac376b60142a1d1d871a82ce6fa55',
 '94e23fe6bf7974a20a60e6e4b00a57af26faf5db',
 'aa9143f040f153b7fcc5001a5547c8d9c1657de1',
 'a833865ecfd8125d5c61ea6b8dbbcf16d3a787d2',
 'adada20660da21d6155237fb65495db1d103a07b',
 '87a33b39f592a2dcfea60365ed5fa66ac9138c18',
 '792eb4a3c1ca287f583dbd2c900ca58c0009fecc',
 '58db9bcf9f153e835c6d1d5cd36e2be46d4ea42c',
 '636b1d9b60f3640965c0cc38bd37c6b1b9031950',
 'fcfe48417c29b732871fd9a1da4d63279030742b',
 '2d8ef6ad1067a8363da4e9a67b917c4890ed957b',
 'e5e7bf471ef29093e60e02d449f0cef82fe3a4ae',
 'dab9fe63fd20bf210bbe4cb4f28e9573c11c229b',
 'f22555c95175a53d035885390f1fff9ac95f621f',
 'b57b6aa9a4387ca51bc021da2374362ed4bce948',
 '09ff8a7364c3b723e9fff14b1c6bf67f98767d3d',
 '2e2b0924129355f8517b2e15875602fbb07b210f',
 '0321b49cea0e817a6c75d7d21f1272cff31b0db6',
 'cf274edcb3b74d8b4843a3d5b2eee29ebc0f4f91',
 '73c245c7f29d8d98707a0e9384da145cff52ba81',
 '0a31e1c032a8c883f328c33fdfec3d313138ba03',
 '27fe1a0fba4ad65faa67a962294dfa7a2700d3a5',
 '6b58334654fe5c00b092867d2e96fc4708bfef43',
 'c382dab9964ec9cffb6154e64abb6b1ca3267d62',
 '5d5a4c8e58017c4044fa627752d28884d12a1d17',
 '6b03753882884f98fbb42af1d73e951129e267a2',
 'b8d0ed8f92339d73fc25071f9ff1e7e6143303bf',
 '0b6b3975e0acfd7db7aa63a30d26f99bdec76c92',
 '05f3d9fc11fe91e6f1467ba255b50d3c5a1dc7e6',
 '6d237390b23f677790778a828a83775475708c35',
 '41c2b62d38f2cc2d41e5ba55f8c1e76aca70b83b',
 'e136fd1cb0930b294bee6dd5c08484c8d686b4fc',
 '453ec38918a1770970a38621d6184adf76e848d9',
 '3d0fd2c90fbb5f29f0a0838d9d580aa2ff2eccfd',
 '648836dd5fa077f1d9406b8c04d9efddc01247e3',
 '207d1ba5e2b57c5a45599923c2a1b37af4e34e1d',
 'c34e206b7cfb7d7a23ae4e800f290582c0b02362',
 'bc7ca20e6cf42b6c7b3ca6872a439cfb5d515467',
 'c5a183a1a4c698afa860ee952b2e993ac940390a',
 'ffbcb88007b2108b9867579cf401b9efc8a7921e',
 'dc295e6204ae5b6064ff695384d02ead1e85727c',
 '5715f84d5be748a7e861fb85a6d4a960bee883e7',
 '4957d94e2086bbb0789eaa1a60d826ddfad66b07',
 'f2592d13acd50dcb16dff1f01a1f022ff57c69db',
 'd15d9425ed9140bc6b531bf5466d4a29bfeba36b',
 '6ef523f55c5572ad8d598a52136d7f3cc8ff9fba',
 'aa18006c1bce773f3af7431aa958b4299cc8d1e3',
 'ef755bf47de3b0d61596c270d193028debf682e9',
 'ad46b3a8126d0465284b72579ed32d204cdb1c60',
 'cdd767428b04d02af28f70ba9134da2f035f7f77',
 '96e2d44d161733fd68cb4a67ae6d5a2ba2fa7a72',
 'a38c5a0e1de6a498ec2976a7b3cbb9f18c63a419',
 'ee0fdfc424dd77f4b53caba2cf4d66dd6d6e9618',
 '78a5578416c7976be202b2d408dd95690b16b967',
 '896a2b149415b5b562501d89d4e9bac8b7f987ec',
 '30e714cdb131d9d992d34277c6c35e9b433bde79',
 '4fa651388a47bd8d577e7499060c830aa08e0acd',
 '92c466704a21f638c4fa6e59401f20c87b74a1c3',
 '1c869c904d110713aa2a7f5daae046176e3271cd',
 '55a9e4900ce31508b42c9ff84f51af24ec8b41a6',
 'dc65a89eb53e00e7b5a5e05a32728903725c6e1c',
 '8cd4e27536acf10018568fe5f06f5e338e1a4d2c',
 'c03248fa19013b0a1cf8b0766df49d0c4f5ba585',
 '31b5f6b8c1c2f7dd41220d2b61b4b9821a1da0d4',
 '86727a6a6dd99f3ee9d1f531e5d760d00108e3b8',
 '0d211e23843e75612b8448baf1702cc14ce624db',
 'ae07681ded74ca1aa506387975cd8e374a6b7a7f',
 '7495f6c3c44e481241355ec0dbc6e816cee8a311',
 'd500d5ed1e56f4b5a8859baea9d8b231b97fc44f',
 'c09ba4b4c2e1c7239f44d64e344df7465ddc90d6',
 '6f44752bc403b15cf96d88c30969843c0dd320f3',
 'a3cd925274074e03c9173326c0a9c9facc28d4c2',
 'd2bc08bbfc814f09a8bec9b602fb0df9c33e5a24',
 'd41d56b7487017f6f482805c6a932dc89756b6c7',
 '4cd685c13abf00347fec23ddd73566ddfe3e0241',
 'ff198d9a705c9745940162e266bb91629b59f832',
 '2c20a991fac4013c98559441d61afcea014ac235',
 '99190912430d1d0a920228d4f939c96d2034203a',
 'c9dc5c556ec2a82852d89bedab45be7659112477',
 'bb19c31f3bbafcf8f0404fba9f96fcf2e4b40dd8',
 '10f4d6a0340d9a98aa316d9f8a27858efa618068',
 '604f04035f7b783e1a7dcab2db176c1e87abf542',
 '332d7a7ddabf23f3788db16dcb8e5fec29cacf58',
 '36bd0b01f79468b2bae721873ece47147588089f',
 'bfe645308440e158b04d032692047e719a9d5be0',
 'e15793580f48d2d3c5b242e25ae6f8eb8534855c',
 '8821e2f922f27e079d2cdb14e2f7c03388141082',
 '1180048602365ce28d4c105a805686e2305b1c4f',
 'e40e43562f7733cbacaf4c91638d6806fe8f20e7',
 'aa1b487f3b945e04850b514769fc4b623bc36928',
 '4c57ca38bb3b215ec4518a1cb52790f8a00ee086',
 '3b6dddc007518fca7ad80dc6869d19549d6ceca1',
 '285ec669bd91a4aa629568a7c7b87395dcdc6901',
 'f49a1179d97af0032bb2dd15ab52d1dbe1f44257',
 '40a739b524af2e4db09138d76de585309f711c94',
 '1faff67b3e36861f7d1b394db033bfaf4923f576',
 '3929fd6f4c412836536dd21e89a2cb0a5ee36d43',
 '317bfb5290e50d2d67c6ded7aab9eca01ce0fcde',
 'f8b035a1120300c7ce42fa63fc2eb8d1acbef230',
 '8c795e871cd3e5a36ff8d3f19aa7259af19b4a22',
 '9b9a3c45dc2696eac4ff0ca9684e6c993d63ff38',
 'd41fe16727b7f164030304f06acef853a60e3831',
 '1175af7035b3d7be945d23271ef949a5d2c1fa10',
 '055eb648c5a868ec2b7f502f6754f61dc537b05e',
 '3b6ebe1e97a09faffa0c11c1e3eac6efda3c31a2',
 '8da410d417df0a7b7fe1662c644d4b83f9c2f637',
 '546c0c76bddf6a7214b4a4036f039b09299fefe8',
 'dcd33ce2b2606472e9a8c2df3a7abbefc0c76e52',
 '2a96c4c8ae09ebd27d2cbea4c627733a156a725c',
 '5ce238fcc6756c25063434fb7c1a3878f9b25523',
 '7866133370903cea5dbea59207df945b051789cf',
 '881a806e3d06100a4aeda8e4664c8a07966bd4d2',
 '50c49fa45c4bdfa453909dade3d7f58b14e8be8a',
 'd36a3b809f5207d5791c57617edbdee36d960257',
 '1e4242361fecbdffc574858726f9b75d5140a619',
 '59a5dd46f5b4bc146b76919e0cb3dd76525dbd6e',
 '151ab77207cec3cc4147e3ceadbb382749bc69bd',
 '866f4049a75c7f166977a11d8d71851a63b37bce',
 '3681c0fbf80ce36b3b8febc93aefa841f83b5bba',
 '9cb78c8e18106ab75dcd64dbdf853e01ead1c3b7',
 '378b6c3a0fac00e63f4174a5252f36da20b1fa08',
 '431fd614354c4fa1814262e240e400919cf2d2d5',
 'f44a2e1310004af7cb5d6a01231ebde4af03b971',
 '30d68469b93e322acc010187f6f411b299154cf3',
 '20cc091aba2a6aeb537bc5fc90bacf0d0cd4b9ef',
 '3db40dc96207830cffc19904a32b1a679b74dcd0',
 '476d222d87ac342450dd5ef42079e1ede6e58132',
 '27e5e13d2ed3e22d0f5fce8653a045930d62fe8f',
 '48c0c3a873bc5adaf20c632b5b7630a4fac000b4',
 '88b74d6ef0046446071a66b0aa3d59c8d86e97bf',
 'cfa327e5dcad303339bce218a1daa72b87930c76',
 'f6234d00c5d59450adea1d7edd30ad3859375c79',
 '558d76c401cc28b85e11c534a87ace43e19bafa6',
 '1bd9fce6cb622846ecdf003754e8d2f7388d1883',
 'd19038e76b49f260aee8b1436b7c7a9675ac8818',
 '7e17ff0ab34642a08d023767058e0fb5fe2d9593',
 '85816595d2f49194a918ac1b02fc201d797dfad6',
 '5b82658b3ccaee22b6792a7003a0d256526d8555',
 '9a91f7058c9728724d403304e50250af4e4433ab',
 '0404020f7b944398db529a0d09726a0e5f3c06a4',
 'abf87e7248b0218a56fb7f05be8379c7d19dfa7f',
 '46e1a59d84c84dc549f5cf04592d2846be12fff8',
 'd6d751213432ecb61fef9a7cd1f6e4dd3aa7ee8b',
 '8794e5a21c980a0f26532cb4883284b786cb3f25',
 '847a9df09c196b1e3ffd974d918fb91d5aacc248',
 'a1a1bee5c235503ca3b5e1d901ae07b3eb9d0ec8',
 '141af4febfb478a013d83c12a9d8ca4fd2de02f0',
 '87e32b6b30f3e5113e26ac7468319d000a3affac',
 'd21cd51d860454b921abf9f2fe1d5dfd4c071e33',
 'b5455fdd16fe608214f91149233660e1836b067c',
 'ca4a0c43584f0a595faa2da3b2c05b427a498e28',
 'b82f8b06540720d1b637e97a8d605e3c2eb69870',
 '649c20629eedea5a26d34b01ec8f3e14e96e9249',
 'ab099779de6aff3f61a631e5122800133b137c64',
 '6e67841a9a45c8160bc85846001004edbca97386',
 '246477a80986f2af64fd00181eeb4ed5c5b6330c',
 'f6bfcb973ce518c1515cc39e4d000a122325d144',
 'a51337ecee2a61cfc6c0a444314d3214bc93c702',
 '0f5f5a59966d5eb26a8ff217264f4dad73ba6dee',
 'e818025b4d2cd7b5bf622608284bf497b5babe17',
 '75bbc055a7d6137bd0a54f9de551a3c3f8fa300f',
 '8b03e65804712f03926f8b92bae15a0bb1705910',
 'c9dbdbcea735dae65e7c9fc6c9d4a6983ff156be',
 '8e81e2aaaefdb9c247c422f90dc0f68c4d8382fa',
 '8f97ae9aec729bcbbad17da462115e1ec1398421',
 '2d91c085e3f37402e257c4f2c93694e726ee1d23',
 '4c8e326500aca04b7b670b34611915ee7f0b7caf',
 'b50e27e6d7cd1885a37b4c468dabfab56185c329',
 'ae23b1f676276d8fa55ea9d9b7483f4925bc7d6a',
 '16accefd2f5f799c6d67c86d26e13d197603547c',
 '5b5b5e8da0d5a457b5217d54444d04442834d22e',
 '6e041d524439b69ddfec73398816f8a7b01edfa0',
 'd27f9e6bbff0fd76cbe01839ef7b2d69962affe6',
 '9d146c9cc27c62fb0f294ab04eb95c3c3e34a0d4',
 'cc1645452466afc465eaa130aa6094b8fa8dcc23',
 '578374675e6e2ac42daca75d42e4199c146484b2',
 'e0028410ee69e797905e2c1d9d44a26cfb543c37',
 '67863777caeaba2188977ab8df1738e36cbf9a3f',
 '48fc1d780b8dd58323273a33665a6255a2827077',
 'b81bb0638dc46501d41ac86f65e8c6b8aa239527',
 '155a792013fc2640dde885babe958802645b03fe',
 '5c03e27ce2df02126de797cc98294098f755ce36',
 '7c954969b70bb75a24a1d16bbe7faeb8705a6f42',
 '35e9c63d89a306133dfdacf1c848f5f5549de2a4',
 'e495b33bf16f403cf4ffbfd52833848ca6dfe5e8',
 '2ba1cfeb2e28c77a3ae2323772e5a6bca43844cb',
 '5937b95192bc02a98a8a29d44caffd71f2b25694',
 'ff85af3fc7d38e663e08cdada10e26f3d99ab91e',
 '5342eccf12102c14a9739eaffd4fbe401d5b5403',
 '7ace96ba40ef9a3caf58cec846eb32b1cc1a281a',
 'bfcc9f92b4587ccf8e9f0eeafee85a66b76afd34',
 '0cd7d54b00106d8992803a918ce55d5c205550f3',
 '81a4f280d24e2ab77e1a10196c0a601838716111',
 '152d42d1a7314a8f7912e661547341dafd62a5fb',
 '6158964ff69c0c3ec0ee37fd5de5221b65ac7bcf',
 '9e67c8e8be5047a61519139ed38536e015207449',
 'f9ad7400e30517159712a77e6a4bc2f3a390b2db',
 'cd64bed55ee816536cd0ad0cebf75568af3fca09',
 '8f19078c6ad72300676376f5824fcf50cd9b693b',
 '4fdfdf67499c33015ed56e6e5910d8bdc00aa901',
 '486b692ddd801f8f665d3fff023149fb1cb6509e',
 '3197b4de6d5a83896fa138bdcc02e7d2fd4561ac',
 '2ec8d2b42ffecec2128f1004c0a8451378057d42',
 '3e7145e0bbcded852a5324ceb549fe5ca94ac924',
 'bf2cc8b31c73f69d6c8fce97aa00607077bb8a67',
 'a63a2b7c78562cd4eaf33f5f7db81ae0b3938552',
 '404640a2b752f502825dc8b26212204fa890d495',
 'a3131862bd00f5a23c9f60dadfffa8be26d1d3c1',
 '307c85e5c1b0dd0ca0d94ec362976254cbd949b4',
 '20132435c07cd1cee9f896ba9cd504698c09589c',
 '5ad030d19d71523bb4c901991bc0fac47cdd1ebf',
 '0159f25fd024c76a5a1f66fdbca39a828e4e2a61',
 '265bd59c1d9f8dea658f243b257d4fae3685af53',
 '57a44a82a9de48b5be53b59a9d84d8bc47c620ea',
 '3b6de315aa8f74de6d2ce60853e6a33055645f57',
 '435130279acac0a815b88ba26e53937b52f2e6da',
 '8b3721e90effff48bccc3fd261217c12bc4d4d74',
 '0128aa862b59b30724f8774eefc2d39f33d92289',
 'd3ea11eb46fc827073fee818711841243d726683',
 '1082058d7be3d49910071da04c1809ada8e8bfb3',
 'e6f2be27d92b2966eb1441551ac09ebf501fa84e',
 '9d045400603c807452d191dfc1c3f90edd4791d0',
 '922b8fb6f284ac4471d5e6b61c6b9bf0926bbe6d',
 'caf5230ab0637c794f4de6a7af6c49c5c3612a54',
 'ba9b64f36ef69b521a9aacb0d7b836555ff65d7e',
 '0b15cbf2403c970d7168375c1cf73270fb88c8b9',
 '8017a0b920077fc5f8e78e374d93031de98912f6',
 'ea232538d7c65c3676122ac2687e833a03e8963b',
 '15f3556988d62d02fcb9ad1f9b200d01201d3cbf',
 'a3acd3497a5309d12b61c143a8fbdd47b07fa42c',
 '95461828c51eb5c367f822646243f3ce0435ce6f',
 '93347ab6cb15675806b1691f5c3343c195865f4b',
 '333b09592db092a3cdbec04f265b4bb3e13556c0',
 '847864a02221e4cbbe6f722db9c09af51dcf4d9f',
 '21aa63ef056e603de07d667fc254f354e0edcb08',
 '9c9aefcc3699dcf7cb564f8f2c814aa831b23c80',
 '2fe0d6e9aacdc26f47f3a47ef75f1850f204ae6c',
 'fb209f7d09682129184a531f2391bd87010a8c67',
 'd202ef938c1fef2849ce3a66e2ce644ad83134fb',
 '93b17ea3219d9b03a104d56297da79e20eb7c52c',
 '7044dd3da185306f4d1e26a9b735aa819f016d84',
 '624b7895a07170dc2423d43d7796cf6f40b4b527',
 '717390025fbe1bdf4142748e56c872511f19d5b7',
 '04e408f7df5d6a017336b749f4566520fb198a25',
 'ecf5cbd66a40946066ed7b3a494bf9f436aacd78',
 '955fb5cd83aeffb337b6115526049f0a989c15ca',
 'd8404715020e5689ec62bb3d79fabb110521daf7',
 'f7db043b892a19bc7b49573f1b8afe266ed7612d',
 'd805bccdc6b882c14fc6bf56dec0cc25ca4d3887',
 'e2e2135c1c6b56076be19bb98606e3a395432fb9',
 '6f6d901e6bc5c5ebcef8e1a25c18d755f2332ac5',
 'fa485c6f670a65b5e9cc651dd1a31c74674423af',
 'a99e1bba7a430059fdb94dc983b06a185a28019c',
 'b7bc21f369a749650653f98b165d96fd59e315ed',
 'e9a918a924706268ca76069d62e8c1c43a549d19',
 'fd4c61c7531005d7a567e64e472184e91e08ea0d',
 'd8cbb91ebc9a8587a64d1049cd0c4663f37302d4',
 '5e598612db6b5cab6ba297850831c71cfccc9bca',
 '06fb767c5ed089a6bbfbba0747561aee76c1a638',
 '918bf215c2f19399b8072a10ba7eb75fd421b2df',
 'e788edc9509be98489c2aac82ee0810f98f2163f',
 '4950ce728585bfc3c19b5bc53ede083d87978a1c',
 '946a90f74a062f5365d5ef6d98f8c48c5465a2f2',
 'bfe62af69be6a584142e82c3dd5ee7902ecbd18f',
 '6a7929fb1f143add7d3a80a1363e20ecdd033e6e',
 '1c4178e8b7123ecb1c2ba85c18758af377e961ac',
 'f14f9c8a47d31cc6ece1a25f89da2bd41568de32',
 'f1b1575e87f57b2273c29b8fc81d795d248a3f31',
 '67b09db8644f2548134aa81a8974924c88551444',
 '20a82ed4259f340d1ac25eb86b6ce5c74bcb737f',
 'c9c0db29b98f45ed6de84eed6bedaff6b9935ba8',
 '8d71cd02e6cbf879e53d90ac2a9b43d138e0d09d',
 'eedb8859367c97064654d282ff2ef77da7b81a79',
 '011e73e89fa7cff1e7ab1b95dccc149bb250c7b3',
 'ae373825f249c6b1643dfeafdd9b8d8571b3b3fc',
 'c2a7807bdf44c36d41db12b38d0cd140b10ea0ff',
 '68667dcd38ddfe8be6bf45692ed165865152a7f4',
 '2b70335668aba826ef3758a0b39481c8a9174f07',
 'c7c9109f1d43cd31da53df114faab232dabab77b',
 'af8b992b2eb0e4db6ea0927fe3d89fefe0bdf133',
 '4539df1c953ae3459401b433dcbe95d705732966',
 '74fc53e3342a890045ea0a855fb1803687b74391',
 'dc7a83e3dbdccd1da90bac5ce38c8c68b6f254d3',
 '6902fde1e1319aa896fc4f5341b848e3ae5445fe',
 '98553ff158c379bba5208f5c6c692f55a3428d4b',
 'e8b22ae8b7d7dc1ae327a9b3c05faf710f081333',
 'd5ad69e567b058d68efb5819dfec6c8719ee5b12',
 '3cd3a67088a94254ca9b8e0f079a9ee84767b8a5',
 '80c9b993a33332102791e2f969cce7687715ca51',
 'd8d416c69e17f8f5d27f581ed957f1d0425a22f9',
 'a8d0f4606ba0317c19a0f3cc6551dfe18123a6fb',
 '9243a8d5e9a46728125c00c98594e00285ed0019',
 '19e9bb23254316b54b0d3afbc94812c1b9d87fc2',
 '8237ac5184d83281d9089e66d807a2381df305a5',
 '667064f2c3f97c42c082d41299eae03a75af13b9',
 '98e60a40bb65e98b87376f30e3fc9d9d88ecfe63',
 '18a68d57c015d5a3f27ca840da9029b7cb0752e4',
 'f787eedef477ce0753d9af58e178c970ca668dea',
 '5786f7e2a07efbd9593cd2e843bdfd97bbd5da2b',
 '7e08307d31b59e36e9280bfbeac928d05ed5691b',
 '9b451b64fd403bda19b1363abb351d7aca596079',
 '798aef596cdb924684357e7563ec4d89c25619ed',
 '30c1c90e610292fe8a0202199971a72a34d0d4de',
 'ae0d2bad68c3312b01925541deeb8100e27cef4b',
 '6605156b25dee360c4ccae7eb41c740bd33726ed',
 '41cdbd3c79f5bcd4353540ade40a1aae987ef0df',
 'bac988683e055082b3465bb53d21139324fa4580',
 'bf3e251f7432b187d0d26662e153db7cfec7314d',
 '5393e1748959f35990d057bf22e19060dc4a8a3a',
 '23ea472c5608b08191734edc94b8cf34bf9dad89',
 '5cf09a8e444fd6b5569ac081a531f55bba782b16',
 'b5512d1ca905063d14f62f1dddb1ad61f6d1644d',
 'dbe92ae4078a93c52d51c5f9deae6cf14b11ed0d',
 'e085f1eb395da48f2f29087411c7459f953d8c46',
 '9dd2376ba2c948805906c20ce5bacb92f523e90f',
 '681bd7b3b1d353689482a49d41385788f81724b5',
 'ecd1caaf7d575ab937260710735a9cc327145d5a',
 '94b9f7cf464d5c905ad74371a9511856419c763b',
 'bcf382f17585138ccfa959e47e22eca1e9b3f49a',
 'a11ccc2b62cdd6d9c9247b180db748befaefc35d',
 'f9b37669b300c8935945beb72baecc02a7d0ff95',
 'c6721afec7b627430f6b43c322539cb85eb176a6',
 '0ed15552039f30fb9a27d64639685a839a7979e4',
 '1bfd803b46f574acac977e4fbae3f1f4c5643761',
 '0d5188608b44253ef5983cb57f184dedad426592',
 '7bafc94d3fc2f6e7fe76a7d40b6518ff9c9e718a',
 '8a5d8f76f7288e0de4fff910400f27d28f22b444',
 '70a1b4ab682b03fbdfaa988a6fde1f01b79c47f4',
 'd25d181f8d7a93a882301668dd76d281f6d6dce5',
 '665b47ab0a2c25689fc222e52f8957f4f71b6711',
 '47afd1c2e0009b4b519180e55ef0b0a572b0c848',
 '0c592eb2d9fc0177acd803ab73295c48932a159c',
 'd832cf5c079640c6d2de0d450c1b4f4f979c317c',
 '733e8786e4e1b30f6f4f29e084b7341b1b09a1b3',
 'b1eaccd9e235fdece7d602f525f7d7fb6e89923b',
 '07ff4afe2c23f111b696bb040176716d7978d26b',
 '39266896ca575f94c3e7c62aad756e9425cbaf6f',
 '915777a8321b4186faad8e87e7fc00077a6bcca9',
 'b2628bda1a8279dbb74e7bd6ecc82fb2e077e865',
 'deb018da2e4c71906ec9f532cdf74b15feaca6c2',
 'f14ce50d82057b08c4a3c7c7ef4e5502bf894f37',
 '28d911dbf733ef4f9955f4367d58df97eb47f779',
 '1d8f926ca5c390016ba030280b51c2b8d47ee77f',
 'f913c596c1a8fa7c23980b38aee9449b4a23b738',
 'fd80cdfa3082459074d4784c46173e1ddff877a8',
 '9ef24ff7f32de2f7681a4b95e4ef90aa5eb5e43f',
 '81caf8fd720dfa9a0a8783c31fc6d00507e0a2cb',
 'b1781d587bba94cc31375843b2d007d9bac0eabc',
 '19508c776608edb28638eb7f79e53f6843f64d51',
 'c61b5b42ce48e2417e137c67471b98f4c50b541a',
 'cd07e74ff3fa33b38657685eeeccd915e76b2ecf',
 '2f907325d9424654b4b6106c76a2480f49711e66',
 '1f76f1e7dba6b76d245fde223c473bcb2b5e0732',
 '0295c3b67d17fc24939cdb695083bfd0fbc3d3c7',
 '20202751b2c641d8a2572df06c2052ca481bb8bf',
 '557665444e8118a0e3643a787334da641906e71e',
 'c28ef4e9ef05d9f3499c633767622d31ffcd035d',
 '89b40ca2dd596ec8c5da782b014c01f96a82420f',
 '07095e2044da368da0b90ed5ac7796c0ddccc5e6',
 '9a39f1e24cbee4ac4a26e45578490dfa5befe67c',
 '0e8613ca7a5792097df0bde2dd66918a71f3d43b',
 '4795b6253cae51601615a63c25f1b34d1f49a212',
 '583a9132164404a974e00638084a15f01507e919',
 '16d478e49de1825f8f76d0eb0f000c93f6472569',
 '2829819ba657cc21d369d46a699b4655a16fec55',
 'b9409e9df58b464bfa187c403d4bc58a02dd3e96',
 'f22e0ce567f8b46ff3dd3f1c4e74cdf0e1807310',
 '0e8ee202a70e44d782f1b7e64f4dbaf60f63f387',
 'fb8dc7d55965d9b12530914b65e22ea9208e6344',
 '2a15f88b50a6b5bbce56201cb7c78c40e236998f',
 'b9a58b2c6bbebc1c094e3dcc6dcad3c4fb0b5175',
 '961a1b7bd4bcbf6414abb016c9f94cb3d62f3d25',
 'efcc8b9f564eaf09953950bf44bec3b9aa809cc0',
 'fd5b7942171bb530cb1c9e8b60d6bbf41b1cb531',
 '9bc48d6d581e969a6f7e2624f75887fbbc4d04ef',
 '3b0f37546b4f086c83ec7b1a188e8b9605c8de82',
 '7cda8c02a8053b17ca139e9a320ef126b815f3d8',
 '02752d9f92e53b210af963bede45be9d52fbc00b',
 'a106bbe2aaeca9dcb56e7fd5ef2303c901c4070b')
