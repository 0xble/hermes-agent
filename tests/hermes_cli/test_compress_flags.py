"""Tests for /compress --preview/--dry-run/--aggressive flags and the
/compact alias (PR #3243 salvage).

Covers the pure helpers in ``hermes_cli.partial_compress`` plus alias
resolution in the command registry. The CLI and gateway surfaces both
route through these helpers, so the flag semantics are pinned here once.
"""

from hermes_cli.commands import resolve_command
from hermes_cli.partial_compress import (
    DEFAULT_KEEP_LAST,
    extract_compress_flags,
    parse_partial_compress_args,
    summarize_compress_preview,
)


def _history(n_pairs: int) -> list[dict[str, str]]:
    h: list[dict[str, str]] = []
    for i in range(n_pairs):
        h.append({"role": "user", "content": f"u{i}"})
        h.append({"role": "assistant", "content": f"a{i}"})
    return h


# ── /compact alias resolution ─────────────────────────────────────────


def test_compact_resolves_to_compress():
    cmd = resolve_command("compact")
    assert cmd is not None
    assert cmd.name == "compress"
    assert "compact" in cmd.aliases


# ── extract_compress_flags ────────────────────────────────────────────


def test_dry_run_is_preview():
    for form in ("--dry-run", "--dryrun", "--DRY-RUN"):
        _, preview, _ = extract_compress_flags(form)
        assert preview is True, form


def test_flags_coexist_with_focus_topic():
    rest, preview, _ = extract_compress_flags("database schema --dry-run")
    assert rest == "database schema"
    assert preview is True
    partial, _, focus = parse_partial_compress_args(rest)
    assert partial is False and focus == "database schema"


# ── summarize_compress_preview ────────────────────────────────────────


def test_preview_full_compress_counts():
    hist = _history(5)
    report = summarize_compress_preview(hist, False, DEFAULT_KEEP_LAST, None, 1234)
    assert report["head_count"] == 10
    assert report["tail_count"] == 0
    assert report["total"] == 10
    assert report["partial"] is False


def test_preview_partial_boundary_counts():
    hist = _history(5)
    report = summarize_compress_preview(hist, True, 2, None, 999)
    # Keeping last 2 exchanges = 4 tail messages, 6 head messages.
    assert report["head_count"] == 6
    assert report["tail_count"] == 4
    assert report["partial"] is True


def test_preview_is_side_effect_free():
    hist = _history(4)
    before = [dict(m) for m in hist]
    summarize_compress_preview(hist, True, 1, None, 10)
    assert hist == before


# ── --level ───────────────────────────────────────────────────────────


def test_level_flag_is_parsed_anywhere_and_never_becomes_a_focus_topic():
    from agent.conversation_compression_manual import parse_compress_args
    assert parse_compress_args("--level 2").level == 2
    assert parse_compress_args("--level=3 --preview").level == 3
    request = parse_compress_args("database schema --level 2")
    assert (request.level, request.focus_topic) == (2, "database schema")
    request = parse_compress_args("here 3 --level 3")
    assert (request.partial, request.keep_last, request.focus_topic) == (True, 3, None)
    assert parse_compress_args("").level == 1


def test_level_flag_clamps_out_of_range_and_garbage_values():
    from agent.conversation_compression_manual import parse_compress_args
    from hermes_cli.partial_compress import MAX_COMPRESS_LEVEL
    assert parse_compress_args("--level 99").level == MAX_COMPRESS_LEVEL
    assert parse_compress_args("--level 0").level == 1
    assert parse_compress_args("--level").level == 1
    assert parse_compress_args("--level=abc").focus_topic is None
