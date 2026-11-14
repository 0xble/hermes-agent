"""Named dependency timeouts and one shared incident for a proxy outage."""

from pathlib import Path

import pytest

from cron import incidents
from cron.dependency_timeout import annotate_job_failure, name_gateway_handshake_timeout


def test_control_socket_timeout_names_the_gateway_handshake(monkeypatch):
    class _Peer:
        def sendall(self, _data):
            return None

        def settimeout(self, _seconds):
            return None

        def recv(self, _n):
            raise TimeoutError("timed out")

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

    monkeypatch.setattr(
        "hermes_cli.gateway_runtime_discovery.connect_private",
        lambda *_args, **_kwargs: _Peer(),
    )
    from hermes_cli.gateway_client import private_control_query

    with pytest.raises(TimeoutError, match="gateway handshake timed out"):
        private_control_query(Path("/tmp/hermes-home"), b"{}\n", timeout=1)


def _proxy_request(url="http://127.0.0.1:8317/v1/chat/completions"):
    import httpx

    return httpx.Request("POST", url)


def test_model_proxy_timeout_names_the_endpoint_and_budget():
    import httpx
    import openai

    for exc in (openai.APITimeoutError(request=_proxy_request()),
                httpx.ReadTimeout("timed out", request=_proxy_request())):
        named = annotate_job_failure(exc, base_url="http://127.0.0.1:8317/v1", elapsed=5)

        assert str(named) == "model proxy 127.0.0.1:8317 timed out after 5s"
        assert type(named) is TimeoutError


def test_bare_timeout_is_not_blamed_on_a_loopback_proxy():
    """Only a timeout raised by the proxy request names the proxy; a loopback base_url is not proof."""
    import httpx

    bare = TimeoutError("timed out")
    elsewhere = httpx.ReadTimeout("timed out", request=_proxy_request("https://example.com/feed"))
    other_port = httpx.ReadTimeout("timed out", request=_proxy_request("http://127.0.0.1:9000/x"))

    for exc in (bare, elsewhere, other_port):
        assert annotate_job_failure(exc, base_url="http://127.0.0.1:8317/v1", elapsed=5) is exc
    assert incidents._classify_failure_type(f"TimeoutError: {bare}") == "timeout"


def test_model_proxy_503_names_auth_unavailable_and_overload():
    class _Status(Exception):
        def __init__(self, status, text):
            super().__init__(text)
            self.status_code = status

    auth = annotate_job_failure(
        _Status(503, "auth_unavailable: no auth available"),
        base_url="http://127.0.0.1:8317/v1", elapsed=1)
    overload = annotate_job_failure(
        _Status(503, "servers overloaded"),
        base_url="http://127.0.0.1:8317/v1", elapsed=1)

    assert str(auth) == "model proxy 127.0.0.1:8317 unavailable: 503 auth_unavailable"
    assert str(overload) == "model proxy 127.0.0.1:8317 unavailable: 503 servers overloaded"


def test_job_owned_timeout_is_not_renamed():
    own = TimeoutError("Cron job 'monitor' idle for 120s (limit 600s) — last activity: tool")
    assert annotate_job_failure(own, base_url="http://127.0.0.1:8317/v1", elapsed=120) is own
    assert str(name_gateway_handshake_timeout(own)) == str(own)


@pytest.mark.parametrize(("error", "expected"), [
    ("TimeoutError: model proxy 127.0.0.1:8317 timed out after 51s", "dependency_unavailable"),
    ("RuntimeError: model proxy 127.0.0.1:8317 unavailable: 503 auth_unavailable", "dependency_unavailable"),
    ("RuntimeError: model proxy 127.0.0.1:8317 unavailable: 503 servers overloaded", "dependency_unavailable"),
    ("TimeoutError: gateway handshake timed out", "dependency_unavailable"),
    ("Detached cron run exceeded hard wall-clock timeout (1800s).", "timeout"),
    ("Script timed out after 2s: monitor.sh", "timeout"),
    ("Provider read timed out", "timeout"),
    ("Anthropic proxy: HTTP 503 auth_unavailable: no auth available", "auth"),
])
def test_dependency_outage_is_classified_once_and_job_timeout_stays_timeout(error, expected):
    assert incidents._classify_failure_type(error) == expected


