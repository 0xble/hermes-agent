"""Direct card typing must not echo into later browser results or process arguments.

All values are public test/synthetic fixtures. No browser or account is contacted.
"""
import hashlib
import json
import subprocess
import sys

import pytest

from agent.redact import clear_vault_date_components, clear_vault_redaction_values
from model_tools import handle_function_call
from tools import browser_camofox as cf, browser_tool as bt, browser_tool_session as session

PAN = "4242424242424242"
CVC = "731"
ORIGIN = "https://checkout.example"
TREE = '- textbox "Card number" [e1]\n- textbox "Security code (CVC)" [e731]\n- textbox "Search" [e3]'


@pytest.mark.parametrize("backend", ["camofox", "agent-browser"])
@pytest.mark.parametrize("failure", [False, True])
@pytest.mark.parametrize("card", [("@e1", PAN), ("@e731", CVC)])
@pytest.mark.parametrize("surface", ["type", "snapshot"])
def test_direct_card_typing_protects_results_snapshots_and_console(monkeypatch, tmp_path, backend, failure, card, surface):
    from tools import browser_vault_tool
    clear_vault_redaction_values()
    monkeypatch.setattr(bt, "_is_camofox_mode", lambda: backend == "camofox")
    monkeypatch.setattr(bt, "_blocked_private_page_action", lambda *a: None)
    monkeypatch.setattr(bt, "_blocked_private_page_content", lambda *a: None)
    monkeypatch.setattr(browser_vault_tool, "_current_page_origin", lambda task: ORIGIN)
    typed = {}
    page = {"task_id": "typed-card", "tab_id": "fixture", "user_id": "fixture"}
    monkeypatch.setattr(cf, "_require_tab", lambda *a: (page, None))
    monkeypatch.setattr(bt, "get_browser_snapshot_threshold", lambda: 120)
    from tools import browser_tool_snapshot
    from pathlib import Path
    stored = []
    real_store = browser_tool_snapshot._store_full_snapshot
    def store(tree):
        path = real_store(tree)
        stored.append(path)
        return path
    monkeypatch.setattr(browser_tool_snapshot, "_store_full_snapshot", store)

    def tree():
        return TREE + ''.join(f'\n- textbox "Echo" [e9]: {v}' for v in typed.values()) + '\n' + ('- paragraph "padding"\n' * 40)

    def post(path, body, **kw):
        typed[body["ref"]] = body["text"]
        if failure:
            raise RuntimeError(f'page rejected {body["text"]}')
        return {"ok": True}

    monkeypatch.setattr(cf, "_post", post)
    monkeypatch.setattr(cf, "_snapshot_data", lambda *a: {"snapshot": tree(), "refsCount": 3})

    def run(task, command, args=None, **kw):
        if command == "snapshot":
            return {"success": True, "data": {"snapshot": tree(), "refs": {"e1": {}, "e731": {}, "e3": {}}}}
        if command == "fill":
            typed[args[0].lstrip("@")] = args[1]
            return {"success": not failure, "error": f'page rejected {args[1]}', "fallback_warning": f'echo {args[1]}'}
        if command == "eval":
            return {"success": True, "data": {"result": {"value": list(typed.values())}}}
        raise AssertionError(command)

    monkeypatch.setattr(session, "_run_browser_command", run)
    def call(name, args):
        return handle_function_call(name, args, task_id="typed-card", skip_pre_tool_call_hook=True,
                                    skip_tool_request_middleware=True)

    try:
        ref, value = card
        output = call("browser_type", {"ref": ref, "text": value})
        assert json.loads(output)["success"] is not failure
        if surface == "type":
            assert value not in output.replace("e731", "ref")
        output = call("browser_snapshot", {"full": False})
        assert "truncated" in json.loads(output)["snapshot"]
        assert stored and Path(stored[0]).is_file()
        for path in stored:
            saved = Path(path).read_text()
            assert PAN not in saved and f': {CVC}' not in saved
            assert "[e731]" in saved
        monkeypatch.setattr(bt, "get_browser_snapshot_threshold", lambda: 10000)
        output = call("browser_snapshot", {"full": True})
        snapshot = json.loads(output)["snapshot"]
        assert PAN not in snapshot and f': {CVC}' not in snapshot
        assert "[e731]" in snapshot
        from agent.browser_output_egress import scrub_browser_result
        monkeypatch.setattr(bt, "_browser_eval", lambda *a: json.dumps({"success": True, "result": f'echo {value}'}))
        console = call("browser_console", {"expression": "document.title"})
        assert value not in console
        monkeypatch.setattr(bt, "_browser_eval", lambda *a: json.dumps({
            "success": True, "result": {"nested": [{"value": int(value)}, {"value": float(value)}],
                                        "ref": "e731", "ordinal": 7310,
                                        "fractional": 731.5, "enabled": True}}))
        numeric = json.loads(call("browser_console", {"expression": "window.cardState"}))["result"]
        assert all(leaf["value"] == "«redacted-vault-secret»" for leaf in numeric["nested"])
        assert numeric["ref"] == "e731" and numeric["ordinal"] == 7310
        assert numeric["fractional"] == 731.5 and numeric["enabled"] is True
        for path in tmp_path.rglob("*"):
            if path.is_file():
                assert PAN not in path.read_text(errors="ignore") and f': {CVC}' not in path.read_text(errors="ignore")
        # Same digits in an unrelated task/origin are not a global numeric filter.
        ordinary = json.dumps({"snapshot": f'- textbox "Search" [e731]: {CVC}'})
        assert scrub_browser_result("browser_snapshot", ordinary, "other") == ordinary
        monkeypatch.setattr(browser_vault_tool, "_current_page_origin", lambda task: "https://other.example")
        assert scrub_browser_result("browser_snapshot", ordinary, "typed-card") == ordinary
        monkeypatch.setattr(browser_vault_tool, "_current_page_origin", lambda task: ORIGIN)
        clear_vault_date_components("typed-card")
        assert scrub_browser_result("browser_snapshot", ordinary, "typed-card") == ordinary
        output = call("browser_type", {"ref": "@e3", "text": "normal search words"})
        if not failure:
            assert json.loads(output)["typed"] == "normal search words"
    finally:
        clear_vault_redaction_values()


