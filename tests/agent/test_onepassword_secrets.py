"""Hermetic tests for the 1Password (`op` CLI) secret source.

We never invoke the real ``op`` binary: ``subprocess.run`` is mocked so the
suite stays fast and offline-safe.  A live resolve is exercised manually via
``hermes secrets onepassword sync`` outside of pytest.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path
from unittest import mock

import pytest


# Make the worktree importable without depending on the installed wheel.
ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from agent.secret_sources import onepassword as op  # noqa: E402


@pytest.fixture(autouse=True)
def _reset_caches():
    op._reset_cache_for_tests()
    yield
    op._reset_cache_for_tests()


@pytest.fixture(autouse=True)
def _legacy_read_fixtures(monkeypatch, request):
    # Existing subprocess mocks model only `op read`; exercise that fallback
    # without mistaking the batch's argv for a secret-reference argument.
    if not request.node.name.startswith("test_batch_"):
        def unavailable(*args, **kwargs):
            raise RuntimeError("op run unavailable in read-only fixture")
        monkeypatch.setattr(op, "_run_op_batch", unavailable)


@pytest.fixture(autouse=True)
def _clean_op_env(monkeypatch):
    """Start every test from a known 1Password auth state."""
    for key in list(os.environ):
        if key.startswith("OP_SESSION_"):
            monkeypatch.delenv(key, raising=False)
    monkeypatch.delenv("OP_SERVICE_ACCOUNT_TOKEN", raising=False)
    monkeypatch.delenv("OP_ACCOUNT", raising=False)
    monkeypatch.delenv("OP_CONNECT_HOST", raising=False)
    monkeypatch.delenv("OP_CONNECT_TOKEN", raising=False)
    yield


def _ok(value: str):
    return mock.Mock(returncode=0, stdout=value, stderr="")


def _err(code: int, stderr: str):
    return mock.Mock(returncode=code, stdout="", stderr=stderr)


# ---------------------------------------------------------------------------
# Reference validation
# ---------------------------------------------------------------------------


def test_validate_references_filters_bad_names_and_refs():
    refs = {
        "OPENAI_API_KEY": "op://Private/OpenAI/api key",
        "1BAD_NAME": "op://Private/x/y",          # bad env name
        "HAS SPACE": "op://Private/x/y",          # bad env name
        "NOT_A_REF": "https://example.com",        # not op://
        "WHITESPACE": "  op://Private/z/field  ",  # stripped + kept
    }
    valid, warnings = op._validate_references(refs)
    assert valid == {
        "OPENAI_API_KEY": "op://Private/OpenAI/api key",
        "WHITESPACE": "op://Private/z/field",
    }
    assert len(warnings) == 3


# ---------------------------------------------------------------------------
# fetch_onepassword_secrets
# ---------------------------------------------------------------------------


def test_fetch_happy_path(monkeypatch, tmp_path):
    fake_op = tmp_path / "op"
    fake_op.write_text("")
    values = {
        "op://Private/OpenAI/api key": "sk-abc\n",
        "op://Private/Anthropic/credential": "sk-ant-xyz",
    }

    def fake_run(cmd, **kwargs):
        # argv list, never shell=True; reference passed after `--`.
        assert "--" in cmd
        ref = cmd[cmd.index("--") + 1]
        return _ok(values[ref])

    monkeypatch.setattr(op.subprocess, "run", fake_run)

    secrets, warnings = op.fetch_onepassword_secrets(
        references={
            "OPENAI_API_KEY": "op://Private/OpenAI/api key",
            "ANTHROPIC_API_KEY": "op://Private/Anthropic/credential",
        },
        binary=fake_op,
        use_cache=False,
    )
    assert secrets == {"OPENAI_API_KEY": "sk-abc", "ANTHROPIC_API_KEY": "sk-ant-xyz"}
    assert warnings == []






def test_fetch_read_failure_becomes_warning(monkeypatch, tmp_path):
    fake_op = tmp_path / "op"
    fake_op.write_text("")
    monkeypatch.setattr(
        op.subprocess, "run", lambda *a, **k: _err(1, "\x1b[31m[ERROR] not signed in\x1b[0m")
    )

    secrets, warnings = op.fetch_onepassword_secrets(
        references={"K": "op://V/I/F"}, binary=fake_op, use_cache=False
    )
    assert secrets == {}
    assert len(warnings) == 1
    # ANSI control sequences are fully scrubbed from the surfaced message.
    assert "\x1b" not in warnings[0]
    assert "[31m" not in warnings[0]
    assert "not signed in" in warnings[0]










# ---------------------------------------------------------------------------
# Caching
# ---------------------------------------------------------------------------


def test_inprocess_cache_hit(monkeypatch, tmp_path):
    fake_op = tmp_path / "op"
    fake_op.write_text("")
    calls = {"n": 0}

    def fake_run(*a, **k):
        calls["n"] += 1
        return _ok("v")

    monkeypatch.setattr(op.subprocess, "run", fake_run)
    op._reset_cache_for_tests(tmp_path)
    for _ in range(2):
        op.fetch_onepassword_secrets(
            references={"K": "op://V/I/F"}, cache_ttl_seconds=60,
            binary=fake_op, home_path=tmp_path,
        )
    assert calls["n"] == 1  # second call served from L1 cache








def test_connect_credential_change_invalidates_cache(monkeypatch, tmp_path):
    """A different 1Password Connect identity must not reuse a cached value."""
    fake_op = tmp_path / "op"
    fake_op.write_text("")
    calls = {"n": 0}

    def fake_run(*a, **k):
        calls["n"] += 1
        return _ok("v")

    monkeypatch.setattr(op.subprocess, "run", fake_run)
    op._reset_cache_for_tests(tmp_path)

    monkeypatch.setenv("OP_CONNECT_HOST", "https://connect.example.com")
    monkeypatch.setenv("OP_CONNECT_TOKEN", "tokenA")
    op.fetch_onepassword_secrets(
        references={"K": "op://V/I/F"}, cache_ttl_seconds=300,
        binary=fake_op, home_path=tmp_path,
    )
    # Rotate the Connect token → new identity.
    monkeypatch.setenv("OP_CONNECT_TOKEN", "tokenB")
    op._CACHE.clear()
    op.fetch_onepassword_secrets(
        references={"K": "op://V/I/F"}, cache_ttl_seconds=300,
        binary=fake_op, home_path=tmp_path,
    )
    assert calls["n"] == 2  # cache key changed → refetch






# ---------------------------------------------------------------------------
# find_op
# ---------------------------------------------------------------------------


def test_find_op_pinned_path_not_on_path(tmp_path, monkeypatch):
    pinned = tmp_path / "op"
    pinned.write_text("")
    pinned.chmod(0o755)
    # PATH lookup must NOT be consulted when a binary_path is pinned.
    monkeypatch.setattr(op.shutil, "which", lambda name: "/usr/bin/op")
    assert op.find_op(str(pinned)) == pinned




def test_op_child_env_forwards_config_directory(monkeypatch):
    """The op child must retain an explicit 1Password config location."""
    monkeypatch.setenv("OP_CONFIG_DIR", "/tmp/op-config")
    monkeypatch.setenv("UNRELATED_PROVIDER_TOKEN", "must-not-leak")

    env = op._op_child_env("")

    assert env["OP_CONFIG_DIR"] == "/tmp/op-config"
    assert "UNRELATED_PROVIDER_TOKEN" not in env


# ---------------------------------------------------------------------------
# apply_onepassword_secrets
# ---------------------------------------------------------------------------


def test_apply_disabled_returns_empty():
    result = op.apply_onepassword_secrets(enabled=False, env={"K": "op://V/I/F"})
    assert result.ok
    assert not result.applied


def test_apply_missing_binary_sets_error(monkeypatch):
    monkeypatch.setattr(op, "find_op", lambda binary_path="": None)
    result = op.apply_onepassword_secrets(
        enabled=True, env={"K": "op://V/I/F"}
    )
    assert not result.ok
    assert "op CLI" in result.error


def test_apply_sets_env(monkeypatch, tmp_path):
    fake_op = tmp_path / "op"
    fake_op.write_text("")
    monkeypatch.setattr(op, "find_op", lambda binary_path="": fake_op)
    monkeypatch.setattr(op.subprocess, "run", lambda *a, **k: _ok("resolved-val"))
    monkeypatch.delenv("MY_OP_KEY", raising=False)

    result = op.apply_onepassword_secrets(
        enabled=True, env={"MY_OP_KEY": "op://V/I/F"}, cache_ttl_seconds=0,
    )
    assert result.ok
    assert result.applied == ["MY_OP_KEY"]
    assert os.environ["MY_OP_KEY"] == "resolved-val"


def test_apply_skips_before_fetch_when_not_overriding(monkeypatch, tmp_path):
    fake_op = tmp_path / "op"
    fake_op.write_text("")
    monkeypatch.setattr(op, "find_op", lambda binary_path="": fake_op)
    monkeypatch.setenv("MY_OP_KEY", "from-env")
    calls = {"n": 0}

    def fake_run(*a, **k):
        calls["n"] += 1
        return _ok("from-1password")

    monkeypatch.setattr(op.subprocess, "run", fake_run)

    result = op.apply_onepassword_secrets(
        enabled=True, env={"MY_OP_KEY": "op://V/I/F"},
        override_existing=False, cache_ttl_seconds=0,
    )
    assert "MY_OP_KEY" in result.skipped
    assert os.environ["MY_OP_KEY"] == "from-env"
    assert calls["n"] == 0  # never even called op for a value we'd discard


def test_apply_never_overrides_token_var(monkeypatch, tmp_path):
    fake_op = tmp_path / "op"
    fake_op.write_text("")
    monkeypatch.setattr(op, "find_op", lambda binary_path="": fake_op)
    monkeypatch.setenv("OP_SERVICE_ACCOUNT_TOKEN", "original")
    calls = {"n": 0}

    def fake_run(*a, **k):
        calls["n"] += 1
        return _ok("malicious")

    monkeypatch.setattr(op.subprocess, "run", fake_run)

    result = op.apply_onepassword_secrets(
        enabled=True,
        env={"OP_SERVICE_ACCOUNT_TOKEN": "op://V/I/F"},
        override_existing=True, cache_ttl_seconds=0,
    )
    assert "OP_SERVICE_ACCOUNT_TOKEN" in result.skipped
    assert os.environ["OP_SERVICE_ACCOUNT_TOKEN"] == "original"
    assert calls["n"] == 0


# ---------------------------------------------------------------------------
# Partial-pull caching
# ---------------------------------------------------------------------------


def _entry(tmp_path, refs, ttl=300):
    key = (op._auth_fingerprint(op._DEFAULT_TOKEN_ENV), "", str(tmp_path), op._refs_fingerprint(refs))
    return op._STORE.disk.read(key, ttl, tmp_path)


def test_partial_pull_caches_resolved_refs_and_retries_only_the_failure(monkeypatch, tmp_path):
    """One failing reference must not suppress caching of the ones that resolved.

    A single slow ``op read`` out of N suppressed the cache write for all of them, so
    every process start paid a full serial pull. The resolved values must be reused and
    only the failed reference re-read — and reuse must not extend their TTL, or a
    perpetually flaky reference would keep carried-over values alive forever.
    """
    fake_op = tmp_path / "op"
    fake_op.write_text("")
    refs = {"GOOD": "op://V/good/F", "FLAKY": "op://V/flaky/F"}
    reads: list[str] = []
    flaky_fails = {"on": True}

    def fake_run(argv, *a, **k):
        name = "FLAKY" if "flaky" in argv[-1] else "GOOD"
        reads.append(name)
        if name == "FLAKY" and flaky_fails["on"]:
            return _err(1, "op: context deadline exceeded")
        return _ok(f"value-{name}")

    monkeypatch.setattr(op.subprocess, "run", fake_run)
    op._reset_cache_for_tests(tmp_path)

    first, warnings = op.fetch_onepassword_secrets(
        references=refs, cache_ttl_seconds=300, binary=fake_op, home_path=tmp_path,
    )
    assert first == {"GOOD": "value-GOOD"}
    assert any("flaky" in w for w in warnings)
    assert sorted(reads) == ["FLAKY", "GOOD"]
    first_stamp = _entry(tmp_path, refs).fetched_at

    reads.clear()
    op._CACHE.clear()  # force the on-disk path
    time.sleep(0.01)
    second, _ = op.fetch_onepassword_secrets(
        references=refs, cache_ttl_seconds=300, binary=fake_op, home_path=tmp_path,
    )
    assert reads == ["FLAKY"], "references that already resolved must not be re-read"
    assert second == {"GOOD": "value-GOOD"}
    assert _entry(tmp_path, refs).fetched_at == first_stamp, "reuse must not renew the TTL"

    # Once the flaky reference recovers, the entry completes.
    flaky_fails["on"] = False
    op._CACHE.clear()
    third, _ = op.fetch_onepassword_secrets(
        references=refs, cache_ttl_seconds=300, binary=fake_op, home_path=tmp_path,
    )
    assert third == {"GOOD": "value-GOOD", "FLAKY": "value-FLAKY"}


def test_per_reference_timeout_is_visible_to_the_orchestrator(monkeypatch, tmp_path):
    """A reference lost to a slow read must not look like one the user never configured.

    Per-reference failures are warnings, not a source error, so ``ok`` stays True and the
    resolved values still apply. Without a separate signal the orchestrator cannot tell
    that a value is missing for a reason a retry would fix.
    """
    fake_op = tmp_path / "op"
    fake_op.write_text("")
    fake_op.chmod(0o755)  # a pinned binary_path must be executable to resolve

    def fake_run(argv, *a, **k):
        if "slow" in argv[-1]:
            return _err(1, "op: request timed out")
        return _ok("value")

    monkeypatch.setattr(op.subprocess, "run", fake_run)
    op._reset_cache_for_tests(tmp_path)
    src = op.OnePasswordSource()
    result = src.fetch(
        {"enabled": True, "cache_ttl_seconds": 0, "binary_path": str(fake_op),
         "env": {"GOOD": "op://V/good/F", "SLOW": "op://V/slow/F"}},
        tmp_path,
    )

    assert result.ok, "the references that resolved must still be applied"
    assert "GOOD" in result.secrets and "SLOW" not in result.secrets
    assert op.ErrorKind.TIMEOUT in result.degraded_kinds


def test_degraded_kinds_keeps_every_failure_not_just_one(monkeypatch, tmp_path):
    """A timeout alongside an unrelated failure must stay visible as retryable.

    Collapsing a run to one kind loses the only question the orchestrator asks. A bot
    token that timed out is retryable even if some other reference failed differently.
    """
    fake_op = tmp_path / "op"
    fake_op.write_text("")
    fake_op.chmod(0o755)

    def fake_run(argv, *a, **k):
        ref = argv[-1]
        if "aslow" in ref:
            return _err(1, "op: request timed out")
        return _err(1, "[ERROR] account is not signed in")

    monkeypatch.setattr(op.subprocess, "run", fake_run)
    op._reset_cache_for_tests(tmp_path)
    src = op.OnePasswordSource()
    result = src.fetch(
        {"enabled": True, "cache_ttl_seconds": 0, "binary_path": str(fake_op),
         "env": {"ASLOW": "op://V/aslow/F", "ZDENIED": "op://V/zdenied/F"}},
        tmp_path,
    )

    assert result.degraded_kinds == {op.ErrorKind.TIMEOUT, op.ErrorKind.AUTH_FAILED}


def test_identity_rejection_requires_that_nothing_resolved(monkeypatch, tmp_path):
    """A rejected identity is one that resolved NOTHING, cache included.

    Scoping this to the re-attempted references alone inverts it: with most values
    carried from cache and a single reference retried, that one failure IS "every
    attempted read", so one unreadable item would be judged a revoked identity and
    would take every good credential with it.
    """
    fake_op = tmp_path / "op"
    fake_op.write_text("")
    refs = {"A": "op://V/a/F", "B": "op://V/b/F"}

    def all_refused(argv, *a, **k):
        return _err(1, "[ERROR] account is not signed in")

    monkeypatch.setattr(op.subprocess, "run", all_refused)
    op._reset_cache_for_tests(tmp_path)
    # Seed a stale entry so there is something to evict.
    op._STORE.store(
        (op._auth_fingerprint(op._DEFAULT_TOKEN_ENV), "", str(tmp_path), op._refs_fingerprint(refs)),
        op.CachedFetch(secrets={}, fetched_at=time.time()), 300, tmp_path)

    secrets, _ = op.fetch_onepassword_secrets(
        references=refs, cache_ttl_seconds=300, binary=fake_op, home_path=tmp_path)

    assert secrets == {}, "nothing resolved, so nothing may be returned"
    assert _entry(tmp_path, refs) is None, "the rejected identity's entry must be evicted"


def test_one_auth_failure_never_discards_values_that_resolved(monkeypatch, tmp_path):
    """The blast radius of a single refused reference must stay that reference.

    Regression for an inverted scope rule: with the other references served from cache,
    one forbidden item was read as a revoked identity, which returned nothing at all and
    deleted the cache file. It oscillated, losing every credential on alternate starts.
    """
    fake_op = tmp_path / "op"
    fake_op.write_text("")
    refs = {"KEEP": "op://V/keep/F", "DENIED": "op://V/denied/F"}

    def fake_run(argv, *a, **k):
        if "denied" in argv[-1]:
            return _err(1, "op: 403 unauthorized: you do not have access to this item")
        return _ok("keep-value")

    monkeypatch.setattr(op.subprocess, "run", fake_run)
    op._reset_cache_for_tests(tmp_path)

    op.fetch_onepassword_secrets(
        references=refs, cache_ttl_seconds=300, binary=fake_op, home_path=tmp_path)
    op._CACHE.clear()  # next start: KEEP comes from disk, DENIED is retried and refused
    second, _ = op.fetch_onepassword_secrets(
        references=refs, cache_ttl_seconds=300, binary=fake_op, home_path=tmp_path)

    assert second == {"KEEP": "keep-value"}, "the value that resolved must survive"
    assert _entry(tmp_path, refs) is not None, "and the cache must not be deleted"


def test_one_forbidden_item_does_not_block_caching_the_rest(monkeypatch, tmp_path):
    """A per-item permission denial is not an identity rejection.

    `op` words both as "unauthorized"/403. Treating one unreadable item as a revoked
    identity would block caching for the whole reference set on every call, which is the
    exact cost this change exists to remove.
    """
    fake_op = tmp_path / "op"
    fake_op.write_text("")
    refs = {"OK": "op://V/ok/F", "FORBIDDEN": "op://V/forbidden/F"}
    reads: list[str] = []

    def fake_run(argv, *a, **k):
        ref = argv[-1]
        reads.append(ref)
        if "forbidden" in ref:
            return _err(1, "op: 403 unauthorized: you do not have access to this item")
        return _ok("ok-value")

    monkeypatch.setattr(op.subprocess, "run", fake_run)
    op._reset_cache_for_tests(tmp_path)

    first, _ = op.fetch_onepassword_secrets(
        references=refs, cache_ttl_seconds=300, binary=fake_op, home_path=tmp_path)
    assert first == {"OK": "ok-value"}
    assert _entry(tmp_path, refs) is not None, "one forbidden item must not veto the cache"

    reads.clear()
    op._CACHE.clear()
    second, _ = op.fetch_onepassword_secrets(
        references=refs, cache_ttl_seconds=300, binary=fake_op, home_path=tmp_path)
    assert all("forbidden" in r for r in reads), "the readable item must come from cache"
    assert second == {"OK": "ok-value"}, "the forbidden item must not take the good ones"
    assert _entry(tmp_path, refs) is not None, "and must not delete the cache"


# ---------------------------------------------------------------------------
# Last-good fallback when 1Password cannot answer
# ---------------------------------------------------------------------------


def _expire_disk_entry(tmp_path, refs, age_seconds=3600):
    path = op._STORE.disk.path(tmp_path)
    payload = json.loads(path.read_text())
    payload["fetched_at"] = time.time() - age_seconds
    path.write_text(json.dumps(payload))
    op._CACHE.clear()


@pytest.mark.parametrize("stderr", [
    "[ERROR] 2026/09/24 19:40:01 Too many requests. Your client has been rate-limited.",
    "[ERROR] dial tcp: lookup my.1password.com: no such host",
])
def test_expired_values_carry_a_start_through_a_1password_outage(monkeypatch, tmp_path, stderr):
    """An exhausted quota or unreachable 1Password must not start Hermes without its keys.

    The expired entry is served for this call only: it is not re-stored with a fresh
    timestamp, so the next start asks 1Password again instead of trusting it forever.
    """
    fake_op = tmp_path / "op"
    fake_op.write_text("")
    refs = {"A": "op://V/a/F", "B": "op://V/b/F"}
    failing = {"on": False}

    def fake_run(argv, *a, **k):
        return _err(1, stderr) if failing["on"] else _ok("value-" + argv[-1].split("/")[3])

    monkeypatch.setattr(op.subprocess, "run", fake_run)
    op._reset_cache_for_tests(tmp_path)
    op.fetch_onepassword_secrets(references=refs, cache_ttl_seconds=300, binary=fake_op, home_path=tmp_path)
    _expire_disk_entry(tmp_path, refs)
    expired_stamp = json.loads(op._STORE.disk.path(tmp_path).read_text())["fetched_at"]

    failing["on"] = True
    secrets, warnings = op.fetch_onepassword_secrets(
        references=refs, cache_ttl_seconds=300, binary=fake_op, home_path=tmp_path,
    )
    assert secrets == {"A": "value-a", "B": "value-b"}
    assert any("last good cache" in w for w in warnings)
    assert json.loads(op._STORE.disk.path(tmp_path).read_text())["fetched_at"] == expired_stamp


def _counting_op(monkeypatch, tmp_path, *, rate_limited):
    """Fake `op` that records every read and answers 429 while ``rate_limited['on']``."""
    fake_op = tmp_path / "op"
    fake_op.write_text("")
    calls = []

    def fake_run(argv, *a, **k):
        calls.append(argv[-1])
        if rate_limited["on"]:
            return _err(1, "[ERROR] Too many requests. Your client has been rate-limited. Try again in  seconds")
        return _ok("value-" + argv[-1].split("/")[3])

    monkeypatch.setattr(op.subprocess, "run", fake_run)
    return fake_op, calls


def test_a_rate_limit_stops_the_remaining_reads(monkeypatch, tmp_path):
    """Every read after the first 429 would also be refused, so it must not be made."""
    refs = {n: f"op://V/{n.lower()}/F" for n in ("A", "B", "C", "D")}
    limited = {"on": False}
    fake_op, calls = _counting_op(monkeypatch, tmp_path, rate_limited=limited)
    op._reset_cache_for_tests(tmp_path)
    op.fetch_onepassword_secrets(references=refs, cache_ttl_seconds=300, binary=fake_op, home_path=tmp_path)
    _expire_disk_entry(tmp_path, refs)

    limited["on"] = True
    calls.clear()
    secrets, warnings = op.fetch_onepassword_secrets(
        references=refs, cache_ttl_seconds=300, binary=fake_op, home_path=tmp_path)
    assert len(calls) == 1
    assert secrets == {n: "value-" + n.lower() for n in refs}, "the unread refs still get their last good value"
    assert any("last good cache" in w for w in warnings)


def test_a_rate_limit_cools_down_later_processes(monkeypatch, tmp_path):
    """A new process during the cooldown serves last good values without asking 1Password."""
    refs = {"A": "op://V/a/F", "B": "op://V/b/F"}
    limited = {"on": False}
    fake_op, calls = _counting_op(monkeypatch, tmp_path, rate_limited=limited)
    op._reset_cache_for_tests(tmp_path)
    op.fetch_onepassword_secrets(references=refs, cache_ttl_seconds=300, binary=fake_op, home_path=tmp_path)
    _expire_disk_entry(tmp_path, refs)

    limited["on"] = True
    op.fetch_onepassword_secrets(references=refs, cache_ttl_seconds=300, binary=fake_op, home_path=tmp_path)
    op._CACHE.clear()  # a separate process shares only the disk
    calls.clear()
    secrets, warnings = op.fetch_onepassword_secrets(
        references=refs, cache_ttl_seconds=300, binary=fake_op, home_path=tmp_path)
    assert calls == []
    assert secrets == {"A": "value-a", "B": "value-b"}
    assert any("cooldown" in w for w in warnings)


def test_the_cooldown_expires(monkeypatch, tmp_path):
    refs = {"A": "op://V/a/F"}
    limited = {"on": False}
    fake_op, calls = _counting_op(monkeypatch, tmp_path, rate_limited=limited)
    op._reset_cache_for_tests(tmp_path)
    op.fetch_onepassword_secrets(references=refs, cache_ttl_seconds=300, binary=fake_op, home_path=tmp_path)
    _expire_disk_entry(tmp_path, refs)
    limited["on"] = True
    op.fetch_onepassword_secrets(references=refs, cache_ttl_seconds=300, binary=fake_op, home_path=tmp_path)

    limited["on"] = False
    op._CACHE.clear()
    calls.clear()
    real_time = time.time
    monkeypatch.setattr(op.time, "time", lambda: real_time() + op._RATE_LIMIT_COOLDOWN_SECONDS + 1)
    secrets, _ = op.fetch_onepassword_secrets(
        references=refs, cache_ttl_seconds=300, binary=fake_op, home_path=tmp_path)
    assert calls == ["op://V/a/F"]
    assert secrets == {"A": "value-a"}


def test_the_cooldown_is_scoped_to_one_identity(monkeypatch, tmp_path):
    """Another token has its own quota, so one token's cooldown must not silence it."""
    refs = {"A": "op://V/a/F"}
    limited = {"on": True}
    fake_op, calls = _counting_op(monkeypatch, tmp_path, rate_limited=limited)
    monkeypatch.setenv("OP_SERVICE_ACCOUNT_TOKEN", "token-one")
    op._reset_cache_for_tests(tmp_path)
    op.fetch_onepassword_secrets(references=refs, cache_ttl_seconds=300, binary=fake_op, home_path=tmp_path)

    limited["on"] = False
    monkeypatch.setenv("OP_SERVICE_ACCOUNT_TOKEN", "token-two")
    calls.clear()
    secrets, _ = op.fetch_onepassword_secrets(
        references=refs, cache_ttl_seconds=300, binary=fake_op, home_path=tmp_path)
    assert calls == ["op://V/a/F"]
    assert secrets == {"A": "value-a"}


