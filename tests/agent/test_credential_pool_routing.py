"""Tests for credential pool preservation through turn config and 429 recovery.

Covers:
1. CLI _resolve_turn_agent_config passes credential_pool to runtime dict
2. Gateway _resolve_turn_agent_config passes credential_pool to runtime dict
3. Eager fallback deferred when credential pool has credentials
4. Eager fallback fires when no credential pool exists
5. Full 429 rotation cycle: retry-same → rotate → exhaust → fallback
6. Failure attribution: the entry matching the failing API key is marked
   exhausted, not whatever pool.current() happens to point at
"""

import json
import time
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# 1. CLI _resolve_turn_agent_config includes credential_pool
# ---------------------------------------------------------------------------

class TestCliTurnRoutePool:
    def test_resolve_turn_includes_pool(self):
        """CLI's _resolve_turn_agent_config must pass credential_pool in runtime."""
        fake_pool = MagicMock(name="FakePool")
        shell = SimpleNamespace(
            model="gpt-5.4",
            api_key="sk-test",
            base_url=None,
            provider="openai-codex",
            requested_provider="my-named-provider",
            api_mode="codex_responses",
            acp_command=None,
            acp_args=[],
            _credential_pool=fake_pool,
            service_tier=None,
        )

        from cli import HermesCLI
        bound = HermesCLI._resolve_turn_agent_config.__get__(shell)
        route = bound("test message")

        assert route["runtime"]["credential_pool"] is fake_pool
        assert route["runtime"]["requested_provider"] == "my-named-provider"
        assert "my-named-provider" in route["signature"]

        shell.requested_provider = "other-named-provider"
        other_route = bound("test message")
        assert other_route["signature"] != route["signature"]


# ---------------------------------------------------------------------------
# 2. Gateway _resolve_turn_agent_config includes credential_pool
# ---------------------------------------------------------------------------

class TestGatewayTurnRoutePool:
    def test_resolve_turn_includes_pool(self):
        """Gateway's _resolve_turn_agent_config must pass credential_pool."""
        from gateway.run import GatewayRunner

        fake_pool = MagicMock(name="FakePool")
        runner = SimpleNamespace(_service_tier=None)
        runtime_kwargs = {
            "api_key": "***",
            "base_url": None,
            "provider": "openai-codex",
            "requested_provider": "openai-codex",
            "api_mode": "codex_responses",
            "command": None,
            "args": [],
            "credential_pool": fake_pool,
        }

        bound = GatewayRunner._resolve_turn_agent_config.__get__(runner)
        route = bound("test message", "gpt-5.4", runtime_kwargs)

        assert route["runtime"]["credential_pool"] is fake_pool
        assert route["runtime"]["requested_provider"] == "openai-codex"


# ---------------------------------------------------------------------------
# 3 & 4. Eager fallback deferred/fires based on credential pool
# ---------------------------------------------------------------------------

