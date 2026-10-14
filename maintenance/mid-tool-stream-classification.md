# Mid Tool-Call Stream Classification

## Patch identity

The maintenance unit identity is ``mid-tool-stream-classification``.

`mid-tool-stream-classification`.

## Required behavior

A stream that dies after partial delivery while a tool call is in flight must be
classified by what actually happened, not by a fixed label.

- A transport reset that a local proxy relays as an SSE `event: error` frame is
  transient. The Anthropic SDK raises that frame as an HTTP-200 `APIStatusError`,
  and the OpenAI SDK as an `APIError`. Either counts as transient only when its text
  matches `_SSE_CONN_PHRASES` and its status is absent or 200, so the existing
  mid-tool-call reconnect runs. Genuine 4xx and 5xx API errors stay terminal.
- The continuation prompt says a dropped call was "too large" and asks for smaller
  calls only when its streamed argument size reaches
  `PARTIAL_TOOL_ARGUMENT_SIZE_THRESHOLD_CHARS`. Smaller calls get network-interruption
  wording that says the call can be reissued as-is. Both variants keep
  `_LENGTH_CONTINUATION_DROPPED_TOOLS_PREFIX` so compression still recognizes them
  as synthetic.
- The user-visible warning says the action was not executed and that the turn
  continues automatically, matching the length-continuation path. It never asks
  the user to retry by hand.
- The dropped-call log line records provider, model, base_url, error class,
  attempt elapsed time, text size and tool-argument size.

## Independent hypothesis

- **Observed failure:** on 2026-10-13 a Fleet Health turn using the local Claude
  proxy lost a short `terminal` call after 114 characters of text. The proxy
  relayed `read: connection reset by peer` as an SSE error frame. The log shows
  `Streaming failed after partial delivery, not retrying`, the user saw "Stream
  stalled mid tool-call ... Ask me to retry", and the model was told the call
  "was too large". The loop then continued without user action.
- **Causal chain:** `_is_sse_connection_error` accepted only `openai.APIError`
  with no status code, so the Anthropic SDK's HTTP-200 `APIStatusError` was not
  transient and the retry was skipped. `_get_continuation_prompt` applied the
  size explanation to every dropped call without measuring it.
- **Smallest complete correction:** widen the SSE transport check to both SDKs
  while still excluding non-200 statuses, count streamed argument characters per
  attempt on both wire formats, carry the count on the partial-stream stub, and
  choose the continuation wording by that count.
- **Rejected alternatives:** retrying only read-only tools would add an allowlist
  for no benefit, because a dropped call never executed and is safe to reissue.
  Treating every `APIStatusError` as transient would retry genuine request errors.

## Verification

`tests/agent/test_sse_connection_error_classification.py` covers the relayed
reset frame, a real 400 with transport wording, a 200 frame without transport
wording, and an unrelated exception. `tests/agent/test_partial_stream_continuation_prompt.py`
covers the small and large continuation variants and synthetic-turn recognition.
`tests/agent/test_streaming.py` covers the updated warning text on the stub path.