def test_a_fresh_cache_hit_ignores_the_cooldown(monkeypatch, tmp_path):
    """The cooldown only suppresses provider calls; fresh cached values are served as usual."""
    refs = {"A": "op://V/a/F"}
    limited = {"on": False}
    fake_op, calls = _counting_op(monkeypatch, tmp_path, rate_limited=limited)
    op._reset_cache_for_tests(tmp_path)
    op.fetch_onepassword_secrets(references=refs, cache_ttl_seconds=300, binary=fake_op, home_path=tmp_path)
    op._record_rate_limit_cooldown(op._cooldown_key(op._auth_fingerprint("OP_SERVICE_ACCOUNT_TOKEN"), ""), tmp_path)
    op._CACHE.clear()
    calls.clear()
    secrets, warnings = op.fetch_onepassword_secrets(
        references=refs, cache_ttl_seconds=300, binary=fake_op, home_path=tmp_path)
    assert calls == [] and secrets == {"A": "value-a"} and warnings == []


def test_the_cooldown_marker_holds_no_secret_material(monkeypatch, tmp_path):
    refs = {"A": "op://V/a/F"}
    limited = {"on": True}
    fake_op, _ = _counting_op(monkeypatch, tmp_path, rate_limited=limited)
    monkeypatch.setenv("OP_SERVICE_ACCOUNT_TOKEN", "ops_super-secret-token")
    op._reset_cache_for_tests(tmp_path)
    op.fetch_onepassword_secrets(references=refs, cache_ttl_seconds=300, binary=fake_op, home_path=tmp_path)
    marker = op._cooldown_path(tmp_path)
    assert marker.exists()
    text = marker.read_text()
    assert "ops_super-secret-token" not in text and "op://" not in text
    assert (marker.stat().st_mode & 0o777) == 0o600


