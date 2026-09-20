# Telegram topic titles and icons: title generation, Bot API icons, and lineage labels

Load this unit with the root contract when changing session title generation, Telegram
DM topics, or title persistence. The three fork patches are coupled: a model title,
its optional icon, and the durable alias can be observed through one Telegram topic.

## Required behavior

- `auxiliary.title_generation` controls word budget, casing, trusted instructions,
  and literal name-alias restoration. Recent titles are supplied as avoidance context;
  malformed or truncated model output leaves the instant derived title intact.
- The title prompt keeps three shortness rules the archived fork had and the first rebuild
  lost: title the concrete subject rather than the message's intent, prefer a proper name,
  and avoid generic leading or trailing labels (Testing, Analysis, generation, help). Examples
  are case-matched Bad/Good pairs, the word rule biases toward the low end of the configured
  range, and the icon request sits inside the rules so the title examples and reply contract
  close the prompt. Dropping any of these measurably lengthens titles; verify prompt changes by
  sampling the configured model on a few real opening messages, not by unit tests alone.
- Telegram topic icons use Bot API `get_forum_topic_icon_stickers` options only.
  Name and icon changes share one `edit_forum_topic` call. Manual icon ownership is
  preserved when configured, and successful automatic writes record state and history.
- `/title X` on a Telegram topic stores the user's exact text, or the next free lineage alias
  such as `X #2` only when that exact text is held by another session; the visible Telegram
  topic is renamed to the unsuffixed label. Auto-titles that collide likewise keep the visible
  label unsuffixed while the session row carries the alias. Non-Telegram collision behavior
  remains upstream behavior.
- `preserve_manual_topic_icons` relies on a Bot API `StatusUpdate` handler that observes
  `forum_topic_created`/`forum_topic_edited` service messages in private chats; a user-chosen
  icon recorded there is never replaced by an automatic rename.

## Provenance and patches

- **Identity and status:** active fork adaptations `slice-15-title-config`,
  `slice-16-topic-icons`, and `slice-17-topic-lineage`.
- **Source / fork refs:** baseline upstream release `v2026.9.14`, fork base
  `origin/main` `a0f8f3996dae`; source designs are NousResearch/hermes-agent PR
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
- **Fork delivery:** landed on fork `main` as one squash-merged PR whose commit carries the
  `Fork-Patch:` trailer slices above. This unit is the owner of those identities;
  `scripts/check_fork_patches.py` verifies the trailer on every commit after the floor.

## Update

On every upstream sync, compare title prompt construction, Telegram adapter topic
editing, topic state migrations, and `/title` collision handling together. Preserve
manual ownership and the one-call rename invariant. Retire all three patches only
when a released upstream tag provides equivalent behavior, then remove this unit and
its ledger rows in the same reviewed change.

## Verify and recover

Run the focused title, icon, gateway, and state tests through `scripts/run_tests.sh`.
Run `python scripts/check_fork_patches.py` against the worktree HEAD, `git diff --check`,
and `ruff check` on changed Python files. If rollback is required, revert the landed
squash commit (found by its `Fork-Patch:` trailer) as one unit and retain the two icon tables
(`telegram_topic_icon_state`, `telegram_topic_icon_history`) before starting a process on the
rolled-back code; never edit the operator's live config as part of source rollback.
