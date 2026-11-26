# CLI Streaming Preview Framing

Fork patch identity: `cli-stream-preview-framing`.

## Required Behavior

The modern composer's pending-answer preview remains visibly distinct from
committed transcript, including when conservative resize recovery retains an
uncertain painted row. Ordinary activity keeps its spinner and status frame.

## Change And Provenance

`hermes_cli/cli_modern_layout.py::top_rule_fragments` renders streaming previews
without a frame or session title and preserves their leading ellipsis. Elapsed
time and token-flow suffixes remain available within the terminal width. This
matches the classic spinner's preview identity without changing resize erasure
or risking removal of real transcript.

Ported without source or test adaptations from
`ccd148e94fc959931fdad9c3262ae74b821b5108`. The former integration identity does
not own this standalone CLI behavior. No existing maintenance unit owns this
modern-composer rendering boundary.

Related upstream work: [#139241](https://github.com/NousResearch/hermes-agent/pull/139241)
introduced the glyph layout and spinner-in-rule design.
[#142178](https://github.com/NousResearch/hermes-agent/pull/142178) updated the
resize e2e for that layout without changing its transcript uniqueness or
bounded-preview contract. This narrow correction retains that design and
keeps preview text out of ordinary status framing. Neither related change
provides this rendering distinction. No upstream contribution is claimed.

## Proof

- `tests/hermes_cli/test_cli_stream_preview_rule.py::test_composer_rule_preserves_preview_identity_without_losing_activity`
- `tests/hermes_cli/test_cli_modern_layout.py`
- `tests/e2e/core/cli_tmux/test_resize_scrollback.py::test_resizes_keep_each_transcript_line_once_in_tmux_scrollback`

The resize regression allows bounded standalone ellipsis-led preview remnants.
This patch establishes preview identity, not complete erasure of transient rows.

## Retirement

Remove when upstream renders streaming previews distinct from transcript and
the resize-scrollback e2e passes without it. Roll back by reverting the commit
carrying `Fork-Patch: cli-stream-preview-framing`.