@pytest.mark.parametrize("executable", ["agent-browser", "agent-browser.cmd"])
@pytest.mark.parametrize("text", [PAN, CVC, 'literal %PATH% & \"quote\"\nnext line café'])
def test_fill_transport_keeps_literal_input_only_in_memory(monkeypatch, tmp_path, executable, text):
    fixture = tmp_path / "driver.py"
    fixture.write_text('import hashlib,json,sys\ncommands=json.load(sys.stdin)\n'
                       'assert commands[0][0] == "fill"\n'
                       'digest=hashlib.sha256(commands[0][2].encode()).hexdigest()\n'
                       'print(json.dumps([{ "success": True, "result": {"digest": digest}}]))\n')
    sockets = tmp_path / "sockets"
    sockets.mkdir()
    real_popen = subprocess.Popen
    observed = []

    def popen(argv, **kw):
        observed.append((argv, kw["stdin"]))
        assert text not in json.dumps(argv)
        assert argv[-1] == "batch"
        assert kw["stdin"] == subprocess.PIPE
        return real_popen([sys.executable, str(fixture)], **kw)

    monkeypatch.setattr(session.subprocess, "Popen", popen)
    monkeypatch.setattr(session, "_prepare_session_socket_dir", lambda *a: str(sockets))
    monkeypatch.setattr(session, "_agent_browser_command_env", lambda *a: {})
    monkeypatch.setattr(session, "_ensure_screen_for_headed_chromium", lambda: None)
    monkeypatch.setattr(session, "_sandbox_wrap", lambda argv, env, path: (argv, env))
    monkeypatch.setattr(session, "_bot_desktop_attach_port", lambda *a: None)
    monkeypatch.setattr(session._cloud, "_is_headed_mode", lambda: False)
    monkeypatch.setattr(session._cloud, "_get_browser_engine", lambda: "auto")
    info = {"session_name": "fixture", "features": {}}
    _, result = session._dispatch_browser_command("fixture", info, executable, "fill", ["@e1", text], 5, None)
    assert result["success"] is True
    assert result["data"]["digest"] == hashlib.sha256(text.encode()).hexdigest()
    assert observed
    assert not list(sockets.glob("_stdin*"))
    assert all(text not in p.read_text(errors="ignore") for p in sockets.iterdir() if p.is_file())