def test_rejected_identity_never_falls_back_to_expired_values(monkeypatch, tmp_path):
    """A revoked or wrong token is a real credential problem, not an outage to paper over."""
    fake_op = tmp_path / "op"
    fake_op.write_text("")
    refs = {"A": "op://V/a/F"}
    failing = {"on": False}
    monkeypatch.setattr(op.subprocess, "run", lambda *a, **k: _err(
        1, "[ERROR] 401: Unauthorized: authentication required") if failing["on"] else _ok("value-a"))
    op._reset_cache_for_tests(tmp_path)
    op.fetch_onepassword_secrets(references=refs, cache_ttl_seconds=300, binary=fake_op, home_path=tmp_path)
    _expire_disk_entry(tmp_path, refs)

    failing["on"] = True
    secrets, _ = op.fetch_onepassword_secrets(
        references=refs, cache_ttl_seconds=300, binary=fake_op, home_path=tmp_path,
    )
    assert secrets == {}


def test_expired_values_of_another_identity_are_never_served(monkeypatch, tmp_path):
    fake_op = tmp_path / "op"
    fake_op.write_text("")
    refs = {"A": "op://V/a/F"}
    failing = {"on": False}
    monkeypatch.setattr(op.subprocess, "run", lambda *a, **k: _err(
        1, "Too many requests") if failing["on"] else _ok("value-a"))
    monkeypatch.setenv("OP_SERVICE_ACCOUNT_TOKEN", "token-one")
    op._reset_cache_for_tests(tmp_path)
    op.fetch_onepassword_secrets(references=refs, cache_ttl_seconds=300, binary=fake_op, home_path=tmp_path)
    _expire_disk_entry(tmp_path, refs)

    failing["on"] = True
    monkeypatch.setenv("OP_SERVICE_ACCOUNT_TOKEN", "token-two")
    secrets, _ = op.fetch_onepassword_secrets(
        references=refs, cache_ttl_seconds=300, binary=fake_op, home_path=tmp_path,
    )
    assert secrets == {}


