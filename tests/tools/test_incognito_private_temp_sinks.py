"""Temporary artifacts made during a private turn stay inside its interval.

Each sink gets three cases through the real resolver (registry + ``incognito_scope``):
a registered private session writes under its interval, an incognito turn with no bound
interval fails closed, and a public turn keeps its ordinary behavior.
"""

from pathlib import Path
from types import SimpleNamespace
import os
import shutil

import pytest

import gateway.platforms.base as platform_base
import plugins.platforms.telegram.telegram_media_flood as media_flood
import tools.environments.file_sync as file_sync
import tools.transcription_audio as transcription_audio
import tools.transcription_chunking as transcription_chunking
from agent import incognito_private_dir as private_dirs
from agent.incognito import incognito_scope, register_session
from hermes_constants import get_hermes_dir

PRIVATE = "p1-sink-private"
UNBOUND = "p1-sink-unbound"  # incognito scope, never registered: no interval exists
PUBLIC = "p1-sink-public"


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


def _under(path, root: Path) -> bool:
    return Path(path).resolve().is_relative_to(root.resolve())


def _write_ffmpeg_output(command, **_kwargs):
    Path(command[-1]).write_bytes(b"encoded")
    return SimpleNamespace(returncode=0, stderr=b"")


# --- Telegram deferred-upload copy -------------------------------------------------

def test_telegram_deferred_copy_stays_in_interval_and_cleanup_is_scoped(interval, tmp_path):
    source = tmp_path / "voice.ogg"
    source.write_bytes(b"voice")

    with incognito_scope(True, PRIVATE):
        first = media_flood._keep_private_copy(str(source))
        second = media_flood._keep_private_copy(str(source))
    assert first and second
    assert _under(first, interval) and _under(second, interval)
    assert Path(first).parent != interval and Path(first).parent != Path(second).parent

    shutil.rmtree(os.path.dirname(first))
    assert interval.is_dir() and Path(second).read_bytes() == b"voice"


def test_telegram_deferred_copy_fails_closed_without_interval(tmp_path):
    source = tmp_path / "voice.ogg"
    source.write_bytes(b"voice")
    with incognito_scope(True, UNBOUND):
        assert media_flood._keep_private_copy(str(source)) is None


def test_telegram_deferred_copy_public_control(interval, tmp_path):
    source = tmp_path / "voice.ogg"
    source.write_bytes(b"voice")
    with incognito_scope(False, PUBLIC):
        kept = media_flood._keep_private_copy(str(source))
    assert kept and not _under(kept, interval)
    shutil.rmtree(os.path.dirname(kept))


# --- Ogg/Opus transcode default output -----------------------------------------------

@pytest.fixture
def fake_ffmpeg(monkeypatch):
    monkeypatch.setattr(shutil, "which", lambda _name: "/usr/bin/ffmpeg")
    monkeypatch.setattr(platform_base.subprocess, "run", _write_ffmpeg_output)


def test_transcode_default_output_in_interval(interval, fake_ffmpeg, tmp_path):
    source = tmp_path / "voice.wav"
    source.write_bytes(b"wav")
    with incognito_scope(True, PRIVATE):
        output = platform_base.transcode_to_ogg_opus(str(source))
    assert output and _under(output, interval)


def test_transcode_default_output_fails_closed_without_interval(fake_ffmpeg, tmp_path):
    source = tmp_path / "voice.wav"
    source.write_bytes(b"wav")
    with incognito_scope(True, UNBOUND):
        assert platform_base.transcode_to_ogg_opus(str(source)) is None


def test_transcode_default_output_public_control(interval, fake_ffmpeg, tmp_path):
    source = tmp_path / "voice.wav"
    source.write_bytes(b"wav")
    with incognito_scope(False, PUBLIC):
        output = platform_base.transcode_to_ogg_opus(str(source))
    assert output and not _under(output, interval)
    os.unlink(output)


def test_transcode_explicit_output_path_is_unchanged(interval, fake_ffmpeg, tmp_path):
    """tts_tool_delivery passes output_path; the resolver must not redirect it."""
    source = tmp_path / "voice.mp3"
    source.write_bytes(b"mp3")
    target = interval / "hermes_voice" / "reply.ogg"
    target.parent.mkdir(parents=True)
    with incognito_scope(True, PRIVATE):
        assert platform_base.transcode_to_ogg_opus(str(source), output_path=str(target)) == str(target)


# --- Cloud STT silence trim ----------------------------------------------------------

@pytest.fixture
def fake_trim(monkeypatch):
    monkeypatch.setattr(transcription_audio, "_find_ffmpeg_binary", lambda: "ffmpeg")
    monkeypatch.setattr(transcription_audio, "_probe_audio_duration",
                        lambda path: 20.0 if path.endswith("-trimmed.m4a") else 60.0)
    monkeypatch.setattr(transcription_audio, "_run_ffmpeg_stt_encode",
                        lambda _ffmpeg, _input, output, **_kwargs: Path(output).write_bytes(b"encoded"))


def test_cloud_stt_trim_in_interval_and_cleanup_is_scoped(interval, fake_trim, tmp_path):
    source = tmp_path / "voice.wav"
    source.write_bytes(b"wav")
    with incognito_scope(True, PRIVATE):
        trimmed = transcription_audio._trim_silence_for_cloud_stt(str(source), {})
    assert trimmed and _under(trimmed, interval)
    assert Path(trimmed).parent != interval
    shutil.rmtree(os.path.dirname(trimmed))
    assert interval.is_dir()


def test_cloud_stt_trim_fails_closed_without_interval(fake_trim, tmp_path):
    source = tmp_path / "voice.wav"
    source.write_bytes(b"wav")
    with incognito_scope(True, UNBOUND):
        assert transcription_audio._trim_silence_for_cloud_stt(str(source), {}) is None