@pytest.mark.platforms("posix")
@pytest.mark.parametrize("timeout", [False, True])
@pytest.mark.parametrize("output_kind", ["json", "stdout-error", "stderr-error"])
def test_output_echoes_never_touch_disk_with_daemon_held_handles(tmp_path, monkeypatch, caplog, timeout, output_kind):
    import os
    import time
    from pathlib import Path

    fixture = tmp_path / "echo-driver.py"
    release, ack = tmp_path / "release", tmp_path / "ack"
    # Synthetic values are assembled in the fixture so its own source is not an echo file.
    fixture.write_text("""import json,os,sys,time
from pathlib import Path
value='42'*8+' '+'7'+'31'
release,ack=map(Path,sys.argv[1:3])
if sys.argv[4]=='json':
    print(json.dumps({'success':True,'data':{'snapshot':value}}),flush=True)
elif sys.argv[4]=='stdout-error':
    print('malformed echo '+value,flush=True)
print(value,file=sys.stderr,flush=True)
if os.fork()==0:
    limit=time.monotonic()+8
    while not release.exists() and time.monotonic()<limit:
        time.sleep(.01)
    try:
        print('daemon alive '+value,flush=True)
        print(value,file=sys.stderr,flush=True)
        ack.write_text('survived')
    finally:
        os._exit(0)
if sys.argv[3]=='timeout':
    time.sleep(8)
raise SystemExit(1 if sys.argv[4]=='stderr-error' else 0)
""")
    real_popen, real_open = subprocess.Popen, os.open
    output_files, processes = [], []

    def opened(path, flags, *args, **kw):
        if Path(path).name.startswith(("_stdout_", "_stderr_", "_stdin_")):
            output_files.append(str(path))
        return real_open(path, flags, *args, **kw)

    def popen(argv, **kw):
        proc = real_popen([sys.executable, str(fixture), str(release), str(ack),
                           "timeout" if timeout else "success", output_kind], **kw)
        processes.append(proc)
        return proc

    monkeypatch.setattr(session.os, "open", opened)
    monkeypatch.setattr(session.subprocess, "Popen", popen)
    monkeypatch.setattr(session, "_prepare_session_socket_dir", lambda *a: str(tmp_path))
    monkeypatch.setattr(session, "_agent_browser_command_env", lambda *a: {})
    monkeypatch.setattr(session, "_sandbox_wrap", lambda argv, env, path: (argv, env))
    monkeypatch.setattr(session, "_ensure_screen_for_headed_chromium", lambda: None)
    monkeypatch.setattr(session, "_handle_browser_command_timeout", lambda *a: None)
    monkeypatch.setattr(session._cloud, "_is_headed_mode", lambda: False)
    started = time.monotonic()
    try:
        result = session._spawn_and_collect("fixture", {"session_name": "fixture"},
                                           ["agent-browser"], "snapshot", "auto", 1)
        assert time.monotonic()-started < 4
        assert result["success"] is (not timeout and output_kind == "json")
        assert PAN in json.dumps(result)  # internal transport remains literal, model boundary redacts
        release.touch()
        from tests.waiting import wait_until
        assert wait_until(ack.exists, timeout=3)
        assert ack.read_text() == "survived"  # no SIGPIPE into the resident daemon
        capture = getattr(processes[0], "_hermes_browser_capture")
        wait_until(lambda: all(not output.thread.is_alive() for output in capture.outputs),
                   timeout=3, what="daemon EOF collector cleanup")
        assert all(output.stream.closed and not output.buffer for output in capture.outputs)
        assert output_files == []
        assert PAN not in caplog.text and CVC not in caplog.text
        assert all(PAN not in p.read_text(errors="ignore") for p in tmp_path.iterdir() if p.is_file())
    finally:
        release.touch()