class TestEagerFallbackWithPool:
    """Test the eager fallback guard in run_agent.py's error handling loop."""

    def _make_agent(self, has_pool=True, pool_has_creds=True, has_fallback=True):
        """Create a minimal AIAgent mock with the fields needed."""
        from run_agent import AIAgent

        with patch.object(AIAgent, "__init__", lambda self, **kw: None):
            agent = AIAgent()

        agent._credential_pool = None
        if has_pool:
            pool = MagicMock()
            pool.has_available.return_value = pool_has_creds
            agent._credential_pool = pool

        agent._fallback_chain = [{"model": "fallback/model"}] if has_fallback else []
        agent._fallback_index = 0
        agent._try_activate_fallback = MagicMock(return_value=True)
        agent._emit_status = MagicMock()

        return agent

    def test_eager_fallback_deferred_when_pool_has_credentials(self):
        """429 with active pool should NOT trigger eager fallback."""
        agent = self._make_agent(has_pool=True, pool_has_creds=True, has_fallback=True)

        # Simulate the check from run_agent.py lines 7180-7191
        is_rate_limited = True
        if is_rate_limited and agent._fallback_index < len(agent._fallback_chain):
            pool = agent._credential_pool
            pool_may_recover = pool is not None and pool.has_available()
            if not pool_may_recover:
                agent._try_activate_fallback()

        agent._try_activate_fallback.assert_not_called()

    def test_eager_fallback_fires_when_no_pool(self):
        """429 without pool should trigger eager fallback."""
        agent = self._make_agent(has_pool=False, has_fallback=True)

        is_rate_limited = True
        if is_rate_limited and agent._fallback_index < len(agent._fallback_chain):
            pool = agent._credential_pool
            pool_may_recover = pool is not None and pool.has_available()
            if not pool_may_recover:
                agent._try_activate_fallback()

        agent._try_activate_fallback.assert_called_once()

    def test_eager_fallback_fires_when_pool_exhausted(self):
        """429 with exhausted pool should trigger eager fallback."""
        agent = self._make_agent(has_pool=True, pool_has_creds=False, has_fallback=True)

        is_rate_limited = True
        if is_rate_limited and agent._fallback_index < len(agent._fallback_chain):
            pool = agent._credential_pool
            pool_may_recover = pool is not None and pool.has_available()
            if not pool_may_recover:
                agent._try_activate_fallback()

        agent._try_activate_fallback.assert_called_once()


# ---------------------------------------------------------------------------
# 5. Full 429 rotation cycle via _recover_with_credential_pool
# ---------------------------------------------------------------------------

class TestPoolRotationCycle:
    """Verify the retry-same → rotate → exhaust flow in _recover_with_credential_pool."""

    def _make_agent_with_pool(self, pool_entries=3):
        from run_agent import AIAgent

        with patch.object(AIAgent, "__init__", lambda self, **kw: None):
            agent = AIAgent()

        entries = []
        for i in range(pool_entries):
            e = MagicMock(name=f"entry_{i}")
            e.id = f"cred-{i}"
            entries.append(e)

        pool = MagicMock()
        pool.has_credentials.return_value = True
        # Must be set explicitly — MagicMock.provider returns a truthy
        # child mock, which would trigger the provider-mismatch guard.
        pool.provider = ""

        # mark_exhausted_and_rotate returns next entry until exhausted
        self._rotation_index = 0

        def rotate(status_code=None, error_context=None, **_kwargs):
            self._rotation_index += 1
            if self._rotation_index < pool_entries:
                return entries[self._rotation_index]
            pool.has_credentials.return_value = False
            return None

        pool.mark_exhausted_and_rotate = MagicMock(side_effect=rotate)
        agent._credential_pool = pool
        agent._swap_credential = MagicMock()
        agent.log_prefix = ""
        agent.api_key = "test-api-key"
        agent.provider = "test-provider"
        pool.provider = "test-provider"

        return agent, pool, entries

    def test_first_429_sets_retry_flag_no_rotation(self):
        """First 429 should just set has_retried_429=True, no rotation."""
        agent, pool, _ = self._make_agent_with_pool(3)
        recovered, has_retried = agent._recover_with_credential_pool(
            status_code=429, has_retried_429=False
        )
        assert recovered is False
        assert has_retried is True
        pool.mark_exhausted_and_rotate.assert_not_called()

    def test_second_429_rotates_to_next(self):
        """Second consecutive 429 should rotate to next credential."""
        agent, pool, entries = self._make_agent_with_pool(3)
        recovered, has_retried = agent._recover_with_credential_pool(
            status_code=429, has_retried_429=True
        )
        assert recovered is True
        assert has_retried is False  # reset after rotation
        pool.mark_exhausted_and_rotate.assert_called_once_with(status_code=429, error_context=None, api_key_hint="test-api-key", failure_reason="rate_limit")
        agent._swap_credential.assert_called_once_with(entries[1])

    def test_pool_exhaustion_returns_false(self):
        """When all credentials exhausted, recovery should return False."""
        agent, pool, _ = self._make_agent_with_pool(1)
        # First 429 sets flag
        _, has_retried = agent._recover_with_credential_pool(
            status_code=429, has_retried_429=False
        )
        assert has_retried is True

        # Second 429 tries to rotate but pool is exhausted (only 1 entry)
        recovered, _ = agent._recover_with_credential_pool(
            status_code=429, has_retried_429=True
        )
        assert recovered is False

    def test_402_immediate_rotation(self):
        """402 (billing) should immediately rotate, no retry-first."""
        agent, pool, entries = self._make_agent_with_pool(3)
        recovered, has_retried = agent._recover_with_credential_pool(
            status_code=402, has_retried_429=False
        )
        assert recovered is True
        assert has_retried is False
        pool.mark_exhausted_and_rotate.assert_called_once_with(status_code=402, error_context=None, api_key_hint="test-api-key", failure_reason="billing")


    def test_api_key_hint_from_pool_current_when_agent_key_missing(self):
        """api_key_hint should fall back to pool.current().runtime_api_key
        when agent.api_key is not set (#43747)."""
        from run_agent import AIAgent

        with patch.object(AIAgent, "__init__", lambda self, **kw: None):
            agent = AIAgent()

        e0 = MagicMock(name="entry_0")
        e0.id = "cred-0"
        e1 = MagicMock(name="entry_1")
        e1.id = "cred-1"

        pool = MagicMock()
        pool.has_credentials.return_value = True
        pool.provider = "test-provider"
        agent.provider = "test-provider"

        # current entry has a runtime_api_key
        cur_entry = MagicMock()
        cur_entry.runtime_api_key = "pool-current-key"
        pool.current.return_value = cur_entry

        pool.mark_exhausted_and_rotate.return_value = e1
        agent._credential_pool = pool
        agent._swap_credential = MagicMock()
        agent.log_prefix = ""
        # No agent.api_key set — should fall back to pool.current().runtime_api_key

        recovered, has_retried = agent._recover_with_credential_pool(
            status_code=402, has_retried_429=False
        )
        assert recovered is True
        pool.mark_exhausted_and_rotate.assert_called_once_with(
            status_code=402, error_context=None, api_key_hint="pool-current-key",
            failure_reason="billing",
        )