def test_rejection_that_also_mentions_a_rate_limit_is_a_rejection():
    """Mixed wording must take the eviction path, never the last-good fallback."""
    assert op._classify_op_error("[ERROR] 401 Unauthorized: too many requests") == op.ErrorKind.AUTH_FAILED
    assert op._classify_op_error(
        "[ERROR] 2026/09/24 19:40:01 Too many requests. Your client has been rate-limited.") == op.ErrorKind.RATE_LIMITED


def _fake_batch_binary(tmp_path, *, failure=""):
    """Executable fake op: supports run and read without network or stdout secrets."""
    binary = tmp_path / "op"
    binary.write_text('''#!/usr/bin/env python3
import json, os, subprocess, sys
from pathlib import Path
args = sys.argv[1:]
with (Path(__file__).parent / "calls.jsonl").open("a") as log:
    log.write(json.dumps(args[:2]) + "\\n")
if args[0] == "run":
    if (Path(__file__).parent / "failure.txt").exists():
        print((Path(__file__).parent / "failure.txt").read_text(), file=sys.stderr)
        sys.exit(1)
    path = args[args.index("--env-file") + 1]
    mapping = dict(line.split("=", 1) for line in Path(path).read_text().splitlines())
    assert (Path(path).stat().st_mode & 0o777) == 0o600
    assert (Path(args[args.index("--") + 4]).stat().st_mode & 0o777) == 0o600
    assert len(mapping) == 2  # duplicate refs are fetched only once
    values = {name: {"op://V/I/a": "first\\nsecond", "op://V/I/b": "other"}.get(ref, "")
              for name, ref in mapping.items()}
    if not all(values.values()):
        print("missing field", file=sys.stderr)
        sys.exit(1)
    child = args[args.index("--") + 1:]
    sys.exit(subprocess.call(child, env={**os.environ, **values}))
if args[0] == "read":
    ref = args[-1]
    value = {"op://V/I/a": "first\\nsecond", "op://V/I/b": "other"}.get(ref)
    if value is None:
        print("missing field", file=sys.stderr)
        sys.exit(1)
    print(value)
''')
    binary.chmod(0o755)
    return binary


