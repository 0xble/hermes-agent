# Diagnostic notice accuracy

Fork patch identity: `diagnostic-notice-accuracy`.

User-facing diagnostics must not name a cause the evidence rules out or a deadline that has
already passed.

- `agent/stream_diag.py::connect_exhausted_notice` adds the "may reject requests this large"
  hint only when the connection opened. A refused, unresolved or timed-out connect
  (`httpx.ConnectError`, `httpx.ConnectTimeout`, `ConnectionRefusedError` anywhere in the chain)
  never sent the body, so its size cannot be the cause; the notice says the endpoint looks
  unreachable. On 2026-11-14 a proxy restart produced "request 771 KB ... may reject requests
  this large" for a plain `ConnectError`.
- `agent/chat_completion_helpers.py::_rate_limit_fallback_notice` says the primary "is
  recovering" when the shared outage record's reset time is already past, instead of
  "rate-limited until <past time>" (seen as "until 09:06 PST" at 09:09 AM).

Contract tests: `tests/agent/test_diagnostic_notice_accuracy.py`.
