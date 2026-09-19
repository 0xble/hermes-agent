# Telegram topic titles and icons: title generation, Bot API icons, and lineage labels

Load this unit with the root contract when changing session title generation, Telegram
DM topics, or title persistence. The three fork patches are coupled: a model title,
its optional icon, and the durable alias can be observed through one Telegram topic.

## Required behavior

- `auxiliary.title_generation` controls word budget, casing, trusted instructions,
  and literal name-alias restoration. Recent titles are supplied as avoidance context;
  malformed or truncated model output leaves the instant derived title intact.
- Telegram topic icons use Bot API `get_forum_topic_icon_stickers` options only.
  Name and icon changes share one `edit_forum_topic` call. Manual icon ownership is
  preserved when configured, and successful automatic writes record state and history.
- `/title X` on a Telegram topic stores `X` or a serialized lineage alias such as
  `X #2`, while the visible Telegram topic is renamed to unsuffixed `X`. Non-Telegram
  title collision behavior remains upstream behavior.

## Provenance and patches

- **Identity and status:** active fork adaptations `slice-15-title-config`,
  `slice-16-topic-icons`, and `slice-17-topic-lineage`.
- **Source / fork refs:** baseline upstream release `v2026.9.14`, fork base
  `origin/main` `06004e8e1b0`; source designs are NousResearch/hermes-agent PR
  [#66353](https://github.com/NousResearch/hermes-agent/pull/66353) and the
  single-call title+icon idea in [#35737](https://github.com/NousResearch/hermes-agent/pull/35737).
- **Surfaces:** `agent/title_generator.py`, `agent/topic_icons.py`,
  `hermes_state_titles.py`, `hermes_state_telegram.py`, gateway topic/title lanes,
  and `plugins/platforms/telegram/adapter.py`.
- **Adaptations:** Bot API only; no MTProto, Telethon, custom packs, provider key,
  attachment forwarding, or new environment variables. Icon selection falls back
  deterministically when the model field is absent or invalid.
- **Upstream disposition:** source PRs are open design references, not released
  equivalent behavior.
- **Fork delivery:** local commits are recorded in `maintenance/fork-patches.md`.

## Update

On every upstream sync, compare title prompt construction, Telegram adapter topic
editing, topic state migrations, and `/title` collision handling together. Preserve
manual ownership and the one-call rename invariant. Retire all three patches only
when a released upstream tag provides equivalent behavior, then remove this unit and
its ledger rows in the same reviewed change.

## Verify and recover

Run the focused title, icon, gateway, and state tests through `scripts/run_tests.sh`.
Run `python scripts/check_fork_patches.py` against the worktree HEAD, `git diff --check`,
and `ruff check` on changed Python files. If rollback is required, revert the three
feature commits as a unit and migrate or retain the two icon tables before starting a
process on the rolled-back code; never edit the operator's live config as part of
source rollback.
