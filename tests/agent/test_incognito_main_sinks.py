"""Privacy boundaries on the admission and observability owners extracted by main."""

import asyncio

from agent.incognito import incognito_scope, register_session


def test_private_turn_cannot_create_relay_host_or_shared_metrics(monkeypatch):
    from agent import relay_runtime
    from hermes_cli.observability import relay_shared_metrics

    def unexpected_profile_lookup():
        raise AssertionError("private instrumentation reached profile discovery")

    monkeypatch.setattr(relay_runtime, "current_profile_key", unexpected_profile_lookup)
    registry = relay_runtime.RelayHostRegistry()
    with incognito_scope():
        assert registry.for_profile() is None
        assert not relay_runtime.relay_instrumentation_enabled()
        assert not relay_shared_metrics.enabled()
    assert not registry._hosts


def test_cached_relay_lease_is_revoked_by_private_session():
    from agent import relay_runtime

    host = object.__new__(relay_runtime.RelayRuntime)
    lease = relay_runtime.ConversationLease(
        profile_key="default", session_id="session", platform="telegram",
        host=host, session=object.__new__(relay_runtime.RelaySession),
    )
    assert lease.live_runtime() is host
    register_session("session", True)
    try:
        assert lease.live_runtime() is None
    finally:
        register_session("session", False)
    assert lease.live_runtime() is host


def test_private_inbound_stays_off_durable_authority():
    from gateway.config import Platform
    from gateway.platforms.event import MessageEvent
    from gateway.run_inbound import GatewayInboundMixin
    from gateway.session import SessionSource

    runner = object.__new__(GatewayInboundMixin)
    source = SessionSource(platform=Platform.TELEGRAM, chat_id="chat")
    event = MessageEvent(text="saved", source=source)
    with incognito_scope():
        assert asyncio.run(runner._hm_authority_admission(
            event, source, "topic", False,
        )) == (False, None)


def test_delayed_native_result_and_worker_receipt_are_withheld(tmp_path):
    from hermes_state import SessionDB
    import hermes_state_runtime as rt

    db = SessionDB(tmp_path / "state.db")
    try:
        epoch = rt.begin_runtime_epoch(db, instance_id="test")
        db.create_session("session", "telegram")
        admitted = rt.admit_session_input(db,
            epoch=epoch, principal_id="telegram:topic", session_id="session",
            request_id="request", payload={"text": "saved"},
        )
        connection = db._conn
        assert connection is not None
        changes = connection.total_changes
        register_session("session", True)
        try:
            assert rt.settle_session_input(db,
                epoch=epoch, admission_id=admitted["admission_id"], generation=1,
                outcome="completed", result={"response": "saved"},
            ) == {}
            def unexpected_handler(*_args):
                raise AssertionError("private receipt reached a SQL mutation handler")

            assert rt.apply_worker_receipt(db,
                epoch=epoch, execution_id="worker", session_id="session", generation=1,
                sequence=1, operation="messages", payload={"messages": []},
                handlers={"messages": unexpected_handler},
            ) == {}
            assert connection.total_changes == changes
            assert rt.get_session_admission(db, admission_id=admitted["admission_id"])["status"] == "queued"
        finally:
            register_session("session", False)
        claimed = rt.claim_session_input(db, epoch=epoch, session_id="session")
        settled = rt.settle_session_input(db,
            epoch=epoch, admission_id=admitted["admission_id"], generation=claimed["generation"],
            outcome="completed", result={"response": "saved"},
        )
        assert settled["status"] == "terminal"
    finally:
        db.close()


def test_native_result_carries_identity_across_privacy_transition(tmp_path, monkeypatch):
    from hermes_state import SessionDB
    import hermes_state_runtime as rt

    db = SessionDB(tmp_path / "state.db")
    try:
        epoch = rt.begin_runtime_epoch(db, instance_id="test")
        db.create_session("session", "telegram")
        admitted = rt.admit_session_input(db,
            epoch=epoch, principal_id="telegram:topic", session_id="session",
            request_id="request", payload={"text": "saved"},
        )
        write = db._execute_write
        changes = db._conn.total_changes

        def entering_private(fn, *args, **kwargs):
            register_session("session", True)
            return write(fn, *args, **kwargs)

        monkeypatch.setattr(db, "_execute_write", entering_private)
        try:
            assert rt.settle_session_input(db,
                epoch=epoch, admission_id=admitted["admission_id"], generation=1,
                outcome="completed", result={"response": "withheld"},
            ) == {}
            assert db._conn.total_changes == changes
            assert rt.get_session_admission(db, admission_id=admitted["admission_id"])["status"] == "queued"
        finally:
            register_session("session", False)
    finally:
        db.close()
