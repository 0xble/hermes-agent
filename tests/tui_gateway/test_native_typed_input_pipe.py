"""Native Ink input uses a private helper pipe, not a JSON transcript or client metadata."""
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
from types import SimpleNamespace

from agent.source_origin_ingress import acquire_input, admission_input


def test_native_helper_receipt_lives_only_until_the_input_pipe_closes(tmp_path):
    root = Path(__file__).resolve().parents[2]
    text = "Keep notices off."
    params = {"submission_id": "fictional-input", "text": text}
    request = SimpleNamespace(ref=SimpleNamespace(session_id="fixture"), request_id="fictional-input")
    connection = SimpleNamespace(native_owner=True, authority=SimpleNamespace(
        db=SimpleNamespace(db_path=tmp_path / "fictional.db")))
    env = {**os.environ, "HERMES_HOME": str(tmp_path), "HERMES_PYTHON_SRC_ROOT": str(root)}
    child = subprocess.Popen([sys.executable, str(root / "ui-tui/scripts/gateway_bootstrap.py"), "--typed-input"],
                             stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env)
    ready, output = threading.Event(), []
    def read_ready():
        output.append(child.stdout.readline().strip())
        ready.set()
    reader = threading.Thread(target=read_ready, daemon=True)
    reader.start()
    try:
        child.stdin.write(json.dumps({"session": "fixture", "request": "fictional-input", "text": text}) + "\n")
        child.stdin.flush()
        assert ready.wait(timeout=20), "native input helper did not become ready"
        assert output == ["ready"]
        with acquire_input(connection, request.ref, params):
            record = admission_input(request)
        assert record["source_origin_v1"]["record"]["platform"] == "tui"
        assert record["source_origin_v1"]["record"]["spans"] == ((0, len(text)),)
        child.stdin.close()
        assert child.wait(timeout=15) == 0
        with acquire_input(connection, request.ref, params):
            assert admission_input(request) == {}
    finally:
        if child.poll() is None:
            child.kill()
        child.wait(timeout=15)
        reader.join(timeout=5)
