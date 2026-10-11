# Malformed copy markup stays inline

Load this unit when changing `gateway/copy_blocks.py` or the streaming/final
reconciliation paths that handle `[[copy]]` response blocks.

## Required behavior

- A nested open marker, stray close marker, or unclosed block makes the complete
  response invalid. Extract no copy blocks.
- On invalid input remove exact marker lines outside CommonMark fences, preserve
  every other byte, keep bodies inline, and emit one warning naming the reason.
- Streaming cannot validate ahead. Final reconciliation must use the complete
  response's invalid-markup fallback so bodies hidden during streaming are restored
  to the ordinary reply.

## Verification

`scripts/run_tests.sh tests/gateway/test_copy_markup_invalid.py tests/gateway/test_copy_blocks.py tests/gateway/test_empty_sentinel_rewrite_copy.py tests/gateway/test_stream_final_contract.py tests/tui_gateway/test_turn_payload_interim_final.py tests/gateway/test_delivery_ledger.py tests/hermes_cli/test_stream_delta_think_tag.py`

## Retirement and rollback

Retire when equivalent upstream behavior is available and the focused regression
passes without the fork patch. Roll back by reverting the patch commit.
