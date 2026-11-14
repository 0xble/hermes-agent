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


def test_model_proxy_timeout_names_the_endpoint_and_budget():
    exc = TimeoutError("timed out")
    named = annotate_job_failure(exc, base_url="http://127.0.0.1:8317/v1", elapsed=5)

    assert str(named) == "model proxy 127.0.0.1:8317 timed out after 5s"
    assert type(named) is TimeoutError


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


def test_second_job_does_not_page_for_the_same_proxy_outage(monkeypatch, tmp_path):
    monkeypatch.setattr(incidents, "EXECUTIONS_FILE", tmp_path / "executions.db")
    from cron.scheduler import _upsert_incident_for_failure

    error = "TimeoutError: gateway handshake timed out"
    first_job = {"id": "job-a", "name": "a"}
    second_job = {"id": "job-b", "name": "b"}

    first_withheld, first_id = _upsert_incident_for_failure(first_job, error)
    second_withheld, second_id = _upsert_incident_for_failure(second_job, error)

    assert first_id == second_id
    assert first_withheld is False
    assert second_withheld is True
