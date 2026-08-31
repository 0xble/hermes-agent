"""HERMES-022: job-scoped native outbound messages for cron."""

from __future__ import annotations

import asyncio
import json
import threading
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from cron import outbound as cron_outbound
from cron.outbound import (
    claim_or_reuse,
    classify_send_result,
    is_cron_messaging_session,
    job_allows_messaging,
    mark_result,
)
from cron.scheduler import (
    _build_job_prompt,
    _resolve_cron_disabled_toolsets,
    _resolve_cron_enabled_toolsets,
)
from cron.jobs import create_job
from tools.send_message_tool import _send_via_adapter, send_message_tool


@pytest.fixture
def tmp_outbound(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr("cron.outbound.OUTBOUND_FILE", tmp_path / "cron" / "outbound.db")
    return tmp_path


class TestJobOptIn:
    def test_default_job_keeps_messaging_disabled(self):
        disabled = _resolve_cron_disabled_toolsets({"id": "plain"}, {})
        assert "messaging" in disabled

    def test_allow_messaging_removes_only_that_jobs_denylist(self):
        opted_in = _resolve_cron_disabled_toolsets(
            {"id": "opted", "allow_messaging": True},
            {},
        )
        default = _resolve_cron_disabled_toolsets({"id": "plain"}, {})
        assert "messaging" not in opted_in
        assert "messaging" in default
        assert "cronjob" in opted_in
        assert "clarify" in opted_in

    def test_profile_denylist_still_blocks_opt_in(self):
        disabled = _resolve_cron_disabled_toolsets(
            {"id": "opted", "allow_messaging": True},
            {"agent": {"disabled_toolsets": ["messaging"]}},
        )
        assert "messaging" in disabled

    def test_opt_in_adds_messaging_to_explicit_toolset_allowlist(self):
        enabled = _resolve_cron_enabled_toolsets(
            {
                "id": "opted",
                "allow_messaging": True,
                "enabled_toolsets": ["terminal"],
            },
            {},
        )
        assert enabled == ["terminal", "messaging"]

    def test_opt_in_adds_messaging_to_default_cron_toolsets(self):
        with patch(
            "hermes_cli.tools_config._get_platform_tools",
            return_value={"terminal", "file"},
        ):
            enabled = _resolve_cron_enabled_toolsets(
                {"id": "opted", "allow_messaging": True},
                {},
            )
        assert enabled == ["file", "terminal", "messaging"]

    def test_prompt_changes_only_for_opted_in_jobs(self):
        opted = _build_job_prompt({"id": "opted", "allow_messaging": True, "prompt": "do work"})
        default = _build_job_prompt({"id": "plain", "prompt": "do work"})
        assert "target='origin'" in opted
        assert "do NOT use send_message" in default
        assert "do NOT use send_message" not in opted


class TestOutboundLedger:
    def test_default_path_resolves_for_each_active_profile(self, tmp_path, monkeypatch):
        monkeypatch.setattr(cron_outbound, "OUTBOUND_FILE", None)
        first = tmp_path / "first"
        second = tmp_path / "second"
        with patch(
            "cron.outbound.get_hermes_home",
            side_effect=[first, second],
        ):
            assert cron_outbound._outbound_file() == first / "cron" / "outbound.db"
            assert cron_outbound._outbound_file() == second / "cron" / "outbound.db"

    def test_two_keys_are_distinct(self, tmp_outbound):
        first = claim_or_reuse(
            job_id="job-1",
            run_id="run-1",
            message_key="automatic-action:one",
            target="origin",
            body="first",
            platform="telegram",
            chat_id="2027045491",
            thread_id="104992",
        )
        second = claim_or_reuse(
            job_id="job-1",
            run_id="run-1",
            message_key="manual-review:one",
            target="origin",
            body="second",
            platform="telegram",
            chat_id="2027045491",
            thread_id="104992",
        )
        assert first["action"] == "claim"
        assert second["action"] == "claim"
        assert first["record"]["message_key"] != second["record"]["message_key"]

    def test_retry_reuses_verified_message(self, tmp_outbound):
        claim_or_reuse(
            job_id="job-1",
            run_id="run-1",
            message_key="automatic-action:one",
            target="origin",
            body="hello",
            platform="telegram",
            chat_id="2027045491",
            thread_id=None,
        )
        mark_result(
            job_id="job-1",
            run_id="run-1",
            message_key="automatic-action:one",
            status="verified",
            transport_message_id="131192",
        )
        reused = claim_or_reuse(
            job_id="job-1",
            run_id="run-1",
            message_key="automatic-action:one",
            target="origin",
            body="hello",
            platform="telegram",
            chat_id="2027045491",
            thread_id=None,
        )
        assert reused["action"] == "reuse"
        assert reused["record"]["status"] == "verified"
        assert reused["record"]["transport_message_id"] == "131192"

    def test_verified_result_cannot_be_downgraded(self, tmp_outbound):
        claim_or_reuse(
            job_id="job-1",
            run_id="run-1",
            message_key="automatic-action:immutable",
            target="origin",
            body="hello",
            platform="telegram",
            chat_id="2027045491",
            thread_id=None,
        )
        mark_result(
            job_id="job-1",
            run_id="run-1",
            message_key="automatic-action:immutable",
            status="verified",
            transport_message_id="131192",
        )

        preserved = mark_result(
            job_id="job-1",
            run_id="run-1",
            message_key="automatic-action:immutable",
            status="failed",
            error="late duplicate callback",
        )

        assert preserved["status"] == "verified"
        assert preserved["transport_message_id"] == "131192"
        assert preserved["error"] is None

    def test_confirmed_failure_can_be_retried(self, tmp_outbound):
        params = {
            "job_id": "job-1",
            "run_id": "run-1",
            "message_key": "automatic-action:retry",
            "target": "origin",
            "body": "hello",
            "platform": "telegram",
            "chat_id": "2027045491",
            "thread_id": None,
        }
        claim_or_reuse(**params)
        mark_result(
            job_id="job-1",
            run_id="run-1",
            message_key="automatic-action:retry",
            status="failed",
            error="confirmed pre-send failure",
        )
        retried = claim_or_reuse(**params)
        assert retried["action"] == "claim"
        assert retried["record"]["status"] == "queued"
        assert retried["record"]["error"] is None
        assert claim_or_reuse(**params)["action"] == "claim"
        started = cron_outbound.begin_send(job_id="job-1", run_id="run-1",
                                           message_key="automatic-action:retry")
        assert started["action"] == "send"
        blocked = cron_outbound.begin_send(job_id="job-1", run_id="run-1",
                                           message_key="automatic-action:retry")
        assert blocked["action"] == "reuse"
        assert blocked["record"]["status"] == "ambiguous"

    def test_same_key_different_body_fails_closed(self, tmp_outbound):
        claim_or_reuse(
            job_id="job-1",
            run_id="run-1",
            message_key="automatic-action:one",
            target="origin",
            body="hello",
            platform="telegram",
            chat_id="2027045491",
            thread_id=None,
        )
        with pytest.raises(ValueError, match="different body or target"):
            claim_or_reuse(
                job_id="job-1",
                run_id="run-1",
                message_key="automatic-action:one",
                target="origin",
                body="changed",
                platform="telegram",
                chat_id="2027045491",
                thread_id=None,
            )

    def test_classify_unconfirmed_result_is_ambiguous(self):
        assert classify_send_result({"ok": True})["status"] == "ambiguous"
        assert classify_send_result({"error": "timeout"})["status"] == "ambiguous"
        assert classify_send_result({"error": "no socket", "delivery_stage": "pre_send"})["status"] == "failed"


class TestSendGate:
    @pytest.fixture(autouse=True)
    def _restore_session_context(self):
        from gateway.session_context import _VAR_MAP

        tokens = [(var, var.set(var.get())) for var in _VAR_MAP.values()]
        try:
            yield
        finally:
            for var, token in reversed(tokens):
                var.reset(token)

    def _bind_cron(self, monkeypatch, *, allow=True):
        from contextlib import nullcontext
        from gateway.session_context import _VAR_MAP
        import cron.jobs

        _VAR_MAP["HERMES_CRON_FIRE_OWNER"].set("owner-1")
        _VAR_MAP["HERMES_SESSION_PROFILE"].set("default")
        monkeypatch.setattr(
            cron.jobs,
            "fire_claim_fence",
            lambda *args, **kwargs: nullcontext(True),
        )
        import cron.outbound
        monkeypatch.setattr(cron.outbound, "live_fire_claim_matches", lambda *args: True)
        _VAR_MAP["HERMES_CRON_SESSION"].set("1")
        _VAR_MAP["HERMES_CRON_ALLOW_MESSAGING"].set("1" if allow else "")
        _VAR_MAP["HERMES_CRON_JOB_ID"].set("job-1")
        _VAR_MAP["HERMES_CRON_RUN_ID"].set("run-1")
        _VAR_MAP["HERMES_CRON_AUTO_DELIVER_PLATFORM"].set("telegram")
        _VAR_MAP["HERMES_CRON_AUTO_DELIVER_CHAT_ID"].set("2027045491")
        _VAR_MAP["HERMES_CRON_AUTO_DELIVER_THREAD_ID"].set("104992")
        monkeypatch.setenv("HERMES_CRON_SESSION", "1")

    def test_non_origin_target_is_rejected(self, tmp_outbound, monkeypatch):
        self._bind_cron(monkeypatch)
        raw = send_message_tool({
            "target": "telegram:8868177922",
            "message": "payroll",
            "message_key": "manual-review:sharon",
        })
        payload = json.loads(raw)
        assert payload.get("error")
        assert "origin" in payload["error"]

    @pytest.mark.parametrize("action", ["list", "react", "unreact"])
    def test_non_send_actions_are_rejected(self, tmp_outbound, monkeypatch, action):
        self._bind_cron(monkeypatch)
        payload = json.loads(send_message_tool({"action": action}))
        assert payload.get("error")
        assert "only action='send'" in payload["error"]

    def test_account_override_cannot_be_selected(self, tmp_outbound, monkeypatch):
        self._bind_cron(monkeypatch)
        with patch(
            "tools.send_message_tool._handle_send",
            return_value=json.dumps({"success": True, "message_id": "131192"}),
        ) as send_mock:
            payload = json.loads(send_message_tool({
                "target": "origin",
                "message": "payroll",
                "message_key": "manual-review:sharon-2",
                "account": "brianle",
            }))
        send_mock.assert_called_once()
        sent_args = send_mock.call_args[0][0]
        assert sent_args["target"] == "telegram:2027045491:104992"
        assert "account" not in sent_args
        assert payload["status"] == "verified"

    def test_cron_send_carries_session_profile_to_transport(self, tmp_outbound, monkeypatch):
        self._bind_cron(monkeypatch)
        from gateway.session_context import _VAR_MAP

        _VAR_MAP["HERMES_SESSION_PROFILE"].set("secondary")
        with patch(
            "tools.send_message_tool._handle_send",
            return_value=json.dumps({"success": True, "message_id": "131193"}),
        ) as send_mock:
            payload = json.loads(send_message_tool({
                "target": "origin",
                "message": "profile scoped",
                "message_key": "automatic-action:profile",
            }))
        assert payload["status"] == "verified"
        assert send_mock.call_args.args[0]["_profile"] == "secondary"

    def test_live_transport_uses_secondary_profile_adapter(self, monkeypatch):
        from gateway.config import Platform

        default_adapter = SimpleNamespace(send=AsyncMock())
        secondary_adapter = SimpleNamespace(
            send=AsyncMock(
                return_value=SimpleNamespace(
                    success=True,
                    message_id="secondary-message",
                    error=None,
                )
            )
        )
        runner = SimpleNamespace(
            adapters={Platform.TELEGRAM: default_adapter},
            _profile_adapters={"secondary": {Platform.TELEGRAM: secondary_adapter}},
            _active_profile_name=lambda: "default",
        )
        monkeypatch.setattr("gateway.run._gateway_runner_ref", lambda: runner)

        async def exercise():
            runner._gateway_loop = asyncio.get_running_loop()
            return await _send_via_adapter(
                Platform.TELEGRAM,
                SimpleNamespace(),
                "2027045491",
                "hello",
                profile="secondary",
            )

        result = asyncio.run(exercise())

        assert result == {"success": True, "message_id": "secondary-message"}
        secondary_adapter.send.assert_awaited_once()
        default_adapter.send.assert_not_awaited()

    def test_literal_default_profile_does_not_use_nondefault_active_adapter(self, monkeypatch):
        from gateway.config import Platform

        active_adapter = SimpleNamespace(send=AsyncMock())
        default_adapter = SimpleNamespace(
            send=AsyncMock(
                return_value=SimpleNamespace(
                    success=True,
                    message_id="default-message",
                    error=None,
                )
            )
        )
        runner = SimpleNamespace(
            adapters={Platform.TELEGRAM: active_adapter},
            _profile_adapters={"default": {Platform.TELEGRAM: default_adapter}},
            _active_profile_name=lambda: "secondary",
        )
        monkeypatch.setattr("gateway.run._gateway_runner_ref", lambda: runner)

        async def exercise():
            runner._gateway_loop = asyncio.get_running_loop()
            return await _send_via_adapter(
                Platform.TELEGRAM,
                SimpleNamespace(),
                "2027045491",
                "hello",
                profile="default",
            )

        result = asyncio.run(exercise())

        assert result == {"success": True, "message_id": "default-message"}
        default_adapter.send.assert_awaited_once()
        active_adapter.send.assert_not_awaited()

    def test_explicit_missing_profile_adapter_fails_closed(self, monkeypatch):
        from gateway.config import Platform

        runner = SimpleNamespace(
            adapters={Platform.TELEGRAM: SimpleNamespace(send=AsyncMock())},
            _profile_adapters={},
            _active_profile_name=lambda: "secondary",
        )
        monkeypatch.setattr("gateway.run._gateway_runner_ref", lambda: runner)

        result = asyncio.run(_send_via_adapter(
            Platform.TELEGRAM,
            SimpleNamespace(),
            "2027045491",
            "hello",
            profile="default",
        ))

        assert result == {
            "error": "No live adapter for profile 'default' and platform 'telegram'"
        }
        runner.adapters[Platform.TELEGRAM].send.assert_not_awaited()

    def test_profile_bound_standalone_send_uses_matching_active_profile(self, monkeypatch):
        from gateway.config import Platform
        from tools.send_message_tool import _send_to_platform

        sender = AsyncMock(return_value={"success": True, "message_id": "standalone"})
        entry = SimpleNamespace(standalone_sender_fn=sender)
        monkeypatch.setattr("gateway.run._gateway_runner_ref", lambda: None)
        monkeypatch.setattr("gateway.platform_registry.platform_registry.get", lambda _name: entry)
        monkeypatch.setattr("hermes_cli.profiles.get_active_profile_name", lambda: "default")

        result = asyncio.run(_send_to_platform(
            Platform.TELEGRAM,
            SimpleNamespace(),
            "2027045491",
            "hello",
            profile="default",
        ))

        assert result == {"success": True, "message_id": "standalone"}
        sender.assert_awaited_once()

    def test_profile_bound_standalone_send_rejects_profile_mismatch(self, monkeypatch):
        from gateway.config import Platform
        from tools.send_message_tool import _send_to_platform

        sender = AsyncMock(return_value={"success": True, "message_id": "wrong"})
        entry = SimpleNamespace(standalone_sender_fn=sender)
        monkeypatch.setattr("gateway.run._gateway_runner_ref", lambda: None)
        monkeypatch.setattr("gateway.platform_registry.platform_registry.get", lambda _name: entry)
        monkeypatch.setattr("hermes_cli.profiles.get_active_profile_name", lambda: "secondary")

        result = asyncio.run(_send_to_platform(
            Platform.TELEGRAM,
            SimpleNamespace(),
            "2027045491",
            "hello",
            profile="default",
        ))

        assert "refusing cross-profile send" in result["error"]
        sender.assert_not_awaited()

    def test_live_transport_runs_on_gateway_owned_event_loop(self, monkeypatch):
        from gateway.config import Platform

        gateway_loop = asyncio.new_event_loop()
        loop_thread = threading.Thread(target=gateway_loop.run_forever, daemon=True)
        loop_thread.start()

        class LoopBoundAdapter:
            def __init__(self):
                self.send_loop = None

            async def send(self, *, chat_id, content, metadata=None):
                self.send_loop = asyncio.get_running_loop()
                if self.send_loop is not gateway_loop:
                    raise RuntimeError("adapter send is bound to a different event loop")
                return SimpleNamespace(success=True, message_id="gateway-loop-message", error=None)

        adapter = LoopBoundAdapter()
        runner = SimpleNamespace(
            adapters={Platform.TELEGRAM: adapter},
            _profile_adapters={},
            _active_profile_name=lambda: "default",
            _gateway_loop=gateway_loop,
        )
        monkeypatch.setattr("gateway.run._gateway_runner_ref", lambda: runner)

        try:
            result = asyncio.run(_send_via_adapter(
                Platform.TELEGRAM,
                SimpleNamespace(),
                "2027045491",
                "hello",
                profile="default",
            ))
        finally:
            gateway_loop.call_soon_threadsafe(gateway_loop.stop)
            loop_thread.join(timeout=5)
            gateway_loop.close()

        assert result == {"success": True, "message_id": "gateway-loop-message"}
        assert adapter.send_loop is gateway_loop

    def test_live_transport_fails_before_send_when_gateway_loop_is_stopped(self, monkeypatch):
        from gateway.config import Platform

        adapter = SimpleNamespace(send=AsyncMock())
        stopped_loop = asyncio.new_event_loop()
        runner = SimpleNamespace(
            adapters={Platform.TELEGRAM: adapter},
            _profile_adapters={},
            _active_profile_name=lambda: "default",
            _gateway_loop=stopped_loop,
        )
        monkeypatch.setattr("gateway.run._gateway_runner_ref", lambda: runner)

        try:
            result = asyncio.run(_send_via_adapter(
                Platform.TELEGRAM,
                SimpleNamespace(),
                "2027045491",
                "hello",
                profile="default",
            ))
        finally:
            stopped_loop.close()

        assert result == {
            "error": "Live gateway adapter owner loop is not running",
            "delivery_stage": "pre_send",
        }
        adapter.send.assert_not_awaited()

    def test_live_transport_owner_loop_stall_returns_ambiguous_without_late_send(self, monkeypatch):
        from gateway.config import Platform
        import tools.send_message_tool as send_tool

        gateway_loop = asyncio.new_event_loop()
        loop_thread = threading.Thread(target=gateway_loop.run_forever, daemon=True)
        loop_thread.start()
        blocker_started = threading.Event()
        release_blocker = threading.Event()

        def block_loop():
            blocker_started.set()
            release_blocker.wait(timeout=5)

        gateway_loop.call_soon_threadsafe(block_loop)
        assert blocker_started.wait(timeout=5)

        adapter = SimpleNamespace(send=AsyncMock())
        runner = SimpleNamespace(
            adapters={Platform.TELEGRAM: adapter},
            _profile_adapters={},
            _active_profile_name=lambda: "default",
            _gateway_loop=gateway_loop,
        )
        monkeypatch.setattr("gateway.run._gateway_runner_ref", lambda: runner)
        monkeypatch.setattr(send_tool, "_LIVE_ADAPTER_SEND_TIMEOUT_SECONDS", 0.01)

        try:
            result = asyncio.run(_send_via_adapter(
                Platform.TELEGRAM,
                SimpleNamespace(),
                "2027045491",
                "hello",
                profile="default",
            ))
            release_blocker.set()
            gateway_loop.call_soon_threadsafe(gateway_loop.stop)
            loop_thread.join(timeout=5)
        finally:
            release_blocker.set()
            if loop_thread.is_alive():
                gateway_loop.call_soon_threadsafe(gateway_loop.stop)
                loop_thread.join(timeout=5)
            gateway_loop.close()

        assert result == {
            "error": "Live gateway adapter send timed out after dispatch; delivery is ambiguous",
        }
        adapter.send.assert_not_awaited()

    def test_two_native_sends_are_separate(self, tmp_outbound, monkeypatch):
        self._bind_cron(monkeypatch)
        with patch(
            "tools.send_message_tool._handle_send",
            side_effect=[
                json.dumps({"success": True, "message_id": "1"}),
                json.dumps({"success": True, "message_id": "2"}),
            ],
        ) as send_mock:
            first = json.loads(send_message_tool({
                "target": "origin",
                "message": "action done",
                "message_key": "automatic-action:one",
            }))
            second = json.loads(send_message_tool({
                "target": "origin",
                "message": "review this",
                "message_key": "manual-review:one",
            }))
        assert send_mock.call_count == 2
        assert first["message_id"] == "1"
        assert second["message_id"] == "2"

    def test_send_exception_is_recorded_as_ambiguous(self, tmp_outbound, monkeypatch):
        self._bind_cron(monkeypatch)
        with patch(
            "tools.send_message_tool._handle_send",
            side_effect=RuntimeError("secret provider detail"),
        ):
            payload = json.loads(send_message_tool({
                "target": "origin",
                "message": "action done",
                "message_key": "automatic-action:exception",
            }))
        assert payload["success"] is False
        assert payload["status"] == "ambiguous"
        assert payload["error"] == "send engine raised RuntimeError"
        assert "secret provider detail" not in json.dumps(payload)

    def test_job_without_opt_in_cannot_send(self, tmp_outbound, monkeypatch):
        self._bind_cron(monkeypatch, allow=False)
        raw = send_message_tool({
            "target": "origin",
            "message": "payroll",
            "message_key": "manual-review:sharon",
        })
        payload = json.loads(raw)
        assert "not opted in" in payload.get("error", "")

    def test_create_job_persists_allow_messaging(self, tmp_path, monkeypatch):
        monkeypatch.setattr("cron.jobs.CRON_DIR", tmp_path / "cron")
        monkeypatch.setattr("cron.jobs.JOBS_FILE", tmp_path / "cron" / "jobs.json")
        monkeypatch.setattr("cron.jobs.OUTPUT_DIR", tmp_path / "cron" / "output")
        job = create_job(
            prompt="monitor",
            schedule="every 15m",
            allow_messaging=True,
        )
        assert job_allows_messaging(job)
        assert job["allow_messaging"] is True
        default = create_job(prompt="other", schedule="every 15m")
        assert default["allow_messaging"] is False
        assert not is_cron_messaging_session()


class TestLiveAdapterMedia:
    """Profile-bound live-adapter sends must deliver MEDIA attachments.

    The trusted-profile path short-circuits every standalone media branch, so
    the live adapter itself must route extracted attachments through its
    native typed senders instead of silently dropping them while the ledger
    records a verified success.
    """

    def _send(self, monkeypatch, adapter, **kwargs):
        from gateway.config import Platform

        runner = SimpleNamespace(
            adapters={Platform.TELEGRAM: adapter},
            _profile_adapters={},
            _active_profile_name=lambda: "default",
            _gateway_loop=None,
        )
        monkeypatch.setattr("gateway.run._gateway_runner_ref", lambda: runner)

        async def _run():
            # Bind the owner loop to the test loop so the direct-await
            # path (gateway_loop is current_loop) exercises media routing.
            runner._gateway_loop = asyncio.get_running_loop()
            return await _send_via_adapter(
                Platform.TELEGRAM,
                SimpleNamespace(),
                "2027045491",
                kwargs.pop("message"),
                profile="default",
                **kwargs,
            )

        return asyncio.run(_run())


    # NOTE ON SCOPE (2026-08-31): the fork's private media implementation was
    # retired in favour of upstream's shared ``_send_live_adapter_media``,
    # which owns caption splitting, video/voice routing, per-descriptor
    # validation and the inherited-no-op guard, and is covered by
    # tests/tools/test_send_message_target_parse.py. Routing assertions that
    # merely re-tested that shared implementation were removed rather than
    # ported, per the fork patch lifecycle ("adapt or delete duplicate tests").
    # What remains here is the behaviour still owned by this fork: the
    # ambiguous partial-delivery report, and album batching.

    def test_partial_media_failure_reports_ambiguity_not_success(self, monkeypatch, tmp_path):
        """Text delivered + media failed must be an ambiguous error naming the
        partial delivery, never a verified success and never ``pre_send``.

        This is the contract the outbound ledger depends on: a send that
        half-landed must not be retried as if nothing happened, nor recorded
        as fully delivered.
        """
        good = tmp_path / "chart.png"
        good.write_bytes(b"\x89PNG\r\n\x1a\n")
        bad = tmp_path / "report.pdf"
        bad.write_bytes(b"%PDF-1.4")

        class MediaAdapter:
            async def send(self, chat_id, content, metadata=None, **kw):
                return SimpleNamespace(success=True, message_id="m-text", error=None)

            async def send_image_file(self, chat_id, path, **kw):
                return SimpleNamespace(success=True, message_id="m-img", error=None)

            async def send_document(self, chat_id, path, **kw):
                return SimpleNamespace(
                    success=False, message_id=None, error="document upload rejected"
                )

        result = self._send(
            monkeypatch,
            MediaAdapter(),
            message="report attached",
            media_files=[(str(good), False), (str(bad), False)],
        )

        assert "error" in result
        assert "document upload rejected" in result["error"]
        # Ambiguous, not pre_send: the text already reached the platform.
        assert result.get("delivery_stage") != "pre_send"
        assert result["message_id"] == "m-text"
        # The count ships under a name that cannot be misread as success.
        assert result["media_partial_count"] == 1
        assert "media_delivered" not in result

    def test_media_failure_before_any_delivery_is_not_ambiguous(self, monkeypatch, tmp_path):
        """No text and no media delivered is a plain failure, not a partial."""
        bad = tmp_path / "report.pdf"
        bad.write_bytes(b"%PDF-1.4")

        class MediaAdapter:
            async def send(self, chat_id, content, metadata=None, **kw):
                return SimpleNamespace(success=True, message_id=None, error=None)

            async def send_document(self, chat_id, path, **kw):
                return SimpleNamespace(
                    success=False, message_id=None, error="document upload rejected"
                )

        result = self._send(
            monkeypatch,
            MediaAdapter(),
            message="",
            media_files=[(str(bad), False)],
        )

        assert "error" in result
        assert "media_partial_count" not in result
        assert "media_delivered" not in result

    def test_multi_image_send_is_batched_as_one_album(self, monkeypatch, tmp_path):
        """Album batching is fork-owned and must survive the upstream helper.

        Upstream delivers each descriptor individually. On Telegram that turns
        a 3-image album into 3 separate messages, so batching is preserved for
        adapters that implement ``send_multiple_images`` natively.
        """
        paths = []
        for name in ("a.png", "b.png", "c.png"):
            f = tmp_path / name
            f.write_bytes(b"\x89PNG\r\n\x1a\n")
            paths.append(str(f))

        calls = []

        class BatchingAdapter:
            async def send(self, chat_id, content, metadata=None, **kw):
                calls.append("send")
                return SimpleNamespace(success=True, message_id="m-text", error=None)

            async def send_multiple_images(self, chat_id, images, metadata=None, **kw):
                calls.append("images")
                return [SimpleNamespace(success=True, message_id="m-album", error=None)]

            async def send_image_file(self, chat_id, path, **kw):
                calls.append("image")
                return SimpleNamespace(success=True, message_id="m-img", error=None)

        result = self._send(
            monkeypatch,
            BatchingAdapter(),
            message="three charts",
            media_files=[(p, False) for p in paths],
        )

        assert result["success"] is True
        assert result["media_delivered"] is True
        # One batched album call, not three individual image sends.
        assert calls.count("images") == 1
        assert calls.count("image") == 0
