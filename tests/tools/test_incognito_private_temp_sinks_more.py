"""More temporary-artifact sinks: a private turn stages them only inside its interval.

Same three cases per sink as ``test_incognito_private_temp_sinks`` (registered private session,
incognito turn with no bound interval, public control), through the real resolver. The public
temp directory is redirected into ``tmp_path`` so "nothing reached public temp" is observable.
"""

import asyncio
import base64
import json
import os
import shutil
import sys
import tempfile
import threading
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

import tools.transcription_audio as transcription_audio
import tools.transcription_cloud as transcription_cloud
import tools.transcription_command as transcription_command
import tools.transcription_local as transcription_local
import tools.transcription_tools as transcription_tools
import tools.tts_command_provider as tts_command_provider
import tools.tts_tool_delivery as tts_tool_delivery
import tools.tts_tool_speaker as tts_tool_speaker
from agent import incognito_private_dir as private_dirs
from agent.incognito import incognito_scope, is_incognito, register_session
from tests.tools.test_incognito_private_temp_sinks import PRIVATE, PUBLIC, UNBOUND, _under


@pytest.fixture
def interval():
    """A registered private session and its real interval directory."""
    register_session(PRIVATE)
    with incognito_scope(True, PRIVATE):
        root = private_dirs.private_path_for_current_session()
    assert root is not None and root.is_dir()
    try:
        yield root
    finally:
        register_session(PRIVATE, False)
        private_dirs.discard_private_dir(PRIVATE)


@pytest.fixture
def public_tmp(tmp_path, monkeypatch):
    """The OS temp directory, redirected so leaks into it can be listed."""
    path = tmp_path / "public-temp"
    path.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(path))
    return path


def _leftovers(path: Path) -> list[str]:
    return sorted(str(p) for p in path.rglob("*"))


# --- Local command STT ---------------------------------------------------------------

@pytest.fixture
def local_stt(monkeypatch):
    monkeypatch.setenv(transcription_local.LOCAL_STT_COMMAND_ENV, "stt {input_path} {output_dir}")
    seen = {}

    def fake_run(argv, **_kwargs):
        seen["output_dir"] = argv[2]
        (Path(argv[2]) / "out.txt").write_text("private transcript")
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(transcription_local, "_run_quiet", fake_run)
    return seen


def test_local_command_stt_workdir_in_interval(interval, local_stt, public_tmp, tmp_path):
    source = tmp_path / "voice.wav"
    source.write_bytes(b"wav")
    with incognito_scope(True, PRIVATE):
        result = transcription_local._transcribe_local_command(str(source), "base")
    assert result["success"] and result["transcript"] == "private transcript"
    assert _under(local_stt["output_dir"], interval)
    assert _leftovers(public_tmp) == []


def test_local_command_stt_fails_closed_without_interval(local_stt, public_tmp, tmp_path):
    source = tmp_path / "voice.wav"
    source.write_bytes(b"wav")
    with incognito_scope(True, UNBOUND):
        result = transcription_local._transcribe_local_command(str(source), "base")
    assert not result["success"] and "output_dir" not in local_stt
    assert _leftovers(public_tmp) == []


def test_local_command_stt_public_control(interval, local_stt, public_tmp, tmp_path):
    source = tmp_path / "voice.wav"
    source.write_bytes(b"wav")
    with incognito_scope(False, PUBLIC):
        result = transcription_local._transcribe_local_command(str(source), "base")
    assert result["success"] and _under(local_stt["output_dir"], public_tmp)


# --- Command STT / command TTS (disposable_path_for_current_session used to return None) ----

@pytest.fixture
def command_stt(monkeypatch):
    seen = {}

    def fake_run(command, _timeout, **_kwargs):
        import shlex
        output = Path(shlex.split(command)[1])
        seen["dir"] = str(output.parent)
        output.write_text("private transcript")
        return SimpleNamespace(stdout="", returncode=0)

    monkeypatch.setattr(transcription_command, "_run_command_stt", fake_run)
    return seen


