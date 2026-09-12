"""Availability-only whole-utterance fallback through the registered TTS tool."""
import io
import json
import shlex
import threading
import wave
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
import yaml

import tools.tts_tool as tts
from tools.registry import registry


@pytest.fixture(autouse=True)
def isolated_home(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))


@pytest.mark.parametrize("kind", ["requests_connection", "requests_timeout", "openai_connection", "httpx_timeout"])
def test_network_availability_classes(kind):
    import httpx
    import requests
    from openai import APIConnectionError
    from tools.tts_tool_fallback import is_availability_failure
    errors = {
        "requests_connection": requests.exceptions.ConnectionError(),
        "requests_timeout": requests.exceptions.Timeout(),
        "openai_connection": APIConnectionError(request=httpx.Request("POST", "http://localhost/")),
        "httpx_timeout": httpx.ReadTimeout("fixture"),
    }
    assert is_availability_failure(errors[kind])
    assert not is_availability_failure(ValueError("429 in user input is not an outage"))


def test_registered_unavailable_plugin_does_not_disable_primary(monkeypatch):
    from tools.tts_tool_fallback import provider_chain
    from types import SimpleNamespace
    monkeypatch.setattr("tools.tts_tool_plugins._lookup_plugin_provider",
                        lambda name: SimpleNamespace(is_available=lambda: False))
    assert provider_chain({"fallback_providers": ["offline-plugin"]}, "edge") == ["edge", "offline-plugin"]


def wav_bytes():
    output = io.BytesIO()
    with wave.open(output, "wb") as audio:
        audio.setnchannels(1)
        audio.setsampwidth(2)
        audio.setframerate(8000)
        audio.writeframes(b"\x00\x00" * 800)
    return output.getvalue()


@pytest.mark.parametrize("status,should_fallback", [(200, False), (400, False), (401, False),
                                                       (422, False), (429, True), (503, True)])
def test_sdk_failure_routes_only_availability_and_discards_partial_audio(tmp_path, monkeypatch, status, should_fallback):
    pytest.importorskip("elevenlabs")
    requests = []
    fallback_text = []
    audio = wav_bytes()
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass
        def do_POST(self):
            payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            requests.append(payload["text"])
            # The first chunk succeeded; a later failure must discard it and
            # restart the WHOLE normalized utterance on the fallback.
            code = 200 if len(requests) == 1 else status
            body = audio if code == 200 else b'{"detail":{"status":"fixture","message":"fixture failure"}}'
            self.send_response(code)
            self.send_header("Content-Type", "audio/wav" if code == 200 else "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    config = {"tts": {"provider": "elevenlabs", "fallback_providers": ["edge"],
                      "elevenlabs": {"base_url": f"http://127.0.0.1:{server.server_port}",
                                     "max_text_length": 12}, "edge": {"max_text_length": 5000}}}
    (tmp_path / "config.yaml").write_text(yaml.safe_dump(config), encoding="utf-8")
    monkeypatch.setenv("ELEVENLABS_API_KEY", "fixture-not-a-secret")
    # Edge's network boundary is replaced; config, SDK/HTTP primary, registered
    # handler, chunking, artifact cleanup, and final delivery remain real.
    def edge(text, path, config):
        fallback_text.append(text)
        Path(path).write_bytes(audio)
    monkeypatch.setattr(tts, "_run_edge_tts", edge)
    monkeypatch.setattr(tts, "_import_edge_tts", lambda: object())
    text = "First part. Second part. Third part."
    try:
        result = json.loads(registry.get_entry("text_to_speech").handler(
            {"text": text, "output_path": str(tmp_path / "speech.wav")}))
    finally:
        server.shutdown()
        server.server_close()
        thread.join()
    assert result["success"] is (should_fallback or status == 200)
    assert fallback_text == ([text] if should_fallback else [])
    if should_fallback:
        assert result["provider"] == "edge"
        assert result["fallback_from"] == "elevenlabs"
        assert result["attempted_providers"] == ["elevenlabs", "edge"]
        assert Path(result["file_path"]).read_bytes() == audio
    if status != 200:
        assert not list(tmp_path.glob("speech.chunk*"))


@pytest.mark.parametrize("fallbacks,override,available,success", [
    (["edge"], None, True, True), (["edge", "edge"], None, True, True),
    ([], None, False, False), ("edge", None, False, False),
    (["typo"], None, False, False), (["edge"], "elevenlabs", True, False),
])
def test_missing_primary_and_explicit_override_respect_chain(tmp_path, monkeypatch, fallbacks, override, available, success):
    config = {"tts": {"provider": "elevenlabs", "fallback_providers": fallbacks}}
    (tmp_path / "config.yaml").write_text(yaml.safe_dump(config), encoding="utf-8")
    monkeypatch.setattr(tts, "_resolve_provider_key", lambda *args: "")
    monkeypatch.setattr(tts, "_import_edge_tts", lambda: object())
    monkeypatch.setattr(tts, "_import_elevenlabs", lambda: object())
    monkeypatch.setattr(tts, "_run_edge_tts", lambda text, path, cfg: Path(path).write_bytes(wav_bytes()))
    assert tts.check_tts_requirements() is available
    args = {"text": "Fallback fixture.", "output_path": str(tmp_path / "speech.wav")}
    if override:
        args["provider"] = override
    result = json.loads(registry.get_entry("text_to_speech").handler(args))
    assert result["success"] is success
    if success:
        assert result["provider"] == "edge"
        assert result["attempted_providers"] == ["elevenlabs", "edge"]


