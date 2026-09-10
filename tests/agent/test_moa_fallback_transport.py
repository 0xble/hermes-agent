"""Exercise config -> frozen council -> real auxiliary HTTP requests, no provider mocks."""
import copy
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace

import pytest
import yaml


@pytest.mark.parametrize("exhausted", [False, True])
def test_frozen_chains_transport_and_aggregator_isolation(tmp_path, monkeypatch, exhausted):
    from agent import moa_loop
    from hermes_cli.moa_config import normalize_moa_config, validate_moa_payload
    calls = []
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            request = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            if not self.path.endswith("/chat/completions"):
                self.send_error(404)
                return
            calls.append(request)
            model = request["model"]
            status = 429 if model in {"ref-primary", "agg-primary"} or (exhausted and model == "agg-secondary") else 200
            body = {"error": {"message": "rate limited", "type": "rate_limit_error"}} if status != 200 else {
                "id": "fixture", "object": "chat.completion", "model": model,
                "choices": [{"index": 0, "message": {"role": "assistant", "content": "fixture advice"}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
            }
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps(body).encode())
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    def slot(model):
        return {"provider": "custom:fixture", "model": model}
    ref = {**slot("ref-primary"), "fallback_models": [slot("ref-secondary")]}
    agg = {**slot("agg-primary"), "fallback_models": [slot("agg-secondary")]}
    moa = {"reference_models": [slot("ref-independent"), ref], "aggregator": agg}
    config = {"providers": {"fixture": {"name": "fixture", "base_url": f"http://127.0.0.1:{server.server_port}/v1", "api_key": "fixture-token", "api_mode": "chat_completions"}},
              "model": {"provider": "custom:fixture", "default": "never-main-substitute"},
              "moa": moa, "auxiliary": {"transient_retries": 0}}
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("OPENAI_API_KEY", "fixture-token")
    (tmp_path / "config.yaml").write_text(yaml.safe_dump(config), encoding="utf-8")
    try:
        assert not validate_moa_payload(moa)
        assert normalize_moa_config(normalize_moa_config(moa)) == normalize_moa_config(moa)
        frozen = moa_loop.snapshot_moa_preset("default")
        metadata = frozen.metadata()
        assert "fixture-token" not in json.dumps(metadata)
        restored = moa_loop.restore_moa_preset(metadata)
        # Editing live config after launch cannot replace frozen fallback authority.
        changed = copy.deepcopy(config)
        changed["moa"]["aggregator"]["fallback_models"] = [slot("unapproved")]
        (tmp_path / "config.yaml").write_text(yaml.safe_dump(changed), encoding="utf-8")
        client = moa_loop.MoAClient("default", agent=SimpleNamespace(_moa_preset_snapshot=restored))
        if exhausted:
            with pytest.raises(RuntimeError, match="chain exhausted"):
                client.chat.completions.create(messages=[{"role": "user", "content": "Advise"}])
        else:
            result = client.chat.completions.create(messages=[{"role": "user", "content": "Advise"}])
            assert result.choices[0].message.content == "fixture advice"
            assert client.last_aggregator_slot["model"] == "agg-secondary"
        models = [r["model"] for r in calls]
        # SDK same-route retries may repeat a failed primary; successful references
        # must never rerun merely because the aggregator advances its own chain.
        assert models.count("ref-independent") == models.count("ref-secondary") == 1
        assert set(models) == {"ref-independent", "ref-primary", "ref-secondary", "agg-primary", "agg-secondary"}
        aggregations = [r for r in calls if r["model"].startswith("agg-")]
        assert all(r["messages"] == aggregations[0]["messages"] for r in aggregations)
        assert client.chat.completions._ref_cache_outputs[1][2].model == "ref-secondary"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