# ---------------------------------------------------------------------------
# 6. Real-pool regression: the hint routes exhaustion to the FAILED entry
# ---------------------------------------------------------------------------

class TestApiKeyHintRealPool:
    """Prove the routing guarantee through the real CredentialPool selector:
    when the failed key differs from the pool's current/first entry, only the
    failed entry is marked exhausted (#43747, wrong-entry marking)."""

    def _seed_pool(self, tmp_path, monkeypatch):
        import json

        hermes_home = tmp_path / "hermes"
        hermes_home.mkdir(parents=True, exist_ok=True)
        (hermes_home / "auth.json").write_text(
            json.dumps(
                {
                    "version": 1,
                    "providers": {},
                    "credential_pool": {
                        "openrouter": [
                            {
                                "id": "cred-healthy",
                                "label": "healthy",
                                "auth_type": "api_key",
                                "priority": 0,
                                "source": "manual",
                                "access_token": "sk-or-healthy",
                            },
                            {
                                "id": "cred-failed",
                                "label": "failed",
                                "auth_type": "api_key",
                                "priority": 1,
                                "source": "manual",
                                "access_token": "sk-or-failed",
                            },
                        ]
                    },
                }
            )
        )
        monkeypatch.setenv("HERMES_HOME", str(hermes_home))
        from agent.credential_pool import load_pool

        return load_pool("openrouter")

    def test_hint_marks_failed_entry_not_current(self, tmp_path, monkeypatch):
        pool = self._seed_pool(tmp_path, monkeypatch)
        # Another process/pool instance issued sk-or-failed; THIS pool's
        # current() would resolve to the first (healthy) entry.
        assert pool.select().access_token == "sk-or-healthy"

        next_entry = pool.mark_exhausted_and_rotate(
            status_code=429,
            error_context={"reason": "rate_limit_exceeded"},
            api_key_hint="sk-or-failed",
        )

        statuses = {e.id: e.last_status for e in pool._entries}
        assert statuses["cred-failed"] == "exhausted"
        assert statuses["cred-healthy"] in (None, "ok")
        assert next_entry is not None
        assert next_entry.access_token == "sk-or-healthy"

    def test_without_hint_current_entry_is_marked(self, tmp_path, monkeypatch):
        """Baseline: no hint falls back to current() — the pre-fix behavior."""
        pool = self._seed_pool(tmp_path, monkeypatch)
        assert pool.select().access_token == "sk-or-healthy"

        pool.mark_exhausted_and_rotate(status_code=429, error_context=None)

        statuses = {e.id: e.last_status for e in pool._entries}
        assert statuses["cred-healthy"] == "exhausted"
        assert statuses["cred-failed"] in (None, "ok")


