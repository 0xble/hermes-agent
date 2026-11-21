"""Billing notices use the existing deduplicating notify sender."""


def test_billing_notice_uses_provider_day_key(monkeypatch):
    from agent import billing_notice

    calls = []
    monkeypatch.setattr(billing_notice.subprocess, "run", lambda *args, **kwargs: calls.append((args, kwargs)))

    class ImmediateThread:
        def start(self):
            self.target()

    def spawn(target, **_kwargs):
        thread = ImmediateThread()
        thread.target = target
        return thread

    monkeypatch.setattr("agent.memory_provider.spawn_context_thread", spawn)
    billing_notice.notify_provider_out_of_credits("custom:openai-proxy")
    assert calls
    argv = calls[0][0][0]
    assert argv[:4] == ("notify", "--json", "--yes", "push")
    assert "custom:openai-proxy out of credits" in argv
    assert any(value.startswith("hermes:provider-credits:custom:openai-proxy:") for value in argv)
