"""Durable generation records tolerate concurrent status and heartbeat writes."""
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import threading

from gateway.generation import GenerationIdentity, write_generation_record


def test_concurrent_generation_record_writes_use_distinct_temporary_paths(tmp_path, monkeypatch):
    identity = GenerationIdentity.create(release_sha="a" * 40, label="test", boot_id="test")
    path = tmp_path / "gateway_state.test.json"
    barrier = threading.Barrier(2, timeout=5)
    original = Path.write_text

    def write_then_wait(self, *args, **kwargs):
        result = original(self, *args, **kwargs)
        if self.name.startswith(".gateway_state.test.json.") and self.name.endswith(".tmp"):
            barrier.wait()
        return result

    monkeypatch.setattr(Path, "write_text", write_then_wait)
    with ThreadPoolExecutor(max_workers=2) as pool:
        jobs = [pool.submit(write_generation_record, path, identity, state=state)
                for state in ("ready", "serving")]
        for job in jobs:
            job.result(timeout=8)
    assert json.loads(path.read_text())["id"] == identity.id
