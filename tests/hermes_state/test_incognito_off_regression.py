"""Off-mode persistence and plugin callbacks compared with the exact agreed base."""

from __future__ import annotations

import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tarfile
import tempfile
import unittest


BASE = "e8387e2758"
ROOT = Path(__file__).resolve().parents[2]
SCENARIO = r'''
import json, os, sys, time, uuid
from pathlib import Path
sys.path.insert(0, sys.argv[1])
from hermes_state import SessionDB
from hermes_cli.plugins import PluginContext, PluginManager, PluginManifest
from hermes_cli.middleware import run_tool_execution_middleware
import hermes_cli.plugins as plugins
# Give both revisions identical clock/identity inputs, without replacing any store.
time.time = lambda: 1780000000.0
import datetime, types, hermes_state_schema
class FrozenDateTime(datetime.datetime):
    @classmethod
    def now(cls, tz=None):
        return cls.fromtimestamp(1780000000.0, tz)
hermes_state_schema.datetime = types.SimpleNamespace(datetime=FrozenDateTime, UTC=datetime.UTC)
counter = iter(range(1, 10000))
uuid.uuid4 = lambda: uuid.UUID(int=next(counter))
db = SessionDB(Path(os.environ['HERMES_HOME']) / 'state.db')
trace = []
db._conn.set_trace_callback(trace.append)
db.create_session('normal', 'cli', model='test-model', system_prompt='normal prompt')
first = db.append_message('normal', 'user', 'normal message', message_uid='msg_00000000000000000000000000000001')
db.append_message('normal', 'assistant', 'normal answer', message_uid='msg_00000000000000000000000000000002')
db.set_session_title('normal', 'Normal Title')
db.touch_session_activity('normal', description='normal activity')
db.update_token_counts('normal', input_tokens=4, output_tokens=3, model='test-model', api_call_count=1)
db.record_auxiliary_usage('normal', 'title', model='test-title-model', input_tokens=2)
db.save_gateway_routing_entry('normal-key', json.dumps({'session_id': 'normal', 'preview': 'normal preview'}))
db.replace_gateway_routing_entries({'normal-key': json.dumps({'session_id': 'normal', 'active_turn_token': 'normal-token'})})
assert db.clear_gateway_routing_active_turn('normal-key', 'normal-token')
db.end_session('normal', 'normal end')
# Compare exact writes/triggers, all persisted rows, and checkpointed SQLite pages.
db._conn.set_trace_callback(None)
dump = list(db._conn.iterdump())
db._conn.execute('PRAGMA wal_checkpoint(TRUNCATE)')
raw = db.db_path.read_bytes().hex()
manager = PluginManager()
ctx = PluginContext(PluginManifest(name='ordinary'), manager)
seen = []
def hook(**kwargs):
    seen.append(('hook', kwargs['content']))
    return 'hook result'
ctx.register_hook('on_session_finalize', hook)
ctx.register_middleware('tool_request', hook)
def execute(next_call, **kwargs):
    seen.append(('execution', kwargs['args']))
    return next_call()
ctx.register_middleware('tool_execution', execute)
def event(**kwargs):
    seen.append(('event', kwargs['content']))
ctx.subscribe('hermes:test', event)
ctx.register_system_prompt_section('normal', lambda info: 'normal section')
plugins._delivery_manager = lambda: manager
outputs = [manager.invoke_hook('on_session_finalize', content='normal'),
           manager.invoke_middleware('tool_request', content='normal'),
           run_tool_execution_middleware('terminal', {'normal': True}, lambda payload: payload)]
outputs.append(manager._dispatch_event('hermes:test', {'content': 'normal'}))
assert manager._wait_for_event_dispatch()
sections = [(s.id, s.content, s.plugin) for s in manager.render_system_prompt_sections({})]
manager.unload()
db.close()
print(json.dumps({'trace': trace, 'dump': dump, 'bytes': raw, 'callbacks': seen,
                  'outputs': outputs, 'sections': sections}, sort_keys=True))
'''


class IncognitoOffRegressionTests(unittest.TestCase):
    def test_persistence_pages_writes_and_callbacks_equal_base(self):
        with tempfile.TemporaryDirectory(prefix="incognito-base-", dir=os.environ.get("TMPDIR")) as temp:
            root = Path(temp)
            baseline = root / "base"
            baseline.mkdir()
            archive = subprocess.run(
                ["git", "archive", BASE, "hermes_state*.py", "hermes_cli/"],
                cwd=ROOT, capture_output=True, check=True,
            ).stdout
            with tarfile.open(fileobj=io.BytesIO(archive)) as source:
                source.extractall(baseline, filter="data")
            results = []
            for name, source in [("base", baseline), ("candidate", ROOT)]:
                home = root / name / "home"
                home.mkdir(parents=True)
                env = {**os.environ, "HERMES_HOME": str(home), "PYTHONPATH": str(ROOT)}
                run = subprocess.run([sys.executable, "-c", SCENARIO, str(source)], env=env,
                                     cwd=ROOT, capture_output=True, text=True, timeout=90, check=False)
                self.assertEqual(run.returncode, 0, run.stderr)
                results.append(json.loads(run.stdout))
            for key in results[0]:
                with self.subTest(contract=key):
                    self.assertEqual(results[0][key], results[1][key])


if __name__ == "__main__":
    unittest.main()