def test_cloud_stt_trim_public_control(interval, fake_trim, tmp_path):
    source = tmp_path / "voice.wav"
    source.write_bytes(b"wav")
    with incognito_scope(False, PUBLIC):
        trimmed = transcription_audio._trim_silence_for_cloud_stt(str(source), {})
    assert trimmed and not _under(trimmed, interval)
    shutil.rmtree(os.path.dirname(trimmed))


# --- Oversized cloud STT split -------------------------------------------------------

@pytest.fixture
def fake_split(monkeypatch):
    monkeypatch.setattr(transcription_chunking, "_find_ffmpeg_binary", lambda: "ffmpeg")
    monkeypatch.setattr(transcription_chunking, "_probe_audio_duration", lambda _path: 20.0)
    monkeypatch.setattr(transcription_chunking, "_silence_midpoints", lambda _ffmpeg, _path: [])

    def transcode(_path, work_dir):
        compact = Path(work_dir) / "compact.m4a"
        compact.write_bytes(b"compact")
        return str(compact), None

    def split(_ffmpeg, _path, work_dir, _cuts):
        part = Path(work_dir) / "part000.m4a"
        part.write_bytes(b"part")
        return [str(part)]

    monkeypatch.setattr(transcription_chunking, "_transcode_audio_for_stt", transcode)
    monkeypatch.setattr(transcription_chunking, "_split", split)


def _run_oversized(tmp_path):
    source = tmp_path / "voice.wav"
    source.write_bytes(b"source")
    uploads: list[str] = []

    def dispatch(path):
        uploads.append(path)
        return {"success": True, "transcript": "hello from the segment"}

    return transcription_chunking.transcribe_oversized(str(source), (1, None), dispatch), uploads


def test_oversized_stt_work_dir_in_interval(interval, fake_split, tmp_path):
    with incognito_scope(True, PRIVATE):
        result, uploads = _run_oversized(tmp_path)
    assert result["success"] is True
    assert uploads and all(_under(path, interval) for path in uploads)
    assert not any(Path(path).exists() for path in uploads)
    assert interval.is_dir()


def test_oversized_stt_fails_closed_without_interval(fake_split, tmp_path):
    with incognito_scope(True, UNBOUND):
        result, uploads = _run_oversized(tmp_path)
    assert result["success"] is False
    assert uploads == []


def test_oversized_stt_public_control(interval, fake_split, tmp_path):
    with incognito_scope(False, PUBLIC):
        result, uploads = _run_oversized(tmp_path)
    assert result["success"] is True
    assert uploads and not any(_under(path, interval) for path in uploads)


# --- Remote sandbox file sync --------------------------------------------------------

def test_private_turn_sync_omits_interval_cache_files(interval):
    with incognito_scope(True, PRIVATE):
        private_cache = get_hermes_dir("cache/documents", "document_cache")
        assert _under(private_cache, interval)
        private_cache.mkdir(parents=True, exist_ok=True)
        (private_cache / "secret.pdf").write_bytes(b"private")
        hosts = [host for host, _remote in file_sync.iter_sync_files()]
    assert not any(_under(host, interval) for host in hosts)


def test_sync_omits_interval_paths_from_any_producer(interval, monkeypatch, tmp_path):
    """Defense in depth: an interval path handed in by any enumerator is dropped."""
    from tools import credential_files

    private_file = interval / "cache" / "secret.bin"
    public_file = tmp_path / "public" / "ordinary.txt"
    lookalike = tmp_path / "public" / f"{private_dirs.PRIVATE_DIR_PREFIX}not-private.txt"
    for path in (private_file, public_file, lookalike):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(path.name, encoding="utf-8")
    monkeypatch.setattr(credential_files, "get_credential_file_mounts", lambda: [])
    monkeypatch.setattr(credential_files, "iter_skills_files", lambda **_kwargs: [
        {"host_path": str(public_file), "container_path": "/root/.hermes/ordinary.txt"},
    ])
    monkeypatch.setattr(credential_files, "iter_cache_files", lambda **_kwargs: [
        {"host_path": str(private_file), "container_path": "/root/.hermes/cache/secret.bin"},
        {"host_path": str(lookalike), "container_path": "/root/.hermes/cache/lookalike.txt"},
    ])

    hosts = {Path(host) for host, _remote in file_sync.iter_sync_files()}
    assert hosts == {public_file, lookalike}


def test_private_sync_fails_closed_when_private_root_cannot_resolve(interval, monkeypatch, tmp_path):
    from tools import credential_files

    public_file = tmp_path / "public.txt"
    public_file.write_text("public", encoding="utf-8")
    monkeypatch.setattr(credential_files, "get_credential_file_mounts", lambda: [])
    monkeypatch.setattr(credential_files, "iter_skills_files", lambda **_kwargs: [])
    monkeypatch.setattr(credential_files, "iter_cache_files", lambda **_kwargs: [
        {"host_path": str(public_file), "container_path": "/root/.hermes/cache/public.txt"},
    ])
    monkeypatch.setattr(private_dirs, "_configured_root", lambda: (_ for _ in ()).throw(OSError("unresolved")))

    with incognito_scope(True, PRIVATE):
        assert file_sync.iter_sync_files() == []


def test_public_sync_keeps_ordinary_cache_files(interval):
    with incognito_scope(False, PUBLIC):
        public_cache = get_hermes_dir("cache/documents", "document_cache")
        assert not _under(public_cache, interval)
        public_cache.mkdir(parents=True, exist_ok=True)
        public_file = public_cache / "public.pdf"
        public_file.write_bytes(b"public")
        files = file_sync.iter_sync_files()
    assert (str(public_file), "/root/.hermes/cache/documents/public.pdf") in files