def _fake_calls(tmp_path):
    return [json.loads(line) for line in (tmp_path / "calls.jsonl").read_text().splitlines()]


def test_batch_success_multiline_duplicate_refs_and_private_files(monkeypatch, tmp_path):
    binary = _fake_batch_binary(tmp_path)
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    monkeypatch.setenv("TMPDIR", str(scratch))
    refs = {"A": "op://V/I/a", "COPY": "op://V/I/a", "B": "op://V/I/b"}
    secrets, warnings = op.fetch_onepassword_secrets(references=refs, binary=binary, use_cache=False)
    assert secrets == {"A": "first\nsecond", "COPY": "first\nsecond", "B": "other"}
    assert warnings == []
    assert len(_fake_calls(tmp_path)) == 1
    assert _fake_calls(tmp_path)[0][0] == "run"
    assert list(scratch.iterdir()) == []


def test_batch_failure_falls_back_to_per_ref(monkeypatch, tmp_path):
    binary = _fake_batch_binary(tmp_path)
    (tmp_path / "failure.txt").write_text("missing field")
    secrets, warnings = op.fetch_onepassword_secrets(
        references={"A": "op://V/I/a", "BAD": "op://V/I/missing"}, binary=binary, use_cache=False)
    assert secrets == {"A": "first\nsecond"}
    assert len(warnings) == 1 and "missing" in warnings[0]
    assert [call[0] for call in _fake_calls(tmp_path)] == ["run", "read", "read"]