@pytest.mark.parametrize("label,value,expected", [
    ("CVC", "731", True), ("CVV2", "731", True), ("Security code", "7310", True),
    ("Card verification number", "731", True), ("CID", "731", True),
    ("Search", "731", False), ("CVC", "73", False), ("CVC", "12345", False),
])
def test_short_code_registration_requires_matching_field_metadata(monkeypatch, label, value, expected):
    from tools.browser_tool_typing import register_typed_card_value
    from tools import browser_vault_tool, browser_tool_snapshot
    from agent.redact import has_vault_scoped_components
    clear_vault_redaction_values()
    monkeypatch.setattr(browser_vault_tool, "_current_page_origin", lambda task: ORIGIN)
    try:
        register_typed_card_value("@e731", value, "metadata", lambda: f'- textbox "{label}" [ref=e731]')
        assert has_vault_scoped_components("metadata") is expected
        masked = browser_tool_snapshot._redact_browser_output(f'code={value} ref=e731 ordinal=7310',
                                                              vault_tab="metadata", vault_origin=ORIGIN)
        assert (f'code={value}' not in masked) is expected
        assert 'ref=e731' in masked
        if value != "7310":
            assert 'ordinal=7310' in masked
    finally:
        clear_vault_redaction_values()


@pytest.mark.platforms("windows")
def test_windows_capture_peeks_before_reading_no_data(monkeypatch):
    import _winapi as api
    import msvcrt
    from tools import browser_tool_capture as capture
    monkeypatch.setattr(msvcrt, "get_osfhandle", lambda fd: fd)
    monkeypatch.setattr(api, "PeekNamedPipe", lambda handle, size: (0, 0))
    with pytest.raises(BlockingIOError):
        capture._available_read(1)
    monkeypatch.setattr(api, "PeekNamedPipe", lambda handle, size: (3, 0))
    monkeypatch.setattr(capture.os, "read", lambda fd, size: b"abc" if size == 3 else pytest.fail("unbounded read"))
    assert capture._available_read(1) == b"abc"


def test_stdin_deadline_when_child_never_reads(tmp_path, monkeypatch):
    import os
    import time
    from tools.browser_tool_capture import wait_for_cli
    fixture = tmp_path / "no-read.py"
    fixture.write_text("import time; time.sleep(8)")
    proc = session._popen_agent_browser([sys.executable, str(fixture)], dict(os.environ),
                                       str(tmp_path), "fill", b"x")
    started = time.monotonic()
    try:
        with pytest.raises(subprocess.TimeoutExpired):
            wait_for_cli(proc, .2, b"x" * (1 << 20))
        assert time.monotonic() - started < 2
    finally:
        proc.kill()
        proc.wait(timeout=2)
        session._read_command_output(proc)
    assert not list(tmp_path.glob("_stdin*"))


def test_oversize_transport_failure_clears_both_memory_buffers(tmp_path):
    import os
    from tools.browser_tool_capture import wait_for_cli
    fixture = tmp_path / "oversize.py"
    fixture.write_text("import os; os.write(2, b'7'+b'31'); os.write(1, b'x' * (17 << 20))")
    proc = session._popen_agent_browser([sys.executable, str(fixture)], dict(os.environ),
                                       str(tmp_path), "snapshot")
    try:
        wait_for_cli(proc, 10)
        with pytest.raises(RuntimeError, match="in-memory capture limit"):
            session._read_command_output(proc)
        assert all(not output.buffer for output in getattr(proc, "_hermes_browser_capture").outputs)
        assert not list(tmp_path.glob("_std*"))
    finally:
        if proc.poll() is None:
            proc.kill()
        proc.wait(timeout=2)