def _command_stt(tmp_path):
    source = tmp_path / "voice.wav"
    source.write_bytes(b"wav")
    return transcription_command._transcribe_command_stt(
        str(source), "cmd", {"command": "stt {output_path}"}, {})


def test_command_stt_workdir_in_interval(interval, command_stt, public_tmp, tmp_path):
    with incognito_scope(True, PRIVATE):
        assert _command_stt(tmp_path)["success"]
    assert _under(command_stt["dir"], interval) and _leftovers(public_tmp) == []


def test_command_stt_fails_closed_without_interval(command_stt, public_tmp, tmp_path):
    with incognito_scope(True, UNBOUND):
        assert not _command_stt(tmp_path)["success"]
    assert "dir" not in command_stt and _leftovers(public_tmp) == []


def test_command_stt_public_control(interval, command_stt, public_tmp, tmp_path):
    with incognito_scope(False, PUBLIC):
        assert _command_stt(tmp_path)["success"]
    assert _under(command_stt["dir"], public_tmp)


@pytest.fixture
def command_tts(monkeypatch):
    seen = {}

    def fake_run(command, _timeout, **_kwargs):
        import shlex
        input_path, output_path = shlex.split(command)[1:3]
        seen["dir"] = str(Path(input_path).parent)
        Path(output_path).write_bytes(b"audio")

    monkeypatch.setattr(tts_command_provider, "run_command_provider", fake_run)
    return seen


def _command_tts(tmp_path):
    return tts_command_provider._generate_command_tts(
        "secret words", str(tmp_path / "out" / "reply.mp3"), "cmd",
        {"command": "tts {input_path} {output_path}"}, {})


def test_command_tts_workdir_in_interval(interval, command_tts, public_tmp, tmp_path):
    with incognito_scope(True, PRIVATE):
        _command_tts(tmp_path)
    assert _under(command_tts["dir"], interval) and _leftovers(public_tmp) == []


def test_command_tts_fails_closed_without_interval(command_tts, public_tmp, tmp_path):
    with incognito_scope(True, UNBOUND), pytest.raises(OSError):
        _command_tts(tmp_path)
    assert "dir" not in command_tts and _leftovers(public_tmp) == []


def test_command_tts_public_control(interval, command_tts, public_tmp, tmp_path):
    with incognito_scope(False, PUBLIC):
        _command_tts(tmp_path)
    assert _under(command_tts["dir"], public_tmp)


# --- Cloud STT container retry -------------------------------------------------------

@pytest.fixture
def cloud_retry(monkeypatch):
    import httpx
    from openai import APIStatusError

    seen = {}
    request = httpx.Request("POST", "https://stt.example/v1/audio/transcriptions")
    rejection = APIStatusError("unsupported container", response=httpx.Response(400, request=request), body=None)

    def create(file, **_kwargs):
        if "dir" not in seen:
            seen["first"] = True
            raise rejection
        return "hello"

    client = SimpleNamespace(audio=SimpleNamespace(transcriptions=SimpleNamespace(create=create)))

    def fake_transcode(path, work_dir):
        seen["dir"] = work_dir
        out = Path(work_dir) / "retry.m4a"
        out.write_bytes(b"m4a")
        return str(out), None

    monkeypatch.setattr(transcription_tools, "_HAS_OPENAI", True)
    monkeypatch.setattr(transcription_cloud, "_transcode_audio_for_stt", fake_transcode)
    monkeypatch.setattr(transcription_cloud, "_with_openai_client",
                        lambda _key, _url, _path, _label, body: body(client))
    return seen


def _cloud(tmp_path):
    source = tmp_path / "voice.ogg"
    source.write_bytes(b"ogg")
    return transcription_cloud._transcribe_openai(
        str(source), "gpt-4o-transcribe", api_key="k", base_url="https://stt.example/v1",
        provider_label="deepinfra", language="en")


def test_cloud_retry_transcode_in_interval(interval, cloud_retry, public_tmp, tmp_path):
    with incognito_scope(True, PRIVATE):
        assert _cloud(tmp_path)["success"]
    assert _under(cloud_retry["dir"], interval) and _leftovers(public_tmp) == []