def test_batch_rate_limit_does_not_fall_back_and_cools_down(monkeypatch, tmp_path):
    binary = _fake_batch_binary(tmp_path)
    (tmp_path / "failure.txt").write_text("Too many requests. Your client has been rate-limited")
    refs = {"A": "op://V/I/a", "B": "op://V/I/b"}
    first, warnings = op.fetch_onepassword_secrets(
        references=refs, binary=binary, home_path=tmp_path)
    assert first == {} and any("rate-limited" in w for w in warnings)
    assert [call[0] for call in _fake_calls(tmp_path)] == ["run"]
    second, warnings = op.fetch_onepassword_secrets(
        references=refs, binary=binary, home_path=tmp_path)
    assert second == {} and any("cooldown" in w for w in warnings)
    assert len(_fake_calls(tmp_path)) == 1
    assert (op._cooldown_path(tmp_path).stat().st_mode & 0o777) == 0o600


def test_reason_survives_a_long_item_path(monkeypatch, tmp_path):
    """op names the vault and item before the reason; a long item name must not hide it."""
    fake_op = tmp_path / "op"
    fake_op.write_text("")
    ref = "op://Default/MERCURY_API_KEY_HOST_HOME_CLEANERS_MYRTLE_BEACH/credential"
    stderr = ("[ERROR] 2026/09/24 21:50:11 could not read secret '" + ref + "': could not get item "
              "Default/MERCURY_API_KEY_HOST_HOME_CLEANERS_MYRTLE_BEACH: Too many requests. "
              "Your client has been rate-limited. Try again in  seconds")
    monkeypatch.setattr(op.subprocess, "run", lambda *a, **k: _err(1, stderr))
    with pytest.raises(RuntimeError) as exc:
        op._run_op_read(fake_op, ref)
    assert op._classify_op_error(str(exc.value)) == op.ErrorKind.RATE_LIMITED