# ---------------------------------------------------------------------------
# 7. Failure attribution — mark the key that failed, not pool.current()
# ---------------------------------------------------------------------------

class TestFailureAttribution:
    """Regression: recover_with_credential_pool must mark the entry whose API
    key actually produced the failure.

    pool.current() is shared mutable state: round-robin select() advances it,
    concurrent turns move it, and a freshly loaded pool (second process) has
    current() == None — in which case the old code fell through to
    _select_unlocked() and exhausted the NEXT (healthy) entry, copying the
    failing key's error/reset time onto it until the whole pool went offline.
    """

    def _make_pool(self, tmp_path, monkeypatch, entries, provider="anthropic"):
        hermes_home = tmp_path / "hermes"
        hermes_home.mkdir(parents=True, exist_ok=True)
        monkeypatch.setenv("HERMES_HOME", str(hermes_home))
        # Keep host Anthropic/Claude credentials out of this fixture. load_pool()
        # auto-seeds ~/.claude/.credentials.json and env keys when anthropic is
        # explicitly configured on the machine, which turns a deliberate
        # single-entry pool into a multi-entry pool and invalidates isolation
        # assertions (see test_unmatched_key_does_not_retry_only_pool_entry).
        for env_var in (
            "ANTHROPIC_API_KEY",
            "ANTHROPIC_TOKEN",
            "CLAUDE_CODE_OAUTH_TOKEN",
        ):
            monkeypatch.delenv(env_var, raising=False)
        monkeypatch.setattr(
            "hermes_cli.auth.is_provider_explicitly_configured",
            lambda provider: False,
        )
        (hermes_home / "auth.json").write_text(
            json.dumps({"version": 1, "credential_pool": {provider: entries}}),
            encoding="utf-8",
        )
        from agent.credential_pool import load_pool

        pool = load_pool(provider)
        assert [entry.id for entry in pool.entries()] == [
            entry["id"] for entry in entries
        ], "pool fixture leaked host credentials into the test pool"
        return pool

    def _entry(self, idx, key, **overrides):
        entry = {
            "id": f"cred-{idx}",
            "label": f"key-{idx}",
            "auth_type": "api_key",
            "priority": idx,
            "source": "manual",
            "access_token": key,
        }
        entry.update(overrides)
        return entry

    def _agent(self, pool, failing_key, credential_id=None, provider="anthropic"):
        return SimpleNamespace(
            provider=provider,
            api_key=failing_key,
            _credential_pool=pool,
            _credential_pool_entry_id=credential_id,
            _swap_credential=MagicMock(),
        )

    def _statuses(self, pool):
        return {e.id: e.last_status for e in pool.entries()}

    def test_soft_cooldown_steers_normal_selection_without_exhausting(self, tmp_path, monkeypatch):
        """Transient routing state prefers another account but is not durable health."""
        pool = self._make_pool(
            tmp_path, monkeypatch,
            [self._entry(0, "key-a"), self._entry(1, "key-b")],
            provider="openai-codex",
        )

        pool.soft_cooldown("cred-0", reason="overloaded", duration=60)
        selected = pool.select()

        assert selected is not None
        assert selected.id == "cred-1"
        assert self._statuses(pool)["cred-0"] in (None, "ok")
        assert "cred-0" in pool.soft_cooldown_ids()

    def test_soft_cooldown_is_shared_across_pool_instances(self, tmp_path, monkeypatch):
        """Nearby agents in one process observe the same profile-scoped steering."""
        entries = [self._entry(0, "key-a"), self._entry(1, "key-b")]
        first_pool = self._make_pool(
            tmp_path, monkeypatch, entries, provider="openai-codex"
        )
        first_pool.soft_cooldown("cred-0", reason="overloaded", duration=60)

        from agent.credential_pool import load_pool

        second_pool = load_pool("openai-codex")
        selected = second_pool.select()

        assert selected is not None
        assert selected.id == "cred-1"

    def test_soft_cooldown_expiry_restores_fill_first(self, tmp_path, monkeypatch):
        """Expired runtime-only cooldowns restore the configured strategy."""
        pool = self._make_pool(
            tmp_path, monkeypatch,
            [self._entry(0, "key-a"), self._entry(1, "key-b")],
            provider="openai-codex",
        )

        pool.soft_cooldown("cred-0", reason="timeout", duration=0)

        selected = pool.select()
        assert selected is not None
        assert selected.id == "cred-0"
        assert pool.soft_cooldown_ids() == set()

    def test_alternate_selection_prefers_non_cooled_third_account(self, tmp_path, monkeypatch):
        """A second transient failure uses C before reusing cooled A."""
        pool = self._make_pool(
            tmp_path, monkeypatch,
            [
                self._entry(0, "key-a"),
                self._entry(1, "key-b"),
                self._entry(2, "key-c"),
            ],
            provider="openai-codex",
        )
        pool.soft_cooldown("cred-0", reason="overloaded", duration=60)
        pool.soft_cooldown("cred-1", reason="timeout", duration=60)
        agent = self._agent(
            pool, failing_key="key-b", credential_id="cred-1", provider="openai-codex"
        )

        from agent.agent_runtime_helpers import _select_alternate_credential

        alternate = _select_alternate_credential(agent)
        assert alternate is not None
        assert alternate.id == "cred-2"

    def test_all_soft_cooled_accounts_still_allow_emergency_alternate(self, tmp_path, monkeypatch):
        """Soft cooldowns never eliminate the only available alternate."""
        pool = self._make_pool(
            tmp_path, monkeypatch,
            [self._entry(0, "key-a"), self._entry(1, "key-b")],
            provider="openai-codex",
        )
        pool.soft_cooldown("cred-0", reason="overloaded", duration=60)
        pool.soft_cooldown("cred-1", reason="timeout", duration=60)
        agent = self._agent(
            pool, failing_key="key-a", credential_id="cred-0", provider="openai-codex"
        )

        from agent.agent_runtime_helpers import _select_alternate_credential

        alternate = _select_alternate_credential(agent)
        assert alternate is not None
        assert alternate.id == "cred-1"

    def test_soft_cooldown_applies_to_lease_selection(self, tmp_path, monkeypatch):
        pool = self._make_pool(
            tmp_path, monkeypatch,
            [self._entry(0, "key-a"), self._entry(1, "key-b")],
            provider="openai-codex",
        )
        pool.soft_cooldown("cred-0", reason="overloaded", duration=60)

        assert pool.acquire_lease() == "cred-1"

    def test_soft_cooldown_applies_to_peek(self, tmp_path, monkeypatch):
        pool = self._make_pool(
            tmp_path, monkeypatch,
            [self._entry(0, "key-a"), self._entry(1, "key-b")],
            provider="openai-codex",
        )
        assert pool.select() is not None
        pool.soft_cooldown("cred-0", reason="overloaded", duration=60)

        peeked = pool.peek()
        assert peeked is not None
        assert peeked.id == "cred-1"


    def test_pre_exhausted_check_uses_failing_key(self, tmp_path, monkeypatch):
        """The 'already exhausted → rotate immediately' check must inspect the
        failing entry, not pool.current(): first 429 on an already-exhausted
        key rotates without burning a retry."""
        pool = self._make_pool(
            tmp_path, monkeypatch,
            [
                self._entry(0, "key-a"),
                self._entry(
                    1, "key-b",
                    last_status="exhausted",
                    last_status_at=time.time(),
                    last_error_code=429,
                ),
            ],
        )
        agent = self._agent(pool, failing_key="key-b")

        from agent.agent_runtime_helpers import recover_with_credential_pool

        recovered, has_retried = recover_with_credential_pool(
            agent, status_code=429, has_retried_429=False
        )

        assert recovered is True
        assert has_retried is False
        statuses = self._statuses(pool)
        assert statuses["cred-0"] != "exhausted"
        swapped = agent._swap_credential.call_args[0][0]
        assert swapped.id == "cred-0"

    def test_auth_refresh_targets_failing_key_not_pointer(self, tmp_path, monkeypatch):
        """The auth path must refresh the entry that supplied the failing key,
        not current(). With current() pointing at healthy A while key B failed,
        try_refresh_current() force-refreshes A — for non-OAuth entries a
        forced refresh marks the entry exhausted outright — so healthy A dies,
        the hinted rotation then exhausts B, and the pool has nothing left."""
        pool = self._make_pool(
            tmp_path, monkeypatch,
            [self._entry(0, "key-a"), self._entry(1, "key-b")],
        )
        # Point the shared cursor at the healthy entry, as a concurrent
        # turn's select() would.
        selected = pool.select()
        assert selected.id == "cred-0"
        assert pool.current().id == "cred-0"

        agent = self._agent(pool, failing_key="key-b")
        agent._is_entitlement_failure = MagicMock(return_value=False)

        from agent.agent_runtime_helpers import recover_with_credential_pool

        recovered, _ = recover_with_credential_pool(
            agent, status_code=401, has_retried_429=False
        )

        assert recovered is True
        statuses = self._statuses(pool)
        assert statuses["cred-1"] == "exhausted"
        assert statuses["cred-0"] != "exhausted"
        swapped = agent._swap_credential.call_args[0][0]
        assert swapped.id == "cred-0"


    def test_unmatched_key_does_not_retry_only_pool_entry(
        self, tmp_path, monkeypatch
    ):
        """Legacy agents without a stable id must stop when an unmatched key
        has no different credential to rotate to."""
        pool = self._make_pool(
            tmp_path, monkeypatch,
            [self._entry(0, "pool-runtime-key")],
            provider="test-isolated-provider",
        )
        agent = self._agent(
            pool,
            failing_key="wrapper-runtime-key",
            provider="test-isolated-provider",
        )
        agent._is_entitlement_failure = MagicMock(return_value=False)

        from agent.agent_runtime_helpers import recover_with_credential_pool

        recovered, _ = recover_with_credential_pool(
            agent, status_code=401, has_retried_429=False
        )

        assert recovered is False
        assert self._statuses(pool)["cred-0"] != "exhausted"
        agent._swap_credential.assert_not_called()

    def test_classified_billing_403_recorded_on_entry(self, tmp_path, monkeypatch):
        """A billing-classified 403 must reach the pool as `billing`, not a bare 403.

        `error_classifier` maps OpenRouter's `key limit exceeded` 403 (and xAI
        spending-limit blocks) to FailoverReason.billing, but the pool only
        ever saw the raw status — so a sole-credential pool gave a spent
        account the 60s transient cooldown and re-failed every minute. The
        recovery path now forwards the classified reason so the pool can size
        the bench correctly.
        """
        from agent.error_classifier import FailoverReason

        pool = self._make_pool(
            tmp_path, monkeypatch,
            [self._entry(0, "key-a"), self._entry(1, "key-b")],
        )
        agent = self._agent(pool, failing_key="key-b")
        agent._is_entitlement_failure = MagicMock(return_value=False)

        from agent.agent_runtime_helpers import recover_with_credential_pool

        recover_with_credential_pool(
            agent,
            status_code=403,
            has_retried_429=False,
            classified_reason=FailoverReason.billing,
        )

        failed = {e.id: e for e in pool.entries()}["cred-1"]
        assert failed.last_status == "exhausted"
        assert failed.failure_reason == "billing"

    def test_unclassified_403_records_no_billing_reason(self, tmp_path, monkeypatch):
        """An unclassified 403 stays transient — no billing verdict is invented."""
        pool = self._make_pool(
            tmp_path, monkeypatch,
            [self._entry(0, "key-a"), self._entry(1, "key-b")],
        )
        agent = self._agent(pool, failing_key="key-b")
        agent._is_entitlement_failure = MagicMock(return_value=False)

        from agent.agent_runtime_helpers import recover_with_credential_pool

        recover_with_credential_pool(
            agent, status_code=403, has_retried_429=False
        )

        failed = {e.id: e for e in pool.entries()}["cred-1"]
        assert failed.failure_reason != "billing"

    @pytest.mark.parametrize(
        "provider",
        ["openai-codex", "test-isolated-provider"],
    )
    @pytest.mark.parametrize(
        "reason_name",
        ["overloaded", "server_error", "timeout"],
    )
    def test_transient_provider_failure_tries_one_alternate_without_exhausting(
        self, tmp_path, monkeypatch, provider, reason_name
    ):
        """Any matching provider pool should try one alternate credential before
        provider fallback without marking either credential exhausted."""
        from agent.error_classifier import FailoverReason

        pool = self._make_pool(
            tmp_path, monkeypatch,
            [self._entry(0, "key-a"), self._entry(1, "key-b")],
            provider=provider,
        )
        agent = self._agent(pool, failing_key="key-a", provider=provider)

        from agent.agent_runtime_helpers import recover_with_credential_pool

        recovered, has_retried = recover_with_credential_pool(
            agent,
            status_code=None,
            has_retried_429=False,
            classified_reason=getattr(FailoverReason, reason_name),
        )

        assert recovered is True
        assert has_retried is False
        assert agent._swap_credential.call_args[0][0].id == "cred-1"
        assert "cred-0" in pool.soft_cooldown_ids()
        assert agent._last_credential_rotation == {
            "from_entry": "cred-0",
            "to_entry": "cred-1",
            "reason": reason_name,
        }
        assert all(status != "exhausted" for status in self._statuses(pool).values())

    def test_transient_failure_prefers_api_key_over_stale_entry_id(
        self, tmp_path, monkeypatch
    ):
        from agent.error_classifier import FailoverReason
        from agent.agent_runtime_helpers import recover_with_credential_pool

        pool = self._make_pool(
            tmp_path, monkeypatch,
            [self._entry(0, "key-a"), self._entry(1, "key-b")],
            provider="openai-codex",
        )
        agent = self._agent(
            pool,
            failing_key="key-b",
            credential_id="cred-0",
            provider="openai-codex",
        )

        recovered, _ = recover_with_credential_pool(
            agent,
            status_code=None,
            has_retried_429=False,
            classified_reason=FailoverReason.overloaded,
        )

        assert recovered is True
        assert pool.soft_cooldown_ids() == {"cred-1"}
        assert agent._swap_credential.call_args[0][0].id == "cred-0"

    def test_provider_overload_uses_each_alternate_once(self, tmp_path, monkeypatch):
        """The same overloaded turn must not cycle through more than one
        same-provider account before the caller escalates to fallback."""
        from agent.error_classifier import FailoverReason

        pool = self._make_pool(
            tmp_path, monkeypatch,
            [
                self._entry(0, "key-a"),
                self._entry(1, "key-b"),
                self._entry(2, "key-c"),
            ],
            provider="openai-codex",
        )
        agent = self._agent(pool, failing_key="key-a")
        agent.provider = "openai-codex"

        from agent.agent_runtime_helpers import recover_with_credential_pool

        first, _ = recover_with_credential_pool(
            agent,
            status_code=None,
            has_retried_429=False,
            classified_reason=FailoverReason.overloaded,
        )
        second, _ = recover_with_credential_pool(
            agent,
            status_code=None,
            has_retried_429=False,
            alternate_credential_attempted=True,
            classified_reason=FailoverReason.overloaded,
        )

        assert first is True
        assert second is False
        assert agent._swap_credential.call_count == 1
        assert agent._last_credential_rotation is None

    def test_failed_alternate_is_cooled_before_provider_fallback(
        self, tmp_path, monkeypatch
    ):
        from agent.error_classifier import FailoverReason
        from agent.agent_runtime_helpers import recover_with_credential_pool

        pool = self._make_pool(
            tmp_path,
            monkeypatch,
            [
                self._entry(0, "key-a"),
                self._entry(1, "key-b"),
                self._entry(2, "key-c"),
            ],
            provider="openai-codex",
        )
        agent = self._agent(pool, failing_key="key-a", provider="openai-codex")

        first, _ = recover_with_credential_pool(
            agent,
            status_code=None,
            has_retried_429=False,
            classified_reason=FailoverReason.overloaded,
        )
        assert first is True

        agent.api_key = "key-b"
        agent._credential_pool_entry_id = "cred-1"
        second, _ = recover_with_credential_pool(
            agent,
            status_code=None,
            has_retried_429=False,
            alternate_credential_attempted=True,
            classified_reason=FailoverReason.timeout,
        )

        assert second is False
        assert {"cred-0", "cred-1"} <= pool.soft_cooldown_ids()
        selected = pool.select()
        assert selected is not None
        assert selected.id == "cred-2"

    def test_transient_alternate_does_not_consume_first_429_retry(
        self, tmp_path, monkeypatch
    ):
        """The alternate-account budget and same-account 429 retry budget are
        independent when failure classes change within one API-call attempt."""
        from agent.error_classifier import FailoverReason

        pool = self._make_pool(
            tmp_path, monkeypatch,
            [self._entry(0, "key-a"), self._entry(1, "key-b")],
            provider="openai-codex",
        )
        agent = self._agent(pool, failing_key="key-a")
        agent.provider = "openai-codex"

        from agent.agent_runtime_helpers import recover_with_credential_pool

        alternate_recovered, has_retried_429 = recover_with_credential_pool(
            agent,
            status_code=None,
            has_retried_429=False,
            alternate_credential_attempted=False,
            classified_reason=FailoverReason.timeout,
        )
        rate_limit_recovered, has_retried_429 = recover_with_credential_pool(
            agent,
            status_code=429,
            has_retried_429=has_retried_429,
            alternate_credential_attempted=True,
            classified_reason=FailoverReason.rate_limit,
        )

        assert alternate_recovered is True
        assert rate_limit_recovered is False
        assert has_retried_429 is True
        assert agent._swap_credential.call_count == 1

    def test_single_credential_overload_falls_through(self, tmp_path, monkeypatch):
        """A one-account pool cannot recover overload by account rotation."""
        from agent.error_classifier import FailoverReason

        pool = self._make_pool(
            tmp_path, monkeypatch,
            [self._entry(0, "key-a")],
            provider="openai-codex",
        )
        agent = self._agent(pool, failing_key="key-a")
        agent.provider = "openai-codex"

        from agent.agent_runtime_helpers import recover_with_credential_pool

        recovered, has_retried = recover_with_credential_pool(
            agent,
            status_code=None,
            has_retried_429=False,
            classified_reason=FailoverReason.overloaded,
        )

        assert recovered is False
        assert has_retried is False
        agent._swap_credential.assert_not_called()

    def test_upstream_aggregator_rate_limit_still_bypasses_account_rotation(
        self, tmp_path, monkeypatch
    ):
        """Aggregator-upstream 429s must continue to bypass same-provider
        account rotation because the credential is healthy."""
        from agent.error_classifier import FailoverReason

        pool = self._make_pool(
            tmp_path, monkeypatch,
            [self._entry(0, "key-a"), self._entry(1, "key-b")],
            provider="openai-codex",
        )
        agent = self._agent(pool, failing_key="key-a")
        agent.provider = "openai-codex"

        from agent.agent_runtime_helpers import recover_with_credential_pool

        recovered, has_retried = recover_with_credential_pool(
            agent,
            status_code=429,
            has_retried_429=False,
            classified_reason=FailoverReason.upstream_rate_limit,
            error_context={"upstream_provider": "DeepSeek"},
        )

        assert recovered is False
        assert has_retried is False
        agent._swap_credential.assert_not_called()

