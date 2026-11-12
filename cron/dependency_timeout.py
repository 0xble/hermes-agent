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
    parts = urlsplit(text if "://" in text else f"http://{text}")
    host = parts.hostname
    if not host or host.lower() not in _LOOPBACK:
        return None
    port = parts.port
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
        named = RuntimeError(model_proxy_unavailable_message(endpoint, detail))
        named.__cause__ = exc
        return named
    if not _is_transport_timeout(exc):
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