def test_cloud_retry_fails_closed_without_interval(cloud_retry, public_tmp, tmp_path):
    with incognito_scope(True, UNBOUND):
        assert not _cloud(tmp_path)["success"]
    assert "dir" not in cloud_retry and _leftovers(public_tmp) == []


def test_cloud_retry_public_control(interval, cloud_retry, public_tmp, tmp_path):
    with incognito_scope(False, PUBLIC):
        assert _cloud(tmp_path)["success"]
    assert _under(cloud_retry["dir"], public_tmp)


# --- CAF conversion for a remote provider -----------------------------------------------

@pytest.fixture
def caf_stt(monkeypatch):
    seen = {}

    def fake_convert(path, work_dir):
        seen["dir"] = work_dir
        out = Path(work_dir) / "voice.wav"
        out.write_bytes(b"wav")
        return str(out)

    monkeypatch.setattr(transcription_tools, "_load_stt_config", lambda: {})
    monkeypatch.setattr(transcription_tools, "is_stt_enabled", lambda _cfg=None: True)
    monkeypatch.setattr(transcription_tools, "_get_provider", lambda _cfg: "mistral")
    monkeypatch.setattr(transcription_tools, "_is_local_stt_provider", lambda *_a: False)
    monkeypatch.setattr(transcription_tools, "_convert_caf_to_wav", fake_convert)
    monkeypatch.setattr(transcription_tools, "_trim_silence_for_cloud_stt", lambda *_a: None)
    monkeypatch.setattr(transcription_tools, "_dispatch_stt_provider", lambda *_a: {"success": True})
    return seen


def _caf(tmp_path):
    source = tmp_path / "voice.caf"
    source.write_bytes(b"caf")
    return transcription_tools._transcribe_prepared_audio(str(source))


def test_caf_conversion_in_interval(interval, caf_stt, public_tmp, tmp_path):
    with incognito_scope(True, PRIVATE):
        assert _caf(tmp_path)["success"]
    assert _under(caf_stt["dir"], interval) and _leftovers(public_tmp) == []


def test_caf_conversion_fails_closed_without_interval(caf_stt, public_tmp, tmp_path):
    with incognito_scope(True, UNBOUND):
        assert not _caf(tmp_path)["success"]
    assert "dir" not in caf_stt and _leftovers(public_tmp) == []


def test_caf_conversion_public_control(interval, caf_stt, public_tmp, tmp_path):
    with incognito_scope(False, PUBLIC):
        assert _caf(tmp_path)["success"]
    assert _under(caf_stt["dir"], public_tmp)


# --- SILK conversion ---------------------------------------------------------------------

@pytest.fixture
def silk(monkeypatch):
    seen = {}

    def silk_to_wav(_source, target):
        seen["dir"] = str(Path(target).parent)
        Path(target).write_bytes(b"wav")

    monkeypatch.setitem(sys.modules, "pilk", SimpleNamespace(silk_to_wav=silk_to_wav))
    monkeypatch.setattr(transcription_tools, "_HAS_PILK", True)
    return seen


def _silk(tmp_path):
    source = tmp_path / "voice.silk"
    source.write_bytes(b"silk")
    return transcription_audio._prepare_audio_for_transcription(str(source))


def test_silk_conversion_in_interval(interval, silk, public_tmp, tmp_path):
    with incognito_scope(True, PRIVATE):
        converted, cleanup, error = _silk(tmp_path)
    assert error is None and _under(converted, interval) and _leftovers(public_tmp) == []
    shutil.rmtree(cleanup)


def test_silk_conversion_fails_closed_without_interval(silk, public_tmp, tmp_path):
    with incognito_scope(True, UNBOUND):
        converted, cleanup, error = _silk(tmp_path)
    assert converted is None and cleanup is None and error and not error["success"]
    assert "dir" not in silk and _leftovers(public_tmp) == []


def test_silk_conversion_public_control(interval, silk, public_tmp, tmp_path):
    with incognito_scope(False, PUBLIC):
        converted, cleanup, error = _silk(tmp_path)
    assert error is None and _under(converted, public_tmp)
    shutil.rmtree(cleanup)


# --- TTS WAV staging and the local speaker --------------------------------------------------