def test_proxy_outage_is_one_incident_across_jobs(monkeypatch, tmp_path):
    monkeypatch.setattr(incidents, "EXECUTIONS_FILE", tmp_path / "executions.db")
    error = "TimeoutError: model proxy 127.0.0.1:8317 timed out after 8s"
    later = "TimeoutError: model proxy 127.0.0.1:8317 timed out after 51s"

    first_id, first_new = incidents.upsert_incident("job-a", error)
    second_id, second_new = incidents.upsert_incident("job-b", later)
    own_id, own_new = incidents.upsert_incident(
        "job-a", "Detached cron run exceeded hard wall-clock timeout (1800s).")

    assert first_new and not second_new
    assert first_id == second_id
    assert first_id.startswith("dep_")
    shared = incidents.get_incident(first_id)
    owned = incidents.get_incident(own_id)
    assert shared is not None and owned is not None
    assert shared["failure_type"] == "dependency_unavailable"
    assert shared["job_id"] == "dependency"
    assert own_new and own_id != first_id
    assert owned["failure_type"] == "timeout"


def test_second_job_does_not_page_once_the_proxy_outage_was_alerted(monkeypatch, tmp_path):
    monkeypatch.setattr(incidents, "EXECUTIONS_FILE", tmp_path / "executions.db")
    from cron.scheduler import _mark_incident_alerted, _upsert_incident_for_failure

    error = "TimeoutError: gateway handshake timed out"
    first_withheld, first_id = _upsert_incident_for_failure({"id": "job-a", "name": "a"}, error)
    _mark_incident_alerted(first_id)
    second_withheld, second_id = _upsert_incident_for_failure({"id": "job-b", "name": "b"}, error)

    assert first_id == second_id
    assert first_withheld is False
    assert second_withheld is True


def test_undelivered_first_notice_does_not_silence_the_outage(monkeypatch, tmp_path):
    """Job A's notice never landed (still ``detected``), so job B must still page."""
    monkeypatch.setattr(incidents, "EXECUTIONS_FILE", tmp_path / "executions.db")
    from cron.scheduler import _upsert_incident_for_failure

    error = "TimeoutError: model proxy 127.0.0.1:8317 timed out after 8s"
    first_withheld, first_id = _upsert_incident_for_failure({"id": "job-a", "name": "a"}, error)
    second_withheld, second_id = _upsert_incident_for_failure({"id": "job-b", "name": "b"}, error)

    assert first_id == second_id
    assert incidents.get_incident(first_id)["state"] == "detected"
    assert first_withheld is False and second_withheld is False


def test_shared_outage_after_a_quiet_gap_pages_again(monkeypatch, tmp_path):
    """No job recovery resolves the shared row, so a later outage must re-open it."""
    from datetime import timedelta

    from cron.scheduler import _mark_incident_alerted, _upsert_incident_for_failure

    monkeypatch.setattr(incidents, "EXECUTIONS_FILE", tmp_path / "executions.db")
    error = "TimeoutError: model proxy 127.0.0.1:8317 timed out after 8s"
    _, incident_id = _upsert_incident_for_failure({"id": "job-a", "name": "a"}, error)
    _mark_incident_alerted(incident_id)
    assert _upsert_incident_for_failure({"id": "job-b", "name": "b"}, error)[0] is True

    real_now = incidents._hermes_now
    later = real_now() + timedelta(seconds=incidents.DEPENDENCY_OUTAGE_GAP_SECONDS + 1)
    monkeypatch.setattr(incidents, "_hermes_now", lambda: later)
    monkeypatch.setattr("cron.scheduler._hermes_now", lambda: later)

    withheld, same_id = _upsert_incident_for_failure({"id": "job-c", "name": "c"}, error)

    assert same_id == incident_id and withheld is False
    reopened = incidents.get_incident(incident_id)
    assert reopened["state"] == "detected" and reopened["alerted_at"] is None


def test_ack_of_one_outage_does_not_silence_the_next(monkeypatch, tmp_path):
    from datetime import timedelta

    from cron.scheduler import _upsert_incident_for_failure

    monkeypatch.setattr(incidents, "EXECUTIONS_FILE", tmp_path / "executions.db")
    error = "TimeoutError: gateway handshake timed out"
    _, incident_id = _upsert_incident_for_failure({"id": "job-a", "name": "a"}, error)
    assert incidents.ack_incident(incident_id)
    assert _upsert_incident_for_failure({"id": "job-b", "name": "b"}, error)[0] is True

    later = incidents._hermes_now() + timedelta(seconds=incidents.DEPENDENCY_OUTAGE_GAP_SECONDS + 1)
    monkeypatch.setattr(incidents, "_hermes_now", lambda: later)
    monkeypatch.setattr("cron.scheduler._hermes_now", lambda: later)

    assert _upsert_incident_for_failure({"id": "job-c", "name": "c"}, error)[0] is False
    assert incidents.get_incident(incident_id)["state"] == "detected"
