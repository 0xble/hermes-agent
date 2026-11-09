"""Historical installs use bounded transport and retain only verified archive bytes."""
from contextlib import contextmanager
import hashlib
import json
import io
import tarfile
import shutil
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Event, Thread
import time

import pytest

from tests.e2e.core.upgrade._install_cache import InstallCache
from tests.e2e.core.upgrade import _helpers as H

pytestmark = pytest.mark.platforms("posix")


@contextmanager
def artifact_server():
    release = Event()
    requests = []
    payload = b"pinned historical release\n"

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            requests.append(self.path)
            if self.path == "/stall":
                release.wait(10)
                return
            self.send_response(504 if self.path == "/504" else 200)
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, *_args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}", payload, requests
    finally:
        release.set()
        server.shutdown()
        server.server_close()
        thread.join(5)


def test_verified_archive_survives_a_later_504_without_another_request(tmp_path, monkeypatch):
    uv_cache = tmp_path / "runner" / "uv"
    uv_cache.mkdir(parents=True)
    cache = InstallCache(tmp_path / "downloads", {"UV_CACHE_DIR": str(uv_cache)})
    with artifact_server() as (url, payload, requests):
        digest = hashlib.sha256(payload).hexdigest()
        first = cache.artifact(url + "/release", digest)
        assert first.read_bytes() == payload
        assert cache.artifact(url + "/504", digest) == first
        # uv owns and may prune its entire cache. PM archives must survive that lifecycle.
        shutil.rmtree(uv_cache)
        assert cache.artifact(url + "/504", digest) == first
        from pm.store import Store, current_target
        import pm.artifact_mirror

        source = tmp_path / "source"
        (source / "pm").mkdir(parents=True)
        pin = {"url": url + "/504", "sha256": digest}
        (source / "pm" / "lock.json").write_text(json.dumps({"packages": {
            "ffmpeg": {"version": "fixture", "artifacts": {current_target(): pin}},
        }}))
        monkeypatch.setattr(H, "WORKTREE", source)
        monkeypatch.setattr(pm.artifact_mirror, "mirror_url", lambda _digest: url + "/504")
        environments = [cache.environment(H.isolated_env(tmp_path / name)) for name in ("a", "b")]
        assert environments[0]["HERMES_RUNTIME_DIR"] != environments[1]["HERMES_RUNTIME_DIR"]
        for env in environments:
            cache.prepare(env)
            store = Store(Path(env["HERMES_RUNTIME_DIR"]))
            with store.scratch() as scratch:
                downloaded, = store.fetch_many([pin], scratch)
                assert downloaded.read_bytes() == payload
        assert requests == ["/release"]
        first.write_bytes(b"corrupt cache")
        with pytest.raises(AssertionError, match="sha256"):
            cache.artifact(url + "/504", digest)
        assert requests == ["/release"]


@pytest.mark.parametrize("path", ["504", "stall", "wrong-digest"])
def test_cold_download_failure_is_bounded_and_names_the_artifact(tmp_path, path):
    cache = InstallCache(tmp_path / "downloads")
    with artifact_server() as (url, _payload, requests):
        started = time.monotonic()
        with pytest.raises(AssertionError, match="504|timed out|sha256") as error:
            cache.artifact(url + "/" + path, "a" * 64, timeout=1)
        assert url in str(error.value)
        assert time.monotonic() - started < 5
        assert requests == ["/" + path]
        assert not list((cache.root / "artifacts").glob("[a-f0-9]" * 64))


def test_archive_seeding_does_not_publish_a_shell_bootstrap_tool_until_requested(tmp_path, monkeypatch):
    from pm.store import current_target
    import pm.artifact_mirror

    source = tmp_path / "source"
    (source / "pm").mkdir(parents=True)
    archive = tmp_path / "uv.tar.gz"
    with tarfile.open(archive, "w:gz") as stream:
        member = tarfile.TarInfo("uv-fixture/uv")
        payload = b"fixture binary"
        member.size = len(payload)
        member.mode = 0o755
        stream.addfile(member, io.BytesIO(payload))
    digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    pin = {"version": "fixture", "artifacts": {current_target(): {
        "url": archive.as_uri(), "sha256": digest}}}
    (source / "pm" / "lock.json").write_text(json.dumps({"packages": {"uv": pin}}))
    monkeypatch.setattr(H, "WORKTREE", source)
    monkeypatch.setattr(pm.artifact_mirror, "mirror_url", lambda _digest: archive.as_uri())
    cache = InstallCache(tmp_path / "downloads")
    env = cache.environment(H.isolated_env(tmp_path / "install"))
    slot = Path(env["HERMES_RUNTIME_DIR"]) / f"uv-fixture-{current_target()}"
    cache.prepare(env)
    assert not slot.exists(), "legacy handoff must let PM install its pinned toolchain"
    assert len(list((slot.parent / f"fetch-{digest}").iterdir())) == 1
    cache.prepare(env, bootstrap_uv=True)
    assert (slot / "uv").read_bytes() == payload