@pytest.fixture
def wav_stage(monkeypatch):
    seen = {}

    def fake_ffmpeg(_ffmpeg, args, **_kwargs):
        seen["wav"] = args[1]
        Path(args[-1]).write_bytes(b"encoded")
        return SimpleNamespace(returncode=0, stderr=b"")

    monkeypatch.setattr(tts_tool_delivery.shutil, "which", lambda _name: "/usr/bin/ffmpeg")
    monkeypatch.setattr(tts_tool_delivery, "_ffmpeg_run", fake_ffmpeg)
    return seen


def test_tts_wav_staging_in_interval(interval, wav_stage, public_tmp, tmp_path):
    with incognito_scope(True, PRIVATE):
        tts_tool_delivery._write_wav_bytes_as(b"RIFF", str(tmp_path / "reply.mp3"))
    assert _under(wav_stage["wav"], interval) and _leftovers(public_tmp) == []


def test_tts_wav_staging_fails_closed_without_interval(wav_stage, public_tmp, tmp_path):
    with incognito_scope(True, UNBOUND), pytest.raises(OSError):
        tts_tool_delivery._write_wav_bytes_as(b"RIFF", str(tmp_path / "reply.mp3"))
    assert "wav" not in wav_stage and _leftovers(public_tmp) == []


def test_tts_wav_staging_public_control(interval, wav_stage, public_tmp, tmp_path):
    with incognito_scope(False, PUBLIC):
        tts_tool_delivery._write_wav_bytes_as(b"RIFF", str(tmp_path / "reply.mp3"))
    assert _under(wav_stage["wav"], public_tmp)


@pytest.fixture
def speaker(monkeypatch):
    seen = {}

    def play(path):
        seen["played"] = path

    def fake_tts(*, text, output_path):
        seen["requested"] = output_path
        return json.dumps({"success": True, "file_path": output_path})

    monkeypatch.setattr("tools.voice_mode.play_audio_file", play)
    monkeypatch.setattr(tts_tool_speaker, "_origin", lambda: SimpleNamespace(text_to_speech_tool=fake_tts))
    return seen


def _speak():
    tts_tool_speaker._play_via_tempfile([b"\x00\x00" * 8], threading.Event())
    pipeline = object.__new__(tts_tool_speaker._SyncSentencePipeline)
    pipeline._stop = threading.Event()
    return pipeline._synthesize_to_tmp("secret words")


def test_speaker_temp_files_in_interval(interval, speaker, public_tmp):
    with incognito_scope(True, PRIVATE):
        synthesized = _speak()
    assert _under(speaker["played"], interval) and _under(speaker["requested"], interval)
    assert synthesized and _leftovers(public_tmp) == []


def test_speaker_fails_closed_without_interval(speaker, public_tmp):
    with incognito_scope(True, UNBOUND):
        synthesized = _speak()
    assert synthesized is None and not speaker and _leftovers(public_tmp) == []


def test_speaker_public_control(interval, speaker, public_tmp):
    with incognito_scope(False, PUBLIC):
        _speak()
    assert _under(speaker["requested"], public_tmp)


# --- Document extraction / image download / data-URL staging ---------------------------------

def test_document_copy_in_interval(interval, public_tmp):
    from tools.read_extract import _temp_copy
    with incognito_scope(True, PRIVATE), _temp_copy(b"{}", ".json") as path:
        assert _under(path, interval)
    assert _leftovers(public_tmp) == []


def test_document_copy_fails_closed_without_interval(public_tmp):
    from tools.read_extract import ExtractionError, _temp_copy
    with incognito_scope(True, UNBOUND), pytest.raises(ExtractionError):
        with _temp_copy(b"{}", ".json"):
            pass
    assert _leftovers(public_tmp) == []


def test_document_copy_public_control(interval, public_tmp):
    from tools.read_extract import _temp_copy
    with incognito_scope(False, PUBLIC), _temp_copy(b"{}", ".json") as path:
        assert _under(path, public_tmp)