@pytest.mark.parametrize("command,override,should_fallback", [
    ("hermes_missing_synthesis_fixture_01a09205 {output_path}", False, True),
    ("VOICE=fixture hermes_missing_synthesis_fixture_01a09205 {output_path}", False, True),
    ("./hermes_missing_synthesis_fixture_01a09205 {output_path}", False, True),
    ("hermes_missing_synthesis_fixture_01a09205 {output_path}", True, False),
    ("exit 127", False, False),
    ("printf 'not found' >&2; exit 127", False, False),
    ("printf 'invalid input' >&2; exit 2", False, False),
    ("printf 'no output file'", False, False),
    ("hermes_missing_synthesis_fixture_01a09205 | cat", False, False),
])
def test_real_command_dependency_failure_respects_fallback_boundary(tmp_path, monkeypatch, command, override, should_fallback):
    config = {"tts": {"provider": "fixture-command", "fallback_providers": ["edge"],
                      "providers": {"fixture-command": {"type": "command", "command": command,
                                                        "output_format": "wav"}}}}
    (tmp_path / "config.yaml").write_text(yaml.safe_dump(config), encoding="utf-8")
    fallback_text = []
    def edge(text, path, cfg):
        fallback_text.append(text)
        Path(path).write_bytes(wav_bytes())
    monkeypatch.setattr(tts, "_run_edge_tts", edge)
    monkeypatch.setattr(tts, "_import_edge_tts", lambda: object())
    args = {"text": "Full fallback utterance.", "output_path": str(tmp_path / "speech.wav")}
    if override:
        args["provider"] = "fixture-command"
    result = json.loads(registry.get_entry("text_to_speech").handler(args))
    assert bool(result.get("success")) is should_fallback
    assert fallback_text == ([args["text"]] if should_fallback else [])
    if should_fallback:
        assert result["attempted_providers"] == ["fixture-command", "edge"]
        assert Path(result["file_path"]).read_bytes() == wav_bytes()


def test_command_fallback_discards_partial_chunks_and_restarts_whole_utterance(tmp_path, monkeypatch):
    marker = shlex.quote(str(tmp_path / "first-generated"))
    source = tmp_path / "source.wav"
    source.write_bytes(wav_bytes())
    command = (f"if [ -e {marker} ]; then hermes_missing_synthesis_fixture_01a09205; "
               f"else printf generated > {marker}; cp {shlex.quote(str(source))} {{output_path}}; fi")
    config = {"tts": {"provider": "fixture-command", "fallback_providers": ["edge"],
                      "providers": {"fixture-command": {"type": "command", "command": command,
                                                        "output_format": "wav", "max_text_length": 12}}}}
    (tmp_path / "config.yaml").write_text(yaml.safe_dump(config), encoding="utf-8")
    fallback_text = []
    def edge(text, path, cfg):
        fallback_text.append(text)
        Path(path).write_bytes(wav_bytes())
    monkeypatch.setattr(tts, "_run_edge_tts", edge)
    monkeypatch.setattr(tts, "_import_edge_tts", lambda: object())
    text = "First part. Second part. Third part."
    result = json.loads(registry.get_entry("text_to_speech").handler(
        {"text": text, "output_path": str(tmp_path / "speech.wav")}))
    assert result["success"] is True
    assert fallback_text == [text]
    assert result["fallback_from"] == "fixture-command"
    assert not list(tmp_path.glob("speech.chunk*"))


def test_output_path_failure_never_switches_command_provider(tmp_path, monkeypatch):
    config = {"tts": {"provider": "fixture-command", "fallback_providers": ["edge"],
                      "providers": {"fixture-command": {"type": "command", "command": "printf fixture"}}}}
    (tmp_path / "config.yaml").write_text(yaml.safe_dump(config), encoding="utf-8")
    blocked_parent = tmp_path / "not-a-directory"
    blocked_parent.write_text("existing file")
    fallback_text = []
    monkeypatch.setattr(tts, "_run_edge_tts", lambda *args: fallback_text.append(args))
    monkeypatch.setattr(tts, "_import_edge_tts", lambda: object())
    with pytest.raises(OSError):
        registry.get_entry("text_to_speech").handler(
            {"text": "Keep the configured route.", "output_path": str(blocked_parent / "speech.wav")})
    assert fallback_text == []
