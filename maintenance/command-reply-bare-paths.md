# Command replies keep paths as text

Load this unit when changing gateway slash-command dispatch, `EphemeralReply`,
`CommandReply`, or bare-path auto-delivery in `_extract_response_content`.

## Required behavior

- A slash-command reply is gateway-authored text. Bare local paths in it stay
  readable and are never auto-uploaded. Idle and busy dispatch mark every
  handled plain-string result as `CommandReply`. `EphemeralReply` is a
  `CommandReply`.
- `/retry` is excluded because its result is the re-run agent turn's own reply,
  which keeps normal bare-path delivery.
- Explicit `MEDIA:` tags in a command reply still deliver. Agent output keeps
  bare-path auto-delivery unchanged.

## Why

`/goal resume` echoes the goal text, and goals routinely name a plan or handoff
file (`Handoff: ~/.hermes/handoffs/.../plan.md`). The adapter's bare-path
detector stripped the path from the notice (`Handoff: .`) and uploaded the
file. Resuming several goals at once on 2026-10-04 sent three such uploads in
one minute. The third hit Telegram flood control and surfaced as "Couldn't
deliver the file attachment". Gateway logs from 2026-09-28 to 2026-10-04 show
18 of 25 bare-path uploads matched a goal-resume notice exactly.

## Provenance

Fork patch identity: `command-reply-bare-paths`. Own fork patch.

Upstream comparison on 2026-10-04: issue
[#64661](https://github.com/NousResearch/hermes-agent/issues/64661) reports the
same class for `/background`. Open PRs
[#64668](https://github.com/NousResearch/hermes-agent/pull/64668) and
[#64680](https://github.com/NousResearch/hermes-agent/pull/64680) wrap single
commands in `EphemeralReply(ttl_seconds=0)`. Neither covers `/goal`, and
per-command wrapping leaves every other path-echoing command exposed.

## Verification

`scripts/run_tests.sh tests/gateway/test_command_reply_bare_paths.py` fails on
the base without this patch and passes with it. Also run `tests/gateway/`.

## Retirement and rollback

Retire when a selected upstream release stops bare-path delivery for
gateway-authored command replies, including `/goal resume`, and this regression
passes without the local code. To roll back, revert the patch commit. Nothing
is persisted.