@pytest.fixture
def image_download(monkeypatch):
    seen = {}

    async def fake_download(_url, destination):
        seen["path"] = str(destination)
        Path(destination).write_bytes(b"image")
        return destination

    monkeypatch.setattr("tools.vision_tools._download_image", fake_download)
    return seen


def test_image_download_in_interval(interval, image_download, public_tmp):
    from tools.image_source import _download_to_bytes
    with incognito_scope(True, PRIVATE):
        assert asyncio.run(_download_to_bytes("https://example.test/a.png")) == b"image"
    assert _under(image_download["path"], interval) and _leftovers(public_tmp) == []


def test_image_download_fails_closed_without_interval(image_download, public_tmp):
    from tools.image_source import SourceUnsafe, _download_to_bytes
    with incognito_scope(True, UNBOUND), pytest.raises(SourceUnsafe):
        asyncio.run(_download_to_bytes("https://example.test/a.png"))
    assert "path" not in image_download and _leftovers(public_tmp) == []


def test_image_download_public_control(interval, image_download, public_tmp):
    from tools.image_source import _download_to_bytes
    with incognito_scope(False, PUBLIC):
        asyncio.run(_download_to_bytes("https://example.test/a.png"))
    assert _under(image_download["path"], public_tmp)


_DATA_URL = "data:image/png;base64," + base64.b64encode(b"\x89PNG-private").decode()


def test_vision_data_url_in_interval(interval, public_tmp):
    from agent.vision_message_prep import VisionMessagePrepMixin
    with incognito_scope(True, PRIVATE):
        source, cleanup = VisionMessagePrepMixin._materialize_data_url_for_vision(_DATA_URL)
    assert _under(source, interval) and _leftovers(public_tmp) == []
    cleanup.unlink()


def test_vision_data_url_fails_closed_without_interval(public_tmp):
    from agent.vision_message_prep import VisionMessagePrepMixin
    with incognito_scope(True, UNBOUND):
        assert VisionMessagePrepMixin._materialize_data_url_for_vision(_DATA_URL) == ("", None)
    assert _leftovers(public_tmp) == []


def test_vision_data_url_public_control(interval, public_tmp):
    from agent.vision_message_prep import VisionMessagePrepMixin
    with incognito_scope(False, PUBLIC):
        source, cleanup = VisionMessagePrepMixin._materialize_data_url_for_vision(_DATA_URL)
    assert _under(source, public_tmp)
    cleanup.unlink()


@pytest.fixture
def shrink(monkeypatch):
    seen = {}

    def resize(path, **_kwargs):
        seen["path"] = str(path)
        return "data:image/jpeg;base64,AAAA"

    return seen, resize


def _shrink(resize):
    from agent.conversation_compression import _shrink_data_url
    big = "data:image/png;base64," + "A" * (5 * 1024 * 1024)
    return _shrink_data_url(big, max_dimension=2000, resize_fn=resize)


def test_image_shrink_in_interval(interval, shrink, public_tmp):
    seen, resize = shrink
    with incognito_scope(True, PRIVATE):
        resized, unshrinkable = _shrink(resize)
    assert resized and _under(seen["path"], interval) and _leftovers(public_tmp) == []


def test_image_shrink_fails_closed_without_interval(shrink, public_tmp):
    seen, resize = shrink
    with incognito_scope(True, UNBOUND):
        resized, unshrinkable = _shrink(resize)
    assert resized is None and "path" not in seen and _leftovers(public_tmp) == []


def test_image_shrink_public_control(interval, shrink, public_tmp):
    seen, resize = shrink
    with incognito_scope(False, PUBLIC):
        assert _shrink(resize)[0]
    assert _under(seen["path"], public_tmp)


# --- Relay media download (runs in an executor thread: the scope must travel with it) -------

@pytest.fixture
def relay_download(monkeypatch):
    class _Response:
        headers = {"Content-Length": "5", "Content-Type": "image/png"}

        def __enter__(self):
            return self

        def __exit__(self, *_exc):
            return False

        def read(self, _limit):
            return b"bytes"

    monkeypatch.setattr("gateway.relay.media.urllib.request.urlopen", lambda *_a, **_k: _Response())


