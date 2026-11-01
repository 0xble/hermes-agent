# Loop completion display

Identity: `loop-complete-display`

## Required behavior

`LOOP_COMPLETE` is a loop-control sentinel, not assistant content. Strip a trailing standalone top-level marker, including a marker-only reply, at every user-visible gateway, TUI, and CLI delivery boundary. Preserve marker text in prose and fenced code, and hold partial trailing markers during streaming so they never flash. Keep raw final text available to loop post-turn hooks so completion detection and loop shutdown remain unchanged.

## Proof surface

Contract tests: `tests/gateway/test_trailing_silence_marker.py::test_loop_complete_split_uses_released_fence_context`,
  `tests/gateway/test_trailing_silence_marker.py::test_loop_complete_split_holds_the_whole_trailing_marker_run`,
  `tests/gateway/test_trailing_silence_marker.py::test_non_streaming_gateway_delivery_strips_loop_complete_but_detection_sees_raw`,
  `tests/gateway/test_trailing_silence_marker.py::test_loop_complete_trailing_marker_is_display_only`,
  `tests/hermes_cli/test_stream_partial_line_flush.py::TestLogicalLineStreaming::test_marker_after_cross_chunk_fence_close_is_hidden`,
  `tests/hermes_cli/test_stream_partial_line_flush.py::TestLogicalLineStreaming::test_repeated_marker_never_flashes`,
  `tests/hermes_cli/test_stream_partial_line_flush.py::TestLogicalLineStreaming::test_held_partial_marker_is_released_at_tool_boundary`,
  `tests/hermes_cli/test_stream_partial_line_flush.py::TestLogicalLineStreaming::test_marker_inside_open_fence_is_content`,
  `tests/tui_gateway/test_bot_mode_silence_delivery.py::test_live_stream_hides_marker_after_cross_chunk_fence_close`,
  `tests/tui_gateway/test_bot_mode_silence_delivery.py::test_live_stream_keeps_marker_inside_open_fence`,
  `tests/tui_gateway/test_bot_mode_silence_delivery.py::test_live_bot_chat_stream_holds_back_loop_complete_marker`,
  `tests/agent/test_interim_loop_complete.py`,
  `tests/gateway/test_crash_left_reply_media.py`,
  `tests/gateway/test_gateway_silence_tokens.py::test_queued_first_response_strips_loop_complete_but_keeps_raw_result`,
  `tests/gateway/test_gateway_silence_tokens.py::test_queued_bare_loop_complete_sends_nothing`,
  `tests/gateway/test_loop_command.py::test_non_streamed_stripped_reply_still_completes_loop_tick`,
  `tests/gateway/test_stale_finalize_suppression.py::test_streaming_loop_complete_marker_is_held_then_removed`

Focused tests cover fence-aware filtering, gateway streaming and final delivery, TUI payload and streaming deltas, CLI streaming, loop completion hooks, and existing silence-marker behavior. Run affected tests with `scripts/run_tests.sh`; run the exact-head gate before publication.

## Upstream disposition and retirement

Prepare the same behavior for upstream contribution without fork-only maintenance artifacts. Retire this fork record when equivalent upstream behavior is released and the fork no longer carries the display-boundary adaptation.
