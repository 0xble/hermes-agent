"""Named quota regressions. Credentials/transport are synthetic fixtures."""

import json
from types import SimpleNamespace
import pytest
from hermes_cli.auth_commands import auth_refresh_command
from hermes_cli.auth_quota import probe_quota, run_status, run_refresh
from agent.credential_pool import PooledCredential, CredentialPool


def entry(**kw):
    return PooledCredential.from_dict(
        "openai-codex",
        {
            "id": "chosen",
            "label": "chosen",
            "auth_type": "oauth",
            "source": "manual:device_code",
            "access_token": "fake-access",
            "refresh_token": "fake-refresh",
            **kw,
        },
    )


def payload(**kw):
    return {
        "plan_type": "pro",
        "rate_limit": {
            "primary_window": {
                "used_percent": 6,
                "limit_window_seconds": 604800,
                "reset_at": 2000000000,
            },
            **kw,
        },
    }


def args(**kw):
    return SimpleNamespace(**{
        "provider": "openai-codex",
        "target": "chosen",
        "live": True,
        "json": True,
        "verify": True,
        **kw,
    })


def seed(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    p = tmp_path / "auth.json"
    p.write_text(
        json.dumps({
            "version": 1,
            "credential_pool": {
                "openai-codex": [
                    entry(last_status="exhausted", last_error_code=429).to_dict(),
                    entry(id="other", label="other").to_dict(),
                ]
            },
        })
    )
    return p


def test_weekly_primary(monkeypatch):
    monkeypatch.setattr("agent.account_usage._get_json", lambda *a, **k: payload())
    r = probe_quota("openai-codex", entry())
    assert r["outcome"] == "available"
    assert r["windows"][0]["kind"] == "weekly"
    assert r["windows"][0]["remaining_percent"] == 94
    assert r["observed_at"]


@pytest.mark.parametrize(
    "body",
    [
        payload(secondary_window={"used_percent": 100}),
        payload(allowed=False),
        payload(limit_reached=True),
    ],
)
def test_any_depleted_window(monkeypatch, body):
    monkeypatch.setattr("agent.account_usage._get_json", lambda *a, **k: body)
    assert probe_quota("openai-codex", entry())["outcome"] == "exhausted"


@pytest.mark.parametrize(
    "body",
    [
        {},
        {"rate_limit": {}},
        payload(primary_window={"used_percent": float("nan")}),
        payload(primary_window={"used_percent": -1}),
        payload(primary_window={"used_percent": True}),
        payload(primary_window={"used_percent": 101}),
        payload(secondary_window={"used_percent": "bad"}),
    ],
)
def test_invalid_data_is_not_available(monkeypatch, body):
    monkeypatch.setattr("agent.account_usage._get_json", lambda *a, **k: body)
    assert probe_quota("openai-codex", entry())["outcome"] == "unknown"


def test_missing_token_no_request(monkeypatch):
    monkeypatch.setattr(
        "agent.account_usage._get_json", lambda *a, **k: pytest.fail("must not request")
    )
    assert probe_quota("openai-codex", entry(access_token=""))["outcome"] == "unknown"


def test_status_is_readonly(monkeypatch, tmp_path, capsys):
    p = seed(monkeypatch, tmp_path)
    before = p.read_bytes()
    monkeypatch.setattr("agent.account_usage._get_json", lambda *a, **k: payload())
    run_status(args())
    r = json.loads(capsys.readouterr().out)
    assert (
        r["credential"]["id"] == "chosen"
        and r["cached"]["status"] == "exhausted"
        and r["outcome"] == "available"
    )
    assert p.read_bytes() == before


@pytest.mark.parametrize("target", ["missing", None])
def test_invalid_target(monkeypatch, tmp_path, capsys, target):
    p = seed(monkeypatch, tmp_path)
    before = p.read_bytes()
    with pytest.raises(SystemExit):
        run_status(args(target=target))
    assert json.loads(capsys.readouterr().out)["outcome"] == "invalid_target"
    assert p.read_bytes() == before


def test_cached_no_probe(monkeypatch, tmp_path, capsys):
    seed(monkeypatch, tmp_path)
    monkeypatch.setattr(
        "agent.account_usage._get_json", lambda *a, **k: pytest.fail("must not request")
    )
    run_status(args(live=False))
    assert json.loads(capsys.readouterr().out)["outcome"] == "not_probed"


def test_refresh_readback(monkeypatch, tmp_path, capsys):
    p = seed(monkeypatch, tmp_path)

    def refresh(self, credential_id=None, **kw):
        assert credential_id == "chosen"
        d = json.loads(p.read_text())
        d["credential_pool"]["openai-codex"][0].update(
            access_token="rotated", refresh_token="rotated-r"
        )
        p.write_text(json.dumps(d))
        return entry(access_token="rotated", refresh_token="rotated-r")

    monkeypatch.setattr(CredentialPool, "try_refresh_matching", refresh)

    def probe(*a, **k):
        assert a[1]["Authorization"] == "Bearer rotated"
        return payload()

    monkeypatch.setattr("agent.account_usage._get_json", probe)
    run_refresh(args())
    r = json.loads(capsys.readouterr().out)
    assert r["outcome"] == "refreshed_available" and "rotated" not in json.dumps(r)


def test_none_not_reauth(monkeypatch, tmp_path, capsys):
    seed(monkeypatch, tmp_path)
    monkeypatch.setattr(CredentialPool, "try_refresh_matching", lambda *a, **k: None)
    with pytest.raises(SystemExit):
        run_refresh(args())
    assert json.loads(capsys.readouterr().out)["outcome"] == "refresh_failed"


def test_nous_non_device_code_refresh_is_unsupported_without_refresh(
    monkeypatch, tmp_path, capsys
):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    (tmp_path / "auth.json").write_text(
        json.dumps(
            {
                "version": 1,
                "credential_pool": {
                    "nous": [
                        PooledCredential.from_dict(
                            "nous",
                            {
                                "id": "nous-manual",
                                "label": "manual",
                                "auth_type": "oauth",
                                "source": "manual",
                                "access_token": "fake-access",
                                "refresh_token": "fake-refresh",
                            },
                        ).to_dict()
                    ]
                },
            }
        )
    )
    monkeypatch.setattr(
        "hermes_cli.auth_quota._refresh_selected",
        lambda *args, **kwargs: pytest.fail("unsupported Nous credential must not refresh"),
    )

    with pytest.raises(SystemExit) as excinfo:
        auth_refresh_command(args(provider="nous", target="nous-manual", verify=True))

    report = json.loads(capsys.readouterr().out)
    assert excinfo.value.code == 2
    assert report["outcome"] == "unsupported"
    assert report["provider"] == "nous"
    assert report["credential"]["id"] == "nous-manual"


@pytest.mark.parametrize("verify", [True, False])
def test_native_refresh_persists_only_selected(monkeypatch, tmp_path, capsys, verify):
    from hermes_cli import auth

    p = seed(monkeypatch, tmp_path)
    sibling = json.loads(p.read_text())["credential_pool"]["openai-codex"][1]
    calls = []

    def rotate(access, refresh, **kw):
        calls.append((access, refresh))
        return {
            "access_token": "rotated-a",
            "refresh_token": "rotated-r",
            "last_refresh": "2026-09-07T00:00:00Z",
        }

    monkeypatch.setattr(auth, "refresh_codex_oauth_pure", rotate)
    monkeypatch.setattr("agent.account_usage._get_json", lambda *a, **k: payload())
    run_refresh(args(verify=verify))
    r = json.loads(capsys.readouterr().out)
    assert r["outcome"] == ("refreshed_available" if verify else "refresh_completed")
    assert calls == [("fake-access", "fake-refresh")]
    d = json.loads(p.read_text())["credential_pool"]["openai-codex"]
    assert d[0]["access_token"] == "rotated-a"
    assert d[1] == sibling


@pytest.mark.parametrize("code", [401, 403])
def test_access_rejection_is_not_reauth(monkeypatch, code):
    import httpx

    def fail(*a, **kw):
        response = httpx.Response(
            code, request=httpx.Request("GET", "https://example.invalid")
        )
        raise httpx.HTTPStatusError(
            "secret must never print", request=response.request, response=response
        )

    monkeypatch.setattr("agent.account_usage._get_json", fail)
    r = probe_quota("openai-codex", entry())
    assert r["outcome"] == "unknown" and r["action"] == "refresh_credential"
    assert "secret" not in json.dumps(r)


def test_readback_mismatch_fails(monkeypatch, tmp_path, capsys):
    seed(monkeypatch, tmp_path)
    monkeypatch.setattr(
        CredentialPool,
        "try_refresh_matching",
        lambda *a, **kw: entry(access_token="unpersisted"),
    )
    with pytest.raises(SystemExit):
        run_refresh(args())
    assert json.loads(capsys.readouterr().out)["outcome"] == "readback_failed"


def test_explicit_missing_id_never_falls_back(monkeypatch):
    pool = CredentialPool("openai-codex", [entry()])
    monkeypatch.setattr(
        pool, "_try_refresh_current_unlocked", lambda: pytest.fail("fallback")
    )
    assert pool.try_refresh_matching(credential_id="absent") is None


def test_native_terminal_evidence(monkeypatch, tmp_path, capsys):
    from hermes_cli import auth

    seed(monkeypatch, tmp_path)

    def reject(*a, **kw):
        raise RuntimeError("DO NOT ECHO TOKEN")

    monkeypatch.setattr(auth, "refresh_codex_oauth_pure", reject)
    monkeypatch.setattr(
        auth, "_is_terminal_codex_oauth_refresh_error", lambda exc: True
    )
    with pytest.raises(SystemExit):
        run_refresh(args())
    r = json.loads(capsys.readouterr().out)
    assert r["outcome"] == "reauth_required"
    assert "DO NOT ECHO" not in json.dumps(r)


@pytest.mark.parametrize(
    "command,expected,code",
    [
        (["auth", "status", "openai-codex", "chosen", "--json"], "not_probed", 0),
        (
            ["auth", "status", "openai-codex", "absent", "--live", "--json"],
            "invalid_target",
            2,
        ),
        (
            ["auth", "refresh", "openai-codex", "chosen", "--verify", "--json"],
            "refreshed_available",
            0,
        ),
    ],
)
def test_real_cli_isolated_fixture(monkeypatch, tmp_path, command, expected, code):
    import os
    import subprocess
    import sys

    p = seed(monkeypatch, tmp_path)
    before = p.read_bytes()
    script = """
import sys, runpy
from hermes_cli import auth
from agent import account_usage
# Synthetic provider fixtures. Never contact real OAuth or quota endpoints.
auth.refresh_codex_oauth_pure=lambda *a,**k:{'access_token':'fixture-rotated','refresh_token':'fixture-new-refresh','last_refresh':'2026-09-07T00:00:00Z'}
account_usage._get_json=lambda *a,**k:{'plan_type':'pro','rate_limit':{'primary_window':{'used_percent':12,'limit_window_seconds':604800,'reset_at':2000000000}}}
sys.argv=['hermes',*sys.argv[1:]]
runpy.run_module('hermes_cli.main',run_name='__main__')
"""
    env = {
        **os.environ,
        "HOME": str(tmp_path),
        "HERMES_HOME": str(tmp_path),
        "HERMES_PROFILE": "default",
    }
    result = subprocess.run(
        [sys.executable, "-c", script, *command],
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == code, result.stderr
    report = json.loads(result.stdout)
    assert report["outcome"] == expected
    assert "fixture-rotated" not in result.stdout
    if command[1] == "status":
        assert p.read_bytes() == before
    else:
        assert (
            json.loads(p.read_text())["credential_pool"]["openai-codex"][0][
                "access_token"
            ]
            == "fixture-rotated"
        )


def invoke(capsys, fn, options):
    with pytest.raises(SystemExit) as result:
        fn(options)
    return json.loads(capsys.readouterr().out), result.value.code


def test_corrupt_status_writes_nothing(monkeypatch, tmp_path, capsys):
    p = seed(monkeypatch, tmp_path)
    p.write_text("corrupt")
    before = set(tmp_path.iterdir())
    report, code = invoke(capsys, run_status, args())
    assert code == 1 and report["outcome"] == "store_unavailable"
    assert p.read_text() == "corrupt" and set(tmp_path.iterdir()) == before


def test_remove_before_refresh_lock_never_spends(monkeypatch, tmp_path, capsys):
    from contextlib import contextmanager
    from hermes_cli import auth
    from hermes_cli import auth_quota as q

    p = seed(monkeypatch, tmp_path)
    native = auth._auth_store_lock
    first = True

    @contextmanager
    def racing(*a, **k):
        nonlocal first
        if first:
            first = False
            d = json.loads(p.read_text())
            d["credential_pool"]["openai-codex"] = d["credential_pool"]["openai-codex"][
                1:
            ]
            p.write_text(json.dumps(d))
        with native(*a, **k):
            yield

    monkeypatch.setattr(auth, "_auth_store_lock", racing)
    monkeypatch.setattr(
        q, "load_pool", lambda *a: pytest.fail("removed credential spent")
    )
    report, code = invoke(capsys, run_refresh, args(verify=True))
    assert code == 1 and report["outcome"] == "refresh_failed"
    assert len(json.loads(p.read_text())["credential_pool"]["openai-codex"]) == 1


def test_text_shows_quota_error(monkeypatch, tmp_path, capsys):
    seed(monkeypatch, tmp_path)
    monkeypatch.setattr("agent.account_usage._get_json", lambda *a, **k: {})
    with pytest.raises(SystemExit):
        run_status(args(json=False))
    assert "invalid_quota_payload" in capsys.readouterr().out


def test_duplicate_labels_fail_closed(monkeypatch, tmp_path, capsys):
    p = seed(monkeypatch, tmp_path)
    data = json.loads(p.read_text())
    for item in data["credential_pool"]["openai-codex"]:
        item["label"] = "duplicate"
    p.write_text(json.dumps(data))
    report, code = invoke(capsys, run_status, args(target="duplicate"))
    assert code == 2 and report["outcome"] == "invalid_target"


def test_unsupported_provider_is_explicit():
    report = probe_quota("unsupported", SimpleNamespace())
    assert report["outcome"] == "unsupported" and report["windows"] == []


def test_anthropic_unpersisted_rotation_records_terminal(monkeypatch, tmp_path):
    seed(monkeypatch, tmp_path)
    e = PooledCredential.from_dict("anthropic", entry().to_dict())
    pool = CredentialPool("anthropic", [e])
    pool._refresh_failures = {}
    pool._fail_closed_unpersisted_rotation(e, RuntimeError("fixture"), store="fixture")
    assert pool.refresh_failure_reason(e.id) == "terminal"


def test_read_only_root_fallback_and_profile_precedence(monkeypatch, tmp_path):
    from hermes_cli import auth
    from agent.credential_pool import load_pool_read_only

    local = seed(monkeypatch, tmp_path)
    root = tmp_path / "root.json"
    root.write_text(local.read_text())
    monkeypatch.setattr(auth, "_global_auth_file_path", lambda: root)
    local.write_text(json.dumps({"credential_pool": {}}))
    before = (local.read_bytes(), root.read_bytes())
    assert load_pool_read_only("openai-codex").entries()[0].id == "chosen"
    assert (local.read_bytes(), root.read_bytes()) == before
    local.write_text(
        json.dumps({"credential_pool": {"openai-codex": [entry(id="local").to_dict()]}})
    )
    assert [e.id for e in load_pool_read_only("openai-codex").entries()] == ["local"]