def _relay():
    from gateway.relay.media import RelayMediaClient
    return asyncio.run(RelayMediaClient("https://relay.test", "g", "s").download_with_name(
        "https://cdn.example/a.png", suggested_name="a.png"))


def test_relay_download_in_interval(interval, relay_download, public_tmp):
    with incognito_scope(True, PRIVATE):
        path, _name = _relay()
    assert _under(path, interval) and _leftovers(public_tmp) == []


def test_relay_download_fails_closed_without_interval(relay_download, public_tmp):
    with incognito_scope(True, UNBOUND):
        assert _relay() is None
    assert _leftovers(public_tmp) == []


def test_relay_download_public_control(interval, relay_download, public_tmp):
    with incognito_scope(False, PUBLIC):
        path, _name = _relay()
    assert _under(path, public_tmp)


# --- execute_code stdout spill ---------------------------------------------------------------

def test_execute_code_spill_in_interval(interval, public_tmp):
    from tools.code_execution_tool import _spill_full_stdout
    with incognito_scope(True, PRIVATE):
        path = _spill_full_stdout("private stdout")
    assert path and _under(path, interval)


def test_execute_code_spill_fails_closed_without_interval(public_tmp):
    from tools.code_execution_tool import _spill_full_stdout
    with incognito_scope(True, UNBOUND):
        assert _spill_full_stdout("private stdout") is None


# --- /save export staging -------------------------------------------------------------------

def _run_save(runner):
    from gateway.config import Platform
    from gateway.platforms.event import MessageEvent
    from gateway.session import SessionSource
    source = SessionSource(platform=Platform.TELEGRAM, user_id="u1", chat_id="c1", user_name="t", chat_type="dm")
    event = MessageEvent(text="/save md out.md", source=source, message_id="m1")
    return asyncio.run(runner._handle_save_command(event))


@pytest.fixture
def save_runner():
    from gateway.config import Platform
    from tests.gateway.test_save_command_delivery import _entry, _runner
    seen = {}

    async def capture(*, file_path, **_kwargs):
        seen["path"] = file_path
        seen["body"] = Path(file_path).read_text(encoding="utf-8")

    adapter = MagicMock()
    adapter.send_document = AsyncMock(side_effect=capture)
    return _runner(_entry(Platform.TELEGRAM), {Platform.TELEGRAM: adapter}), seen


def test_save_export_in_interval(interval, save_runner, public_tmp):
    runner, seen = save_runner
    with incognito_scope(True, PRIVATE):
        assert _run_save(runner) == "Export complete."
    assert _under(seen["path"], interval) and _leftovers(public_tmp) == []


def test_save_export_fails_closed_without_interval(save_runner, public_tmp):
    runner, seen = save_runner
    with incognito_scope(True, UNBOUND):
        assert _run_save(runner) != "Export complete."
    assert not seen and _leftovers(public_tmp) == []


def test_save_export_public_control(interval, save_runner, public_tmp):
    runner, seen = save_runner
    with incognito_scope(False, PUBLIC):
        assert _run_save(runner) == "Export complete."
    assert _under(seen["path"], public_tmp)


# --- Auto-TTS output path and its real call sites ---------------------------------------------

def test_auto_tts_output_path_in_interval_fails_closed_and_public(interval, public_tmp):
    from gateway.platforms.base import build_auto_tts_output_path
    with incognito_scope(True, PRIVATE):
        assert _under(build_auto_tts_output_path("discord"), interval)
    with incognito_scope(True, UNBOUND), pytest.raises(private_dirs.PrivateIntervalUnavailable):
        build_auto_tts_output_path("discord")
    with incognito_scope(False, PUBLIC):
        assert _under(build_auto_tts_output_path("discord"), public_tmp)
    assert [p for p in public_tmp.rglob("*") if p.is_file()] == []


def _telegram_voice_event():
    from tests.gateway.test_base_auto_tts_output_format import _make_voice_event
    from gateway.config import Platform
    return _make_voice_event(Platform.TELEGRAM)


def _bind_lane(key):
    register_session(key)
    with incognito_scope(True, key):
        return private_dirs.private_path_for_current_session()


