# Telegram inbound Rich Messages

Load this unit when changing Telegram handler registration, inbound message
classification, mention gating sources, or reply-context extraction. Outbound
rich rendering is owned by [Telegram rendering](telegram-rendering.md).

## Required behavior

A formatted paste reaches the agent. Telegram Bot API 10.1 delivers it as
`rich_message` blocks with no `text`, and python-telegram-bot 22.8 keeps the
unknown field only in `Message.api_kwargs`, so `filters.TEXT` misses it. Without
this patch no handler claims the update: no inbound log line, no error, no reply.

- A dedicated Rich Message filter, registered after text and media handlers,
  claims rich-only messages (no text, caption, or media) and enqueues one TEXT
  event whose text is a bounded Markdown projection
  (`plugins/platforms/telegram/rich_messages.py`). Headings, inline emphasis,
  code, links, lists and checkboxes, details, quotes, and tables survive.
- Authorization runs before projection. Group trigger gating, observe-only
  persistence, text batching, forum commands, and reply-media caching reuse the
  ordinary text path. Rich text participates in mention and wake-word gating.
- Reply context for a replied-to Rich Message uses the same projection.
- Depth, node, and character limits bound malformed payloads. An empty
  projection logs a warning instead of dispatching.
- A final `filters.ALL` handler, last in group 0, logs any message family no
  handler claims, with payload field names only, never values.

## Provenance and patches

Fork patch identity: `telegram-inbound-rich-messages`.

Ported from the archived fork's HERMES-068 (archived commit
`49163f7fedbd9c9f239583dc39ed647b47295dba`, 2026-08-29) and its unmatched
message guard. The replacement fork established on 2026-09-19 never carried
either, so formatted pastes were silently dropped again on the live runtime,
observed 2026-09-25 21:41 PDT with no gateway trace. `rich_messages.py` is
byte-identical to the archived file. The adapter wiring was rewritten for the
current handler table, `_gate_or_observe` era gating, and `group_trigger_text`.

Upstream: issue #63485. Open, unmerged PRs #63491, #81369, #94899, #95292,
and #98679 overlap. #98679 is the narrowest, but it flattens to plaintext and
loses inline formatting and nested list structure, which matters for pasted
prompts. None is released as of 2026-09-25.

## Verification

`scripts/run_tests.sh tests/gateway/test_telegram_rich_message_ingress.py`
(RED on base: 12 failed; GREEN: 13 passed, 1 skipped because the gateway test
harness mocks python-telegram-bot). The real-PTB path is proven separately by
dispatching a de_json Rich Message update through a real PTB 22.8
`Application.process_update`: the rich paste enqueues Markdown, plain text keeps
its handler, and an unknown payload logs `Unhandled Telegram message`.

After authorized promotion, send one formatted paste and confirm a
`Received Rich Message as Markdown` line followed by `inbound message` in
`gateway.log`, then a reply.

## Retirement and rollback

Retire when a released upstream version classifies rich-only messages on the
supported PTB version, preserves formatting as Markdown under the existing
authorization, gating, and batching paths, cannot steal text or media updates,
and passes this regression file without the fork code. Retire the unmatched
guard separately if upstream ships an equivalent sanitized catch-all.

Roll back by reverting the patch commit. No persistent state changes.
