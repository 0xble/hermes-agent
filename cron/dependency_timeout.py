"""Name the dependency behind a bare cron ``TimeoutError: timed out``.

Detached workers format ``f"{type(exc).__name__}: {exc}"``. A socket read on the
gateway control socket, and httpx talking to the configured model proxy, both
surface as ``timed out`` or a 503 overload. The job's own watchdog already
includes the job name and the idle budget; those messages must stay untouched.
"""

from __future__ import annotations

from urllib.parse import urlsplit

GATEWAY_HANDSHAKE_TIMEOUT = "gateway handshake timed out"
SESSION_RELAY_TIMEOUT = "session relay timed out"

_LOOPBACK = {"127.0.0.1", "localhost", "::1"}


def model_proxy_endpoint(base_url: str | None) -> str | None:
    """``host:port`` when ``base_url`` is a loopback model proxy such as ``127.0.0.1:8317``."""
    text = str(base_url or "").strip()
    if not text:
        return None
    try:
        parts = urlsplit(text if "://" in text else f"http://{text}")
        host = parts.hostname
        port = parts.port
    except ValueError:
        # This runs inside the scheduler's failure handler: a malformed URL (bad IPv6
        # brackets, non-numeric or out-of-range port) is not the proxy, and must not
        # replace the job's real failure.
        return None
    if not host or host.lower() not in _LOOPBACK:
        return None
    if port is None:
        port = 443 if parts.scheme == "https" else 80
    return f"{host}:{port}"


def model_proxy_timeout_message(endpoint: str, seconds: float) -> str:
    return f"model proxy {endpoint} timed out after {seconds:g}s"


def model_proxy_unavailable_message(endpoint: str, detail: str) -> str:
    return f"model proxy {endpoint} unavailable: {detail}"


def name_gateway_handshake_timeout(exc: BaseException) -> BaseException:
    """Replace a bare control-socket timeout. Other errors pass through."""
    if isinstance(exc, TimeoutError) and str(exc) in {"", "timed out"}:
        named = TimeoutError(GATEWAY_HANDSHAKE_TIMEOUT)
        named.__cause__ = exc
        return named
    return exc


def agent_reported_failure(message: str) -> RuntimeError:
    """The error for a failure the agent returned from its own model calls (retries exhausted).

    The agent summarizes the provider error as text, so the request is gone; this marks that the
    failure came from the agent's model route rather than a tool or the job's own code.
    """
    error = RuntimeError(message)
    error.agent_reported = True  # type: ignore[attr-defined]
    return error


def annotate_job_failure(exc: BaseException, *, base_url: str | None, elapsed: float | None) -> BaseException:
    """Name a model-proxy timeout or 503. Job-owned timeouts are not rewritten."""
    text = str(exc)
    if GATEWAY_HANDSHAKE_TIMEOUT in text or text.startswith("model proxy ") or SESSION_RELAY_TIMEOUT in text:
        return exc
    endpoint = model_proxy_endpoint(base_url)
    if endpoint is None:
        return exc
    detail = _proxy_unavailable_detail(exc)
    if detail:
        if not _from_model_route(exc, endpoint):
            # A matching 503 from a tool or another external call stays that job's failure.
            return exc
        named = RuntimeError(model_proxy_unavailable_message(endpoint, detail))
        named.__cause__ = exc
        return named
    if not _is_transport_timeout(exc) or _request_endpoint(exc) != endpoint:
        # A loopback base_url alone does not make a timeout the proxy's: a bare socket
        # ``timed out`` from a tool or the job's own code must stay that job's failure.
        return exc
    seconds = elapsed if isinstance(elapsed, (int, float)) and elapsed >= 0 else _timeout_seconds(exc)
    if seconds is None:
        return exc
    named = TimeoutError(model_proxy_timeout_message(endpoint, seconds))
    named.__cause__ = exc
    return named


def _is_transport_timeout(exc: BaseException) -> bool:
    name = type(exc).__name__
    text = str(exc).lower()
    if name in {"ConnectTimeout", "ReadTimeout", "WriteTimeout", "PoolTimeout", "APITimeoutError"}:
        return True
    return isinstance(exc, TimeoutError) and text in {"", "timed out"}


def _from_model_route(exc: BaseException, endpoint: str) -> bool:
    """Whether ``exc`` came from the model proxy call: its request went to ``endpoint``, or it
    carries no request and the agent reported it from its own model route."""
    url = _request_url(exc)
    if url is not None:
        return model_proxy_endpoint(url) == endpoint
    return getattr(exc, "agent_reported", False) is True


def _request_endpoint(exc: BaseException) -> str | None:
    """Loopback ``host:port`` of the HTTP request that raised ``exc`` (or caused it), if any.

    httpx and openai timeouts carry the request they were sending; that is the proof the
    timeout came from the model proxy call rather than some other socket in the run.
    """
    url = _request_url(exc)
    return None if url is None else model_proxy_endpoint(url)


def _request_url(exc: BaseException) -> str | None:
    """URL of the HTTP request carried by ``exc`` or its cause/context chain, if any."""
    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        try:
            request = getattr(current, "request", None)
        except RuntimeError:  # httpx raises when the exception was built without a request
            request = None
        url = getattr(request, "url", None)
        if url is not None:
            return str(url)
        current = current.__cause__ or current.__context__
    return None


def _timeout_seconds(exc: BaseException) -> float | None:
    for attr in ("timeout", "read", "connect"):
        value = getattr(exc, attr, None)
        if isinstance(value, (int, float)) and value >= 0:
            return float(value)
    return None


def _proxy_unavailable_detail(exc: BaseException) -> str | None:
    status = getattr(exc, "status_code", None)
    text = str(exc).lower()
    if status != 503 and "503" not in text:
        return None
    if "auth_unavailable" in text or "no auth available" in text:
        return "503 auth_unavailable"
    if "overloaded" in text:
        return "503 servers overloaded"
    return None