def test_runner_voice_reply_uses_interval_when_scope_was_cleared(public_tmp, monkeypatch):
    """_send_voice_reply runs after the handler's scope ended; the lane must be re-entered."""
    from tests.gateway.test_auto_voice_reply_format import _make_adapter, _make_runner
    from gateway.config import Platform
    runner = _make_runner()
    runner.adapters[Platform.TELEGRAM] = _make_adapter(Platform.TELEGRAM)
    event = _telegram_voice_event()
    key = runner._session_key_for_source(event.source)
    interval_root = _bind_lane(key)
    requested = []

    def fake_tts(*, text, output_path):
        requested.append(output_path)
        Path(output_path).write_bytes(b"audio")
        return json.dumps({"success": True, "file_path": output_path})

    monkeypatch.setattr("tools.tts_tool.text_to_speech_tool", fake_tts)
    try:
        assert not is_incognito()  # the scope is gone, exactly as after _clear_session_env
        asyncio.run(runner._send_voice_reply(event, "a private spoken reply"))
    finally:
        register_session(key, False)
        private_dirs.discard_private_dir(key)
    assert requested and _under(requested[0], interval_root)
    assert [p for p in public_tmp.rglob("*") if p.is_file()] == []


def test_runner_voice_reply_public_lane_is_unchanged(public_tmp, monkeypatch):
    from tests.gateway.test_auto_voice_reply_format import _make_adapter, _make_runner
    from gateway.config import Platform
    runner = _make_runner()
    runner.adapters[Platform.TELEGRAM] = _make_adapter(Platform.TELEGRAM)
    requested = []

    def fake_tts(*, text, output_path):
        requested.append(output_path)
        Path(output_path).write_bytes(b"audio")
        return json.dumps({"success": True, "file_path": output_path})

    monkeypatch.setattr("tools.tts_tool.text_to_speech_tool", fake_tts)
    asyncio.run(runner._send_voice_reply(_telegram_voice_event(), "a public reply"))
    assert requested and _under(requested[0], public_tmp)


@pytest.mark.asyncio
async def test_base_adapter_auto_tts_uses_interval_when_scope_was_cleared(public_tmp, monkeypatch):
    """The post-handler auto-TTS block of the base adapter, through the real background path."""
    from tests.gateway.test_base_auto_tts_output_format import _DummyAdapter, _hold_typing
    from gateway.config import Platform
    from gateway.platforms.base import SendResult
    from gateway.session import build_session_key
    adapter = _DummyAdapter(Platform.TELEGRAM)
    adapter._keep_typing = _hold_typing()
    adapter._should_auto_tts_for_chat = lambda _chat_id: True
    adapter.play_tts = AsyncMock(return_value=SendResult(success=True, message_id="tts-1"))
    adapter.set_message_handler(lambda _event: asyncio.sleep(0, result="private reply"))
    event = _telegram_voice_event()
    key = build_session_key(event.source)
    interval_root = _bind_lane(key)
    requested = []

    def fake_tts(*, text, output_path=None):
        requested.append(output_path)
        Path(output_path).write_bytes(b"audio")
        return json.dumps({"success": True, "file_path": output_path})

    monkeypatch.setattr("tools.tts_tool.check_tts_requirements", lambda: True)
    monkeypatch.setattr("tools.tts_tool.text_to_speech_tool", fake_tts)
    try:
        assert not is_incognito()
        await adapter._process_message_background(event, key)
    finally:
        register_session(key, False)
        private_dirs.discard_private_dir(key)
    assert requested and _under(requested[0], interval_root)
    assert [p for p in public_tmp.rglob("*") if p.is_file()] == []


def test_private_lane_scope_only_enters_registered_lanes():
    from gateway.incognito import private_lane_scope
    register_session(PRIVATE)
    try:
        with private_lane_scope(PRIVATE):
            assert is_incognito()
        with private_lane_scope("p1-sink-never-registered"), private_lane_scope(None):
            assert not is_incognito()
    finally:
        register_session(PRIVATE, False)
        private_dirs.discard_private_dir(PRIVATE)


# --- Computer-use screenshot spool ---------------------------------------------------------------

@pytest.fixture
def cua_cli(monkeypatch):
    import tools.computer_use.cua_backend_session as cua
    seen = {}

    def fake_run(cmd, _env, _name, _timeout):
        arguments = json.loads(cmd[3])
        seen["shot"] = arguments.get("screenshot_out_file")
        return {"isError": False}

    monkeypatch.setattr(cua._driver, "resolve_cua_driver_cmd", lambda: "cua-driver")
    monkeypatch.setattr(cua, "_cli_run_json", fake_run)
    monkeypatch.setattr(cua, "_cli_result", lambda parsed, shot: {"ok": True})
    monkeypatch.setattr("tools.computer_use.cua_backend.cua_driver_child_env", lambda: {})
    return cua, seen


def _cua(cua):
    session = object.__new__(cua._CuaDriverSession)
    return session._call_tool_via_cli("get_window_state", {}, 5.0)


def test_cua_screenshot_spool_in_interval(interval, cua_cli, public_tmp):
    cua, seen = cua_cli
    with incognito_scope(True, PRIVATE):
        _cua(cua)
    assert _under(seen["shot"], interval) and _leftovers(public_tmp) == []


def test_cua_screenshot_spool_fails_closed_without_interval(cua_cli, public_tmp):
    cua, seen = cua_cli
    with incognito_scope(True, UNBOUND):
        _cua(cua)
    assert seen["shot"] is None and _leftovers(public_tmp) == []


def test_cua_screenshot_spool_public_control(interval, cua_cli, public_tmp):
    cua, seen = cua_cli
    with incognito_scope(False, PUBLIC):
        _cua(cua)
    assert _under(seen["shot"], public_tmp)


# --- Inbound media caches -------------------------------------------------------------------------

def test_inbound_image_and_document_caches_route_to_private_interval(interval, public_tmp, tmp_path, monkeypatch):
    """Telegram's photo/PDF path uses these shared cache helpers, not tempfile directly."""
    import gateway.platforms.base as platform_base
    public_images = tmp_path / "public-images"
    public_audio = tmp_path / "public-audio"
    public_documents = tmp_path / "public-documents"
    monkeypatch.setattr(platform_base, "IMAGE_CACHE_DIR", public_images)
    monkeypatch.setattr(platform_base, "AUDIO_CACHE_DIR", public_audio)
    monkeypatch.setattr(platform_base, "DOCUMENT_CACHE_DIR", public_documents)
    monkeypatch.setattr(platform_base, "_looks_like_image", lambda _data: True)

    with incognito_scope(False, PUBLIC):
        public_photo = Path(platform_base.cache_image_from_bytes(b"public-photo", ".jpg"))
        public_audio = Path(platform_base.cache_audio_from_bytes(b"public-audio", ".ogg"))
        public_pdf = Path(platform_base.cache_document_from_bytes(b"public-pdf", "private.pdf"))
    assert public_photo.is_relative_to(public_images)
    assert public_audio.is_relative_to(tmp_path / "public-audio")
    assert public_pdf.is_relative_to(public_documents)

    with incognito_scope(True, PRIVATE):
        private_photo = Path(asyncio.run(platform_base.cache_image_from_bytes_async(b"private-photo", ".jpg")))
        private_audio = Path(asyncio.run(platform_base.cache_audio_from_bytes_async(b"private-audio", ".ogg")))
        private_pdf = Path(asyncio.run(platform_base.cache_document_from_bytes_async(b"private-pdf", "private.pdf")))
    assert private_photo.is_relative_to(interval / "cache" / "images")
    assert private_audio.is_relative_to(interval / "cache" / "audio")
    assert private_pdf.is_relative_to(interval / "cache" / "documents")
    assert not private_photo.is_relative_to(public_images)
    assert not private_audio.is_relative_to(public_audio)
    assert not private_pdf.is_relative_to(public_documents)

    from agent.incognito_private_dir import PrivateIntervalUnavailable
    with incognito_scope(True, UNBOUND):
        with pytest.raises(PrivateIntervalUnavailable):
            platform_base.cache_document_from_bytes(b"unbound", "unbound.pdf")
