"""Forward-only acceptance against real release-pinned macOS launchd gateways.

No production launch agent or profile is eligible for this harness. Fault hooks
are in the updater or isolated release, never replacements for a gateway or its coordinator.
Reboot is deliberately excluded: changing this host's boot is outside the fixture.
"""
from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
import json
import hashlib
import os
from pathlib import Path
import plistlib
import re
import shlex
import shutil
import signal
import sqlite3
import subprocess
import sys
import tempfile
import time
import uuid
from urllib.parse import parse_qs

import pytest

SIGKILL = getattr(signal, 'SIGKILL', signal.SIGTERM)

pytestmark = [pytest.mark.integration, pytest.mark.platforms("macos"),
              pytest.mark.skipif(sys.platform != 'darwin', reason='requires native launchd'),
              pytest.mark.live_system_guard_bypass]

from gateway.generation import GenerationCoordinator
from hermes_cli import gateway_forward_update as forward
from tests.fakes.fake_llm_provider import FakeLLMServer, Text, ToolCall

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'plugins'))
from telegram_polling_stub import BotAPI

REPOSITORY = Path(__file__).resolve().parents[2]


def evidence_root(rig_root):
    """Retained native evidence: under the runner's scratch root, else beside the rig."""
    scratch = os.environ.get('HERMES_TEST_SCRATCH_ROOT')
    return Path(scratch or Path(rig_root).parent) / 'p3a-evidence'


# The driver allows 45s for handover, including stopped->successor polling proof
# (gateway/run_generation.py:78,114-145). This is the same bound, not a new timeout.
MAX_HANDOVER_GAP_SECONDS = 45
TOKEN = '123456:LOCAL_STUB_ONLY'
BAD_TOKEN = '654321:BAD_RELEASE_STUB_ONLY'
LOCAL_NETWORK_POLICY = (
    'import sys\n'
    'def local_network_only(event,args):\n'
    '    if event == "socket.getaddrinfo" and args[0] not in (None,"localhost","127.0.0.1"):\n'
    '        raise RuntimeError("native acceptance forbids external DNS")\n'
    '    if event == "socket.connect" and isinstance(args[1],tuple) and args[1][0] != "127.0.0.1":\n'
    '        raise RuntimeError("native acceptance forbids external network")\n'
    'sys.addaudithook(local_network_only)\n'
)


def wait(proof, seconds=45, description='proof'):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        value = proof()
        if value:
            return value
        time.sleep(.05)  # Poll an observable condition, never substitute elapsed time for proof.
    raise AssertionError(f'{description} absent after {seconds}s')


class TracedBotAPI(BotAPI):
    """Keep native HTTP token/time evidence and actual delivered update IDs."""
    def __init__(self):
        self.poll_requests = []
        self.send_receipts = []
        self.delivered_ids = set()
        super().__init__()

    def _handler(self):
        original = super()._handler()
        state = self
        class Handler(original):
            def do_POST(self):
                method = self.path.rsplit('/', 1)[-1].lower()
                raw = []
                reader = self.rfile
                class CaptureRead:
                    def read(_self, size):
                        data = reader.read(size)
                        raw.append(data)
                        return data
                    def __getattr__(_self, name):
                        return getattr(reader, name)
                self.rfile = CaptureRead()
                if method == 'getupdates':
                    token = self.path.split('/bot', 1)[1].split('/', 1)[0]
                    with closing(sqlite3.connect(f'file:{state.home / "gateway-coordinator.db"}?mode=ro', uri=True)) as conn:
                        cursor = conn.execute('SELECT confirmed_offset FROM polling_cursors WHERE token_hash=?',
                                              (hashlib.sha256(token.encode()).hexdigest(),)).fetchone()
                    with state.lock:
                        state.poll_requests.append({'token': token, 'at': time.time(),
                                                    'confirmed_offset': cursor[0] if cursor else None})
                if method in {'getupdates', 'sendmessage'}:
                    writer = self.wfile
                    class Capture:
                        def write(_self, data):
                            try:
                                payload = json.loads(data)
                            except (ValueError, UnicodeDecodeError):
                                pass
                            else:
                                if isinstance(payload, dict) and isinstance(payload.get('result'), list):
                                    with state.lock:
                                        state.delivered_ids.update(item['update_id'] for item in payload['result'])
                                elif method == 'sendmessage' and payload.get('ok'):
                                    form = parse_qs(b''.join(raw).decode())
                                    with state.lock:
                                        state.send_receipts.append({'text': form['text'][0], 'at': time.time()})
                            return writer.write(data)
                        def __getattr__(_self, name):
                            return getattr(writer, name)
                    self.wfile = Capture()
                return super().do_POST()
        return Handler


class NativeSupervisor(forward.GenerationSupervisor):
    """Only adapt the production label namespace and environment for isolation."""
    def __init__(self, home, directory=None, domain=None):
        self.labels = set()
        self.extra_env = {}
        super().__init__(home, directory=Path(directory or Path(home) / 'plists'),
                         domain=domain or f"gui/{getattr(os, 'getuid')()}", runner=self.command)

    def command(self, args, **kwargs):
        assert args[0] == 'launchctl'
        if args[1] == 'bootstrap':
            data = plistlib.loads(Path(args[-1]).read_bytes())
            label = data['Label']
            assert data['EnvironmentVariables']['HERMES_HOME'] == str(self.home)
        else:
            label = args[-1].rsplit('/', 1)[-1]
        assert label.startswith('ai.hermes.p3test-') and label in self.labels, args
        return subprocess.run(args, **kwargs)

    def install(self, row, release):
        label = row['label']
        assert label.startswith('ai.hermes.p3test-')
        self.labels.add(label)
        self.directory.mkdir(exist_ok=True)
        path = self.directory / f'{label}.plist'
        assert not path.exists()
        env = {'HERMES_HOME': str(self.home), 'HOME': str(self.home.parent),
               'PYTHONPATH': str(release), 'HERMES_RELEASE_SHA': row['release_sha'],
               'HERMES_LAUNCHD_LABEL': label, 'HERMES_GENERATION_SCOPE': uuid.uuid4().hex,
               'HERMES_GATEWAY_LOCK_DIR': str(self.home / 'locks'),
               'HERMES_TEST_ISOLATION': '1', 'HERMES_DISABLE_LAZY_INSTALLS': '1',
               'HERMES_KANBAN_HOME': str(self.home),
               'HERMES_KANBAN_DB': str(self.home / 'kanban/kanban.db'),
               'HERMES_TELEGRAM_DISABLE_FALLBACK_IPS': '1',
               'OPENAI_API_KEY': 'local-test-key', 'TMPDIR': str(self.home.parent),
               'PATH': '/usr/bin:/bin:/usr/sbin:/sbin', 'TZ': 'UTC',
               'PYTHONDONTWRITEBYTECODE': '1', **self.extra_env}
        path.write_bytes(plistlib.dumps({
            'Label': label, 'WorkingDirectory': str(release),
            'ProgramArguments': [str(release / '.venv/bin/python'), '-m', 'hermes_cli.main',
                                 'gateway', 'run', '--standby'],
            'EnvironmentVariables': env, 'RunAtLoad': False,
            'KeepAlive': {'SuccessfulExit': False}, 'ThrottleInterval': 1,
            'StandardOutPath': str(self.home / f'{label}.out'),
            'StandardErrorPath': str(self.home / f'{label}.err')}))
        return path

    def bootstrap(self, row, path, timeout, *, before_launch=None):
        self.labels.add(row['label'])
        from hermes_cli.gateway_launchd_generation import refresh_generation_scope
        self._definition(row['label'])
        refresh_generation_scope(path)
        if before_launch:
            before_launch(plistlib.loads(path.read_bytes())['EnvironmentVariables']['HERMES_GENERATION_SCOPE'])
        self.command(['launchctl', 'bootstrap', self.domain, str(path)], check=True, timeout=min(30, timeout))

    def job(self, row):
        return self.command(['launchctl', 'print', f"{self.domain}/{row['label']}"],
                            capture_output=True, text=True, timeout=5)


def isolated_inventory(home):
    """Read only this fixture's durable owners, never enumerate host runtimes."""
    from hermes_cli.update_receipt import record_forward_inventory
    rows = GenerationCoordinator(home).generations()
    assert all(row['label'].startswith('ai.hermes.p3test-') for row in rows)
    record_forward_inventory([])


class Rig:
    def __init__(self, name):
        self.root = Path(tempfile.mkdtemp(prefix='p3a-', dir='/tmp')).resolve()
        self.home = self.root / 'h'
        self.home.mkdir()
        self.name = name
        self.api = TracedBotAPI()
        self.api.home = self.home
        self.bad_gate = False
        self.metrics = {'pytest_home': os.environ['HERMES_HOME']}
        self.llm = FakeLLMServer(self.model, aux=self.model)
        self.llm.start()
        self.supervisor = NativeSupervisor(self.home)
        self.db = GenerationCoordinator(self.home)
        self.a, self.b = [self.release(char * 40) for char in ('a', 'b')]
        (self.home / 'current').symlink_to(self.a)
        self.write_config()

    def release(self, sha):
        release = self.home / 'releases' / sha
        release.mkdir(parents=True)
        # Real code files at release paths (symlinking source breaks loaded-root
        # proof). Hardlinks save I/O. Nothing in the release is edited in place.
        excluded = {'.git', '.worktrees', '.venv', 'venv', 'tests', 'website', 'apps',
                    'web', 'ui-tui', 'node_modules', '__pycache__', '.pytest_cache', '.ruff_cache',
                    '.release-ready', '.hermes_build_sha', 'install-stamp.json', 'sitecustomize.py'}
        def ignore(_directory, names):
            return [n for n in names if n in excluded or n.endswith('.pyc')]
        resource_directories = {'contributors', 'optional-skills', 'skills', 'plugin-catalog',
                                'evals', 'optional-mcps', 'maintenance'}
        for path in REPOSITORY.iterdir():
            if path.name in excluded:
                continue
            if path.name in resource_directories:
                (release / path.name).symlink_to(path, target_is_directory=True)
            elif path.is_dir():
                shutil.copytree(path, release / path.name, copy_function=os.link,
                                ignore=ignore, symlinks=True)
            elif path.is_file():
                os.link(path, release / path.name)
        (release / '.venv').symlink_to(REPOSITORY / '.venv', target_is_directory=True)
        # Process audit policy enforces the fixture's loopback-only contract.
        (release / 'sitecustomize.py').write_text(LOCAL_NETWORK_POLICY, encoding='utf-8')
        from scripts.write_install_stamp import build_stamp
        stamp = build_stamp(commit=sha, branch='', dirty=False, source='local',
                            update_mechanism='self')
        (release / 'install-stamp.json').write_text(json.dumps(stamp) + '\n', encoding='utf-8')
        for name in ('.release-ready', '.hermes_build_sha'):
            (release / name).write_text(sha, encoding='utf-8')
        assert forward.capable(release) and forward._release_is_ready(release, sha)
        return release

    def write_config(self, token=TOKEN):
        (self.home / 'config.yaml').write_text(
            'model:\n  provider: custom\n  default: fake-model\n'
            f'  base_url: {self.llm.base_url}\n  key_env: OPENAI_API_KEY\n'
            'agent:\n  api_max_retries: 1\napprovals:\n  mode: manual\n  timeout: 120\n'
            'kanban:\n  dispatch_interval_seconds: 1\nupdates:\n  check: false\nterminal:\n  cwd: ' + str(self.home) + '\n'
            'gateway:\n  forward_only_handover:\n    enabled: true\n'
            '  durable_outbox:\n    enabled: true\nstreaming:\n  enabled: false\n'
            'platforms:\n  telegram:\n    enabled: true\n    token: "' + token + '"\n'
            '    extra:\n      base_url: "' + self.api.url + '"\n'
            '      base_file_url: "' + self.api.url + '"\n'
            "      allow_from: ['1', '2', '3', '4']\n      drop_pending_on_cold_boot: false\n", encoding='utf-8')
        (self.home / '.env').write_text('OPENAI_API_KEY=local-test-key\n', encoding='utf-8')

    def model(self, record):
        messages = record['body']['messages']
        user = next((m for m in reversed(messages) if m.get('role') == 'user'), {})
        content = str(user.get('content', ''))
        match = re.search(r'HERMES_READY ([a-f0-9]{32})', content)
        if match:
            assert not record['body'].get('tools'), 'gate was given a tool schema'
            return Text('WRONG' if self.bad_gate else 'HERMES_READY ' + match[1])
        if 'fresh recovery of interrupted session' in content:
            # A restored interrupted user turn can be coalesced into this
            # message. The explicit new input, not that context, owns the reply.
            return Text('fresh-final')
        if '[Continuing toward your standing goal]' in content and 'native wake proof' in content:
            return Text('goal-wakeup-final')
        card = re.search(r'work kanban task ([a-zA-Z0-9_-]+)', content)
        if card:
            if messages[-1].get('role') == 'tool':
                return Text('kanban-final')
            return ToolCall('kanban_complete', {'task_id': card[1], 'summary': 'native abort rearm proof'})
        if 'child-final' in content:
            if messages[-1].get('role') == 'tool':
                return Text('delegation-final ' + re.search(r'[ab]{40}', str(messages[-1]['content']))[0])
            return ToolCall('terminal', {'command': 'echo "$HERMES_RELEASE_SHA"'})
        if 'child-proof' in content:
            if messages[-1].get('role') == 'tool':
                return Text('child-final')
            return ToolCall('terminal', {'command': self.tool('child', 60)})
        if messages[-1].get('role') == 'tool':
            if 'followup-old' in content:
                return Text('followup-final ' + re.search(r'[ab]{40}', str(messages[-1]['content']))[0])
            if 'release-proof' in content:
                return Text('release-proof ' + str(messages[-1].get('content', '')))
            if 'delegate-old' in content:
                return Text('delegation-dispatched')
            return Text('approval-final' if 'approval-old' in content else 'inflight-final')
        if 'approval-old' in content:
            return ToolCall('terminal', {'command': 'chmod 777 ' + shlex.quote(str(self.home / 'approval-target')) +
                             ' && ' + self.tool('approval', 1)})
        if 'inflight-old' in content:
            return ToolCall('terminal', {'command': self.tool('inflight', 60)})
        if 'delegate-old' in content:
            return ToolCall('delegate_task', {'tasks': [{'goal': 'child-proof'}], 'background': True})
        if 'followup-old' in content:
            return ToolCall('terminal', {'command': 'echo "$HERMES_RELEASE_SHA"'})
        if 'release-proof' in content:
            return ToolCall('terminal', {'command': 'echo \"$HERMES_RELEASE_SHA\"'})
        if 'delegat' in content.lower() and 'child-final' in content:
            return Text('delegation-final')
        return Text('fresh-final')

    def tool(self, name, seconds):
        # Durable actual executing PID/release plus completion, not a timing mock.
        marker = self.home / name
        return ("printf '%s' \"$HERMES_RELEASE_SHA\" > " + shlex.quote(str(marker.with_suffix('.start'))) +
                f' && sleep {seconds} && ' +
                "printf '%s' \"$HERMES_RELEASE_SHA\" > " + shlex.quote(str(marker.with_suffix('.done'))))

    def row(self, gid):
        return next(row for row in self.db.generations() if row['id'] == gid)

    def holder(self):
        leases = self.db.leases()
        return self.row(next(row for row in leases if row['resource'] == 'active_generation')['generation_id'])

    def start(self):
        gid = str(uuid.uuid4())
        identity = self.db.reserve_generation(generation_id=gid, label='ai.hermes.p3test-' + uuid.UUID(gid).hex,
                                              release_sha=self.a.name)
        row = self.row(identity.id)
        path = self.supervisor.install(row, self.a)
        self.supervisor.boot_active(row, True)
        self.supervisor.bootstrap(row, path, 30)
        started = time.monotonic()
        wait(lambda: self.row(gid)['state'] == 'serving' and self.supervisor.ready(self.row(gid)), description='A ready')
        wait(lambda: self.supervisor.request(self.row(gid), 'polling_status').get('polling'),
             45, 'initial adapters polling')
        forward._poller(self.db, self.row(gid), self.supervisor, time.monotonic() + forward.POLL_SECONDS)
        self.metrics['initial_startup_seconds'] = time.monotonic() - started
        return self.row(gid)

    def send(self, uid, text, chat=1):
        self.api.add(uid, uid, text=text, chat_id=chat)

    def seen(self, text):
        with self.api.lock:
            return [m for m in self.api.sent if text in m['text'].replace('\\', '')]

    def admitted(self, uid):
        with closing(self.db.connect()) as conn:
            return [dict(row) for row in conn.execute('SELECT * FROM inbox WHERE source_event_id=?', (str(uid),))]

    def promote(self):
        from hermes_cli.update_receipt import begin_update_receipt, finalize_update_receipt, forward_receipt_outcome
        begin_update_receipt()
        result = forward.promote_forward(self.home, self.b, self.b.name, supervisor=self.supervisor)
        receipt_path = finalize_update_receipt(forward_receipt_outcome(result['outcome']), fleet=[])
        assert receipt_path is not None and receipt_path.is_relative_to(self.home)
        receipt = json.loads(receipt_path.read_text(encoding='utf-8'))
        # Receipt consumers keep their existing vocabulary; the forward outcome stays nested.
        assert receipt['outcome'] == {'rolled_back': 'partial', 'aborted': 'refused', 'blocked': 'failed',
                                      'locked': 'refused'}.get(result['outcome'], result['outcome'])
        assert receipt['forward_generation']['outcome'] == result['outcome']
        self.metrics['update_receipt'] = str(receipt_path.relative_to(self.home))
        self.metrics['promotion'] = result
        return result

    def poller_proof(self, delivered):
        journal = self.db.poller_journal()
        live, lock, previous_stop, gaps = {}, {}, {}, []
        for event in sorted(journal, key=lambda e: (e['monotonic_at'], e['id'])):
            token, owner, stamp = event['token_hash'], (event['generation_id'], event['epoch']), event['monotonic_at']
            if event['event'] == 'lock_acquired':
                assert token not in lock, ('overlapping token locks', journal)
                lock[token] = owner
            elif event['event'] == 'lock_released':
                assert lock.pop(token) == owner
            elif event['event'] == 'poller_started':
                assert token not in live, ('overlapping poller intervals', journal)
                assert lock.get(token) == owner
                live[token] = owner
                if token in previous_stop:
                    gaps.append(stamp - previous_stop[token])
            elif event['event'] == 'poller_stopped':
                assert live.pop(token) == owner
                previous_stop[token] = stamp
        assert len(live) == 1
        assert gaps and max(gaps) <= MAX_HANDOVER_GAP_SECONDS
        self.metrics['maximum_zero_poller_gap_seconds'] = max(gaps)
        with closing(self.db.connect()) as conn:
            updates = conn.execute('SELECT update_id,state FROM telegram_updates ORDER BY update_id').fetchall()
            assert [(r[0], r[1]) for r in updates] == [(uid, 'accepted') for uid in delivered]
            for uid in delivered:
                rows = conn.execute("SELECT owner_id,state FROM inbox WHERE source_event_id=? AND kind='message'", (str(uid),)).fetchall()
                assert len(rows) == 1 and rows[0]['state'] == 'accepted'
            cursor = conn.execute('SELECT confirmed_offset FROM polling_cursors').fetchone()[0]
        assert cursor >= max(delivered) + 1
        assert self.api.offsets == sorted(self.api.offsets)
        confirmed = [q['confirmed_offset'] for q in self.api.poll_requests
                     if q['token'] == TOKEN and q['confirmed_offset'] is not None]
        assert confirmed and confirmed == sorted(confirmed)
        assert cursor >= confirmed[-1]
        assert self.api.delivered_ids == set(delivered)
        assert self.api.maximum == 1
        assert not self.api.errors

    def close(self):
        target = evidence_root(self.root) / (self.name + '-' + self.root.name)
        try:
            self.supervisor.labels.update(row['label'] for row in self.db.generations())
            target.mkdir(parents=True, exist_ok=True)
            self.preserve_evidence(target)
        finally:
            try:
                self.unload_owned_jobs(target)
            finally:
                self.api.close()
                self.llm.stop()
                if target.is_dir():
                    shutil.copytree(self.home, target / 'home', symlinks=True, dirs_exist_ok=True,
                                    ignore=lambda _p, names: [n for n in names
                                                              if n in {'releases'} or n.endswith('.sock')])

    def preserve_evidence(self, target):
        # Preserve every native log/DB/plist before bootout, plus readbacks.
        jobs = {}
        for label in self.supervisor.labels:
            row = {'label': label}
            jobs[label] = self.supervisor.job(row).stdout
        (target / 'jobs.json').write_text(json.dumps(jobs, indent=2), encoding='utf-8')
        (target / 'metrics.json').write_text(json.dumps(self.metrics, indent=2), encoding='utf-8')
        print('NATIVE_METRICS', self.name, json.dumps({'evidence': str(target), **self.metrics}, sort_keys=True))
        (target / 'stub.json').write_text(json.dumps({'offsets': self.api.offsets, 'maximum': self.api.maximum,
                                                   'sent': self.api.sent, 'errors': self.api.errors,
                                                   'poll_requests': self.api.poll_requests,
                                                   'send_receipts': self.api.send_receipts,
                                                   'delivered_ids': sorted(self.api.delivered_ids)}, indent=2), encoding='utf-8')
        (target / 'llm-requests.json').write_text(json.dumps([
            {key: request[key] for key in ('kind', 'body', 'response') if key in request}
            for request in self.llm.requests], indent=2), encoding='utf-8')

    def unload_owned_jobs(self, target):
        # Only this rig's labels are inspected: concurrent native runs own
        # other p3test jobs, and a global count would fail or race them.
        cleanup_deadline = time.monotonic() + 15
        for label in sorted(self.supervisor.labels):
            self.supervisor.command(['launchctl', 'bootout', f'{self.supervisor.domain}/{label}'],
                                    capture_output=True, timeout=max(.1, cleanup_deadline - time.monotonic()))
            wait(lambda label=label: self.supervisor.job({'label': label}).returncode != 0,
                 max(.1, cleanup_deadline - time.monotonic()), 'owned label bootout')
        counts = []
        def unloaded_inventory():
            listed = subprocess.run(['launchctl', 'list'], capture_output=True, text=True,
                                    encoding='utf-8', check=True).stdout
            loaded = {line.rsplit(None, 1)[-1] for line in listed.splitlines() if line.strip()}
            counts.append(len(self.supervisor.labels & loaded))
            if target.is_dir():
                (target / 'cleanup-counts.json').write_text(json.dumps(counts), encoding='utf-8')
            return counts[-1] == 0
        wait(unloaded_inventory, max(.1, cleanup_deadline - time.monotonic()), 'owned p3test jobs unloaded')
        assert counts[-1] == 0


@pytest.fixture(scope='session', autouse=True)
def acceptance_pytest_root(tmp_path_factory):
    # The repository moves basetemps under ~/.hermes to /var/tmp. Pin its
    # function-scoped HERMES_HOME fixtures to an allowed short root instead.
    root = Path(tempfile.mkdtemp(prefix='p3a-pytest-', dir='/tmp')).resolve()
    tmp_path_factory._given_basetemp = root
    tmp_path_factory._basetemp = None
    original_home = os.environ.get('HOME')
    os.environ['HOME'] = str(root)
    try:
        yield
    finally:
        if original_home is None:
            os.environ.pop('HOME', None)
        else:
            os.environ['HOME'] = original_home


@pytest.fixture
def rig(request, monkeypatch):
    r = Rig(request.node.name)
    monkeypatch.setenv('HERMES_HOME', str(r.home))
    monkeypatch.setenv('HERMES_KANBAN_HOME', str(r.home))
    monkeypatch.setenv('HERMES_KANBAN_DB', str(r.home / 'kanban/kanban.db'))
    monkeypatch.setattr(forward, 'generation_launchd_label', lambda slot: 'ai.hermes.p3test-' + uuid.UUID(slot).hex)
    monkeypatch.setattr(forward, 'require_forward_inventory', isolated_inventory)
    try:
        yield r
    finally:
        r.close()


def test_planned_promotion_owns_inflight_approval_and_queue(rig):
    r = rig
    old = r.start()
    (r.home / 'approval-target').touch()
    r.send(1001, 'inflight-old', 1)
    wait(lambda: (r.home / 'inflight.start').exists(), 30, 'inflight tool started')
    r.send(1002, 'approval-old', 2)
    wait(lambda: r.seen('needs your OK'), 30, 'approval prompt')
    assert not (r.home / 'approval.start').exists()
    r.send(1003, 'delegate-old', 4)
    wait(lambda: (r.home / 'child.start').exists(), 30, 'background child started')
    result = r.promote()
    assert result['outcome'] == 'success', result
    assert r.row(old['id'])['state'] == 'draining'
    assert forward._live(old), 'A exited with work outstanding'
    assert not (r.home / 'inflight.done').exists(), 'tool did not span handover'
    assert not (r.home / 'child.done').exists(), 'delegation did not span handover'
    assert not (r.home / 'approval.start').exists(), 'approval executed before postcommit answer'
    assert (r.home / 'current').resolve() == r.b
    r.send(1004, '/approve', 2)
    wait(lambda: (r.home / 'approval.done').exists(), 20, 'approval executed on A')
    assert (r.home / 'approval.done').read_text(encoding='utf-8') == r.a.name
    r.send(1005, '/queue followup-old', 1)
    wait(lambda: r.admitted(1005), 15, 'queued followup')
    assert r.admitted(1005)[0]['owner_id'] == old['id']
    r.send(1006, 'release-proof', 3)
    wait(lambda: r.seen(r.b.name), 20, 'B fresh reply')
    assert r.admitted(1006)[0]['owner_id'] == result['new_id']
    assert r.row(result['new_id'])['release_sha'] == r.b.name
    finals = ('inflight-final', 'approval-final', 'followup-final ' + r.a.name, 'delegation-final ' + r.a.name)
    for final in finals:
        wait(lambda final=final: r.seen(final), 90, final)
        assert len(r.seen(final)) == 1
        def durable_final():
            with closing(sqlite3.connect(r.home / 'gateway-outbox.db')) as conn:
                return conn.execute("SELECT state FROM outbox WHERE json_extract(payload,'$.content')=?", (final,)).fetchall()
        wait(lambda: durable_final() == [('delivered',)], 5, 'exactly one delivered outbox final')
        assert durable_final() == [('delivered',)]
    assert (r.home / 'inflight.done').read_text(encoding='utf-8') == r.a.name
    assert (r.home / 'child.done').read_text(encoding='utf-8') == r.a.name
    for name in ('inflight', 'child'):
        assert ((r.home / f'{name}.start').stat().st_mtime < result['commit_at']
                < (r.home / f'{name}.done').stat().st_mtime)
    assert (r.home / 'approval.start').stat().st_mtime > result['commit_at']
    with closing(sqlite3.connect(r.home / 'state.db')) as conn:
        delegation = conn.execute('SELECT state,delivery_state,owner_pid FROM async_delegations').fetchall()
    assert delegation == [('completed', 'delivered', old['pid'])]
    assert r.admitted(1004)[0]['owner_id'] == old['id']
    wait(lambda: r.row(old['id'])['state'] == 'exited' and not forward._live(old), 30, 'A exited after finals')
    exit_at = r.row(old['id'])['heartbeat_at']
    final_times = [receipt['at'] for receipt in r.api.send_receipts
                   if any(final in receipt['text'].replace('\\', '') for final in finals)]
    assert len(final_times) == len(finals)
    assert max(final_times) < exit_at, 'A exited before its last final was accepted by BotAPI'
    r.metrics['last_final_to_A_exit_seconds'] = exit_at - max(final_times)
    forward.cleanup_exited(r.home, supervisor=r.supervisor)
    assert r.supervisor.job(old).returncode != 0
    definitions = [plistlib.loads(p.read_bytes()) for p in r.supervisor.directory.glob('*.plist')]
    assert [p['Label'] for p in definitions if p['RunAtLoad']] == [result['new_label']]
    wait(lambda: r.api.confirmed >= 1006, 10, 'cursor confirmed')
    r.poller_proof([1001, 1002, 1003, 1004, 1005, 1006])


def test_bad_startup_gate_never_polls_or_calls_tools(rig):
    r = rig
    old = r.start()
    r.bad_gate = True
    r.write_config(BAD_TOKEN)
    before = len(r.llm.requests)
    result = r.promote()
    assert result['outcome'] == 'refused', result
    bad = r.row(result['new_id'])
    assert (bad['state'], bad['verdict']) == ('exited', 'failed')
    assert not [e for e in r.db.poller_journal() if e['generation_id'] == bad['id']]
    gate = json.loads(bad['verdict_evidence'])['gate']
    with closing(sqlite3.connect(r.home / 'gateway-outbox.db')) as conn:
        bad_reply = conn.execute('SELECT state,send_status,message_id FROM outbox WHERE turn_id=?',
                                ('startup-gate:' + gate['nonce'],)).fetchall()
    assert bad_reply == [('failed_unsent', 'synthetic', None)], 'bad gate reply must be terminal and unsent'
    assert gate['tool_attempted'] is False
    assert gate['zero_tools'] is True
    assert all(not q['body'].get('tools') for q in r.llm.requests[before:])
    assert r.holder()['id'] == old['id']
    assert not [q for q in r.api.poll_requests if q['token'] == BAD_TOKEN]
    assert [e['event'] for e in r.db.poller_journal() if e['generation_id'] == old['id']
            and e['event'].startswith('poller_')] == ['poller_started']
    r.send(2001, 'fresh after refused')
    wait(lambda: r.seen('fresh-final'), 20, 'A still polling')
    r.bad_gate = False
    r.write_config()
    good = r.promote()
    assert good['outcome'] == 'success', good
    from gateway.outbox import Outbox, recover
    class NoSend:
        gateway_runner = None
        def __getattr__(self, name):
            raise AssertionError('synthetic recovery attempted transport: ' + name)
    assert asyncio.run(recover(Outbox(r.home), NoSend())) == (0, 0)
    with closing(sqlite3.connect(r.home / 'gateway-outbox.db')) as conn:
        rows = conn.execute("SELECT state,send_status,message_id FROM outbox WHERE turn_id LIKE 'startup-gate:%'").fetchall()
    assert rows and all(row == ('failed_unsent', 'synthetic', None) for row in rows)
    assert not r.seen('HERMES_READY') and not r.seen('WRONG')


def test_precommit_abort_rearms_and_answers_fresh_work(rig, monkeypatch):
    r = rig
    old = r.start()
    real = forward.handover_to_generation
    def fail_before_commit(home, gid, **kwargs):
        def fail():
            raise RuntimeError('native precommit fault after real stop receipt')
        kwargs['before_commit'] = fail
        return real(home, gid, **kwargs)
    monkeypatch.setattr(forward, 'handover_to_generation', fail_before_commit)
    result = r.promote()
    assert result['outcome'] == 'aborted', result
    assert result['resume']['armed'] == dict.fromkeys(('poller', 'cron', 'kanban', 'goal_wakeup'), True)
    assert r.holder()['id'] == old['id']
    r.send(3001, 'fresh after abort')
    wait(lambda: r.seen('fresh-final'), 20, 'A fresh work after abort')
    assert r.admitted(3001)[0]['owner_id'] == old['id']
    # All work is created AFTER abort. The launchd gateway's real tickers must
    # execute it. Readback of boolean fences alone is insufficient.
    from datetime import datetime, timezone, timedelta
    from cron.jobs import create_job
    script = r.home / 'scripts/cron-proof.sh'
    script.parent.mkdir(exist_ok=True)
    marker = r.home / 'cron-fired'
    script.write_text('echo fired >> ' + shlex.quote(str(marker)) + '\n', encoding='utf-8')
    job = create_job(None, (datetime.now(timezone.utc) + timedelta(seconds=1)).isoformat(),
                     name='native rearm', script=str(script), no_agent=True, repeat=1, deliver='local')
    from hermes_cli import kanban_db as kb, kanban_db_connect as kbc
    with closing(kbc.connect()) as conn:
        card_id = kb.create_task(conn, title='native rearm proof', assignee='default',
                                 workspace_kind='scratch')
    with closing(sqlite3.connect(r.home / 'state.db')) as conn:
        sid = conn.execute("SELECT id FROM sessions WHERE session_key='agent:main:telegram:dm:1'").fetchone()[0]
    from hermes_cli.goals import GoalManager
    goal = GoalManager(sid)
    goal.set('native wake proof', max_turns=1)
    goal.wait_for_seconds(1, reason='native abort rearm proof')
    wait(lambda: r.seen('goal-wakeup-final'), 40, 'autonomous goal wake after abort')
    assert len(r.seen('goal-wakeup-final')) == 1
    def card_done():
        with closing(kbc.connect()) as conn:
            return conn.execute('SELECT status FROM tasks WHERE id=?', (card_id,)).fetchone()[0] == 'done'
    wait(card_done, 40, 'real kanban worker completed after abort')
    with closing(kbc.connect()) as conn:
        runs = conn.execute('SELECT status,worker_pid FROM task_runs WHERE task_id=?', (card_id,)).fetchall()
    assert len(runs) == 1 and runs[0]['status'] == 'done' and runs[0]['worker_pid'] > 0
    wait(lambda: marker.exists(), 70, 'scheduled cron tick after abort')
    assert marker.read_text(encoding='utf-8').splitlines() == ['fired']
    r.metrics['rearmed_work'] = {'cron_job': job['id'], 'kanban_card': card_id, 'goal_session': sid}
    wait(lambda: r.api.confirmed >= 3001, 10, 'abort cursor')
    r.poller_proof([3001])


def parked(supervisor, row):
    result = supervisor.job(row)
    return (result.returncode == 0 and not re.search(r'^\s*pid\s*=\s*[1-9]\d*\s*$', result.stdout, re.M)
            and re.search(r'^\s*last exit (?:code|status)\s*=\s*0\s*$', result.stdout, re.M))


def test_successor_sigkill_rolls_back_to_fresh_previous(rig, monkeypatch):
    r = rig
    old = r.start()
    r.send(4001, 'inflight-old')
    wait(lambda: (r.home / 'inflight.start').exists(), 30, 'A tool started')
    real_commit = GenerationCoordinator.commit_transfer
    death = []
    def kill_after_commit(db, *args, **kwargs):
        epoch = real_commit(db, *args, **kwargs)
        committed = time.monotonic()
        row = r.holder()
        if row['release_sha'] == r.b.name and not death:
            assert row['state'] == 'serving'
            death.append(time.monotonic())
            os.kill(row['pid'], SIGKILL)
            r.metrics['commit_to_kill_seconds'] = death[0] - committed
            assert r.metrics['commit_to_kill_seconds'] < 1
            wait(lambda: parked(r.supervisor, row), 15, 'KeepAlive consumed-scope exit 0')
            assert r.row(row['id'])['pid'] == row['pid'], 'respawn won a second claim'
        return epoch
    # Updater-side fault after the REAL transaction, before its first postcommit
    # observation. The native gateway process and coordinator are not replaced.
    monkeypatch.setattr(GenerationCoordinator, 'commit_transfer', kill_after_commit)
    with ThreadPoolExecutor(max_workers=1) as pool:
        task = pool.submit(r.promote)
        fresh = wait(lambda: next((row for row in r.db.generations()
                                   if row['release_sha'] == r.a.name and row['id'] != old['id']
                                   and row['state'] == 'serving' and r.supervisor.ready(row)
                                   and any(e['generation_id'] == row['id'] and e['event'] == 'poller_started'
                                           for e in r.db.poller_journal())), None), 60, "A-prime serving with durable poller")
        r.send(4002, 'fresh rollback', 3)
        wait(lambda: r.seen('fresh-final'), max(.1, 60 - (time.monotonic() - death[0])), 'rollback reply')
        elapsed = time.monotonic() - death[0]
        assert elapsed <= 60
        result = task.result(timeout=60)
    assert result['outcome'] == 'rolled_back', result
    assert result['rollback']['reply_observed'] and result['rollback']['rollback_bound_met']
    assert r.admitted(4002)[0]['owner_id'] == fresh['id']
    assert (r.home / 'current').resolve() == r.a
    assert r.row(result['new_id'])['verdict'] == 'failed'
    old_events = [e['event'] for e in r.db.poller_journal() if e['generation_id'] == old['id']]
    assert old_events.count('poller_started') == 1
    assert old_events.count('poller_stopped') == 1
    assert r.api.maximum == 1
    r.metrics['rollback_death_to_reply_seconds'] = elapsed


def updater_worker(home, boundary):
    """Real promotion in a killable observer process, with a precise fault hook."""
    home = Path(home)
    forward.generation_launchd_label = lambda slot: 'ai.hermes.p3test-' + uuid.UUID(slot).hex
    forward.require_forward_inventory = isolated_inventory
    supervisor = NativeSupervisor(home)
    supervisor.labels.update(row['label'] for row in GenerationCoordinator(home).generations())
    marker = home / 'updater-boundary'
    def die():
        marker.write_text(boundary, encoding='utf-8')
        os.kill(os.getpid(), SIGKILL)
    if boundary == 'before_commit':
        real = forward.handover_to_generation
        def stop_before_commit(home, gid, **kwargs):
            original = kwargs['before_commit']
            def fault():
                original()
                die()
            kwargs['before_commit'] = fault
            return real(home, gid, **kwargs)
        forward.handover_to_generation = stop_before_commit
    else:
        real = forward._poller
        def stop_after_commit(db, row, supervisor, deadline):
            die()
            return real(db, row, supervisor, deadline)
        forward._poller = stop_after_commit
    forward.promote_forward(home, home / 'releases' / ('b' * 40), 'b' * 40, supervisor=supervisor)


@pytest.mark.parametrize('boundary', ['before_commit', 'after_commit'])
def test_updater_sigkill_recovery_observes_one_owner(rig, boundary):
    r = rig
    old = r.start()
    # Keep A draining long enough to inspect the postcommit observer readback.
    r.send(5001, 'inflight-old')
    wait(lambda: (r.home / 'inflight.start').exists(), 30, 'A tool running')
    env = {'PATH': '/usr/bin:/bin:/usr/sbin:/sbin', 'HOME': str(r.root), 'HERMES_HOME': str(r.home),
           'TMPDIR': str(r.root), 'PYTHONPATH': str(REPOSITORY), 'HERMES_TEST_ISOLATION': '1'}
    with (r.home / 'updater.stderr').open('w') as output:
        proc = subprocess.Popen([sys.executable, '-c',
            'from tests.hermes_cli.test_gateway_forward_acceptance_real import updater_worker; '
            'import sys; updater_worker(sys.argv[1],sys.argv[2])', str(r.home), boundary],
            cwd=REPOSITORY, env=env, stdout=output, stderr=output)
        try:
            wait(lambda: (r.home / 'updater-boundary').exists(), 60, 'updater fault boundary')
            assert proc.wait(timeout=10) == -SIGKILL
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait(timeout=10)
    r.supervisor.labels.update(row['label'] for row in r.db.generations())
    if boundary == 'before_commit':
        def rearmed():
            status = r.supervisor.request(r.row(old['id']), 'polling_status')
            return status.get('polling') and all(status['armed'].values())
        # The existing 45s abort deadline is followed by the coded 5s poller
        # proof window (gateway_forward_update.POLL_SECONDS), without changing either.
        wait(rearmed, 45 + forward.POLL_SECONDS, 'A deadline rearm')
    lease_before = r.db.leases()
    moves_before = None
    with closing(r.db.connect()) as conn:
        moves_before = list(conn.execute('SELECT * FROM lease_moves'))
    result = forward.recover_forward(r.home, supervisor=r.supervisor)
    assert result['outcome'] == ('aborted' if boundary == 'before_commit' else 'success'), result
    assert r.db.leases() == lease_before, 'recovery moved the lease'
    with closing(r.db.connect()) as conn:
        assert list(conn.execute('SELECT * FROM lease_moves')) == moves_before
    r.send(5002, 'fresh observer recovery', 3)
    wait(lambda: r.seen('fresh-final'), 25, 'recovered owner reply')
    assert r.admitted(5002)[0]['owner_id'] == r.holder()['id']
    r.metrics['recovery'] = result
    assert r.api.maximum == 1


@pytest.mark.parametrize('stop_kind', ['clean', 'crash'])
def test_cold_start_new_scope_answers_fresh(rig, stop_kind):
    r = rig
    old = r.start()
    path = r.supervisor.directory / f"{old['label']}.plist"
    if stop_kind == 'crash':
        os.kill(old['pid'], SIGKILL)
        wait(lambda: parked(r.supervisor, old), 15, 'crashed holder parked after CAS loss')
        from hermes_cli.gateway_guardian import run_once
        outcome = run_once(r.home, path, old['label'], domain=r.supervisor.domain,
                           launchctl_runner=r.supervisor.command)
        assert outcome == 'repaired', outcome
    else:
        # launchd delivers graceful SIGTERM, then the launcher bootstraps a fresh
        # nonce. No gateway CLI service command is used.
        r.supervisor.command(['launchctl', 'bootout', f"{r.supervisor.domain}/{old['label']}"],
                             capture_output=True, timeout=15)
        wait(lambda: r.row(old['id'])['state'] == 'exited' and not forward._live(old), 15, 'clean shutdown')
        r.supervisor.bootstrap(old, path, 30)
    fresh = wait(lambda: next((row for row in r.db.generations() if row['label'] == old['label']
                               and row['id'] != old['id'] and row['state'] == 'serving'
                               and r.supervisor.ready(row)), None),
                 45, 'cold scope serving')
    assert fresh['scope_nonce'] != old['scope_nonce']
    assert fresh['pid'] != old['pid']
    if stop_kind == 'crash':
        assert (r.row(old['id'])['state'], r.row(old['id'])['verdict']) == ('exited', 'failed')
    r.send(6001, 'fresh cold scope', 3)
    wait(lambda: r.seen('fresh-final'), 25, 'cold start fresh reply')
    assert r.admitted(6001)[0]['owner_id'] == fresh['id']
    assert r.api.maximum == 1


@pytest.mark.parametrize('fault', ['crash_before_claim', 'never_claim'])
def test_standby_preclaim_faults_are_fenced(rig, fault):
    r = rig
    old = r.start()
    marker = r.home / 'preclaim-fault'
    # Native process fault before Python enters the gateway, not a replacement
    # gateway implementation. The KeepAlive respawn runs the unmodified CLI.
    (r.b / 'sitecustomize.py').write_text(
        LOCAL_NETWORK_POLICY + 'import os,signal,pathlib\n'
        f"if os.environ.get('HERMES_RELEASE_SHA') == {r.b.name!r}:\n"
        f'    marker=pathlib.Path({str(marker)!r})\n'
        '    try:\n        fd=os.open(marker,os.O_CREAT|os.O_EXCL|os.O_WRONLY,0o600)\n'
        '    except FileExistsError:\n        fd=None\n'
        '    if fd is not None:\n        os.write(fd,str(os.getpid()).encode()); os.close(fd)\n' +
        ('        os.kill(os.getpid(),getattr(signal,"SIGKILL"))\n' if fault == 'crash_before_claim' else
         '    signal.pause()\n'))
    result = r.promote()
    assert marker.exists()
    row = r.row(result['new_id'])
    fault_pid = int(marker.read_text(encoding='utf-8'))
    if fault == 'crash_before_claim':
        assert result['outcome'] == 'success', result
        assert row['pid'] != fault_pid
        assert r.supervisor.ready(row), 'respawn did not pass real gate'
        r.send(7001, 'fresh first claimant', 3)
        wait(lambda: r.seen('fresh-final'), 25, 'claimed respawn reply')
        assert r.admitted(7001)[0]['owner_id'] == row['id']
    else:
        assert result['outcome'] == 'refused', result
        assert row['pid'] is None
        assert (row['state'], row['verdict'], row['verdict_evidence']) == ('exited', 'failed', 'unclaimed')
        assert not [e for e in r.db.poller_journal() if e['generation_id'] == row['id']]
        assert r.supervisor.job(row).returncode != 0
        assert r.holder()['id'] == old['id']
        r.send(7001, 'fresh after unclaimed', 3)
        wait(lambda: r.seen('fresh-final'), 25, 'A still polling after unclaimed')
    assert r.api.maximum == 1


def test_old_sigkill_before_stop_receipt_never_promotes_standby(rig, monkeypatch):
    r = rig
    old = r.start()
    real = forward.handover_to_generation
    def kill_before_stop(home, gid, **kwargs):
        os.kill(old['pid'], SIGKILL)
        wait(lambda: not forward._live(old), 5, 'old owner death proof')
        return real(home, gid, **kwargs)
    monkeypatch.setattr(forward, 'handover_to_generation', kill_before_stop)
    result = r.promote()
    assert result['outcome'] == 'aborted', result
    standby = r.row(result['new_id'])
    assert standby['state'] == 'exited' and standby['verdict'] == 'failed'
    assert not [e for e in r.db.poller_journal() if e['generation_id'] == standby['id']]
    assert r.holder()['id'] == old['id'], 'standby acquired without a stop receipt'
    with closing(r.db.connect()) as conn:
        assert conn.execute('SELECT COUNT(*) FROM lease_moves').fetchone()[0] == 0
    assert r.api.maximum == 1


def successor_fault(r, monkeypatch, fault):
    """Prove native B polling and a reply before faulting its postcommit proof."""
    real = forward._poller
    injected = []
    def poller(db, row, supervisor, deadline):
        proof = real(db, row, supervisor, deadline)
        if row['release_sha'] == r.b.name and not injected:
            injected.append(row['id'])
            r.send(8001, 'fresh B before fault', 3)
            wait(lambda: r.seen('fresh-final'), 15, 'B replied before fault')
            assert r.admitted(8001)[0]['owner_id'] == row['id']
            fault(r.row(row['id']))
            # Observe the actual fault through the existing poller proof path.
            return real(db, row, supervisor, deadline)
        return proof
    monkeypatch.setattr(forward, '_poller', poller)


def test_live_successor_wedge_is_terminated_and_replaced(rig, monkeypatch):
    r = rig
    old = r.start()
    from hermes_cli import gateway as gateway_cli
    from gateway.shutdown_watchdog import get_loop_heartbeat_path, get_pid_loop_heartbeat_path
    stopped = []
    real_probe = gateway_cli.probe_gateway_loop_liveness
    probes = []
    def probe(pid, **kwargs):
        verdict = real_probe(pid, **kwargs)
        probes.append({'pid': pid, 'verdict': verdict, 'at': time.monotonic(),
                       'stale_after': kwargs.get('stale_after')})
        return verdict
    monkeypatch.setattr(gateway_cli, 'probe_gateway_loop_liveness', probe)
    def wedge(row):
        own = get_pid_loop_heartbeat_path(r.home, row['pid'])
        def witness():
            payload = json.loads(own.read_text(encoding='utf-8')) if own.exists() else {}
            return payload if payload.get('pid') == row['pid'] and payload.get('loop_tick_socket') is True else None
        wait(witness, 15, 'B owns armed per-PID heartbeat')
        assert real_probe(row['pid'], home=r.home) == gateway_cli.GATEWAY_LOOP_ALIVE
        started = time.monotonic()
        # SIGSTOP freezes the actual loop and all off-loop heartbeat writers.
        # No witness contents, mtimes, thresholds or strike counts are forged.
        os.kill(row['pid'], signal.SIGSTOP)
        stopped.append(row)
        shared = json.loads(get_loop_heartbeat_path(r.home).read_text(encoding='utf-8'))
        r.metrics['wedge'] = {'pid': row['pid'], 'started_clock': started,
                             'heartbeat_age_at_stop_seconds': time.time() - own.stat().st_mtime,
                             'shared_heartbeat_pid_at_stop': shared.get('pid')}
        assert real_probe(row['pid'], home=r.home) == gateway_cli.GATEWAY_LOOP_UNKNOWN
        raise RuntimeError('native SIGSTOP after B served and replied')
    successor_fault(r, monkeypatch, wedge)
    try:
        result = r.promote()
        b = r.row(result['new_id'])
        path = 'in_budget'
        if result['outcome'] == 'blocked':
            # A wedge that starts too late to age into proof inside the 60 s bound is the
            # designed blocked case. The guardian's recovery must still replace it.
            path = 'guardian_late'
            assert 'neither handed over nor proved wedged' in result['failure'] and result['alert'], result
            assert r.holder()['id'] == b['id'] and forward._live(b)
            r.metrics['wedge']['blocked_seconds'] = time.monotonic() - r.metrics['wedge']['started_clock']
            wait(lambda: real_probe(b['pid'], home=r.home, stale_after=forward.WEDGE_STALE_SECONDS)
                 == gateway_cli.GATEWAY_LOOP_WEDGED, 60, 'wedge provable from B heartbeat')
            result = forward.recover_forward(r.home, supervisor=r.supervisor)
            assert result['late_rollback']['bound_missed'] is True and result['alert'], result
        assert result['outcome'] == 'rolled_back', result
        fresh = r.holder()
        assert fresh['release_sha'] == r.a.name and fresh['id'] != old['id']
        r.send(8004, 'fresh after wedge', 3)
        wait(lambda: len(r.seen('fresh-final')) == 2, 30, 'A-prime reply after wedge')
        replied = time.monotonic() - r.metrics['wedge']['started_clock']
        assert r.db._owner_is_dead(b) and r.row(b['id'])['verdict'] == 'failed'
        assert any(p['pid'] == b['pid'] and p['verdict'] == gateway_cli.GATEWAY_LOOP_WEDGED for p in probes), probes
        assert r.admitted(8004)[0]['owner_id'] == fresh['id']
        assert (r.home / 'current').resolve() == r.a
        if path == 'in_budget':
            assert result['rollback']['commit_to_serving_upper_bound_seconds'] <= 60, result['rollback']
        assert r.api.maximum == 1
        wait(lambda: r.api.confirmed >= 8004, 10, 'wedge cursor')
        r.poller_proof([8001, 8004])
        with closing(r.db.connect()) as conn:
            assert conn.execute('SELECT COUNT(*) FROM lease_moves').fetchone()[0] == 2
        r.metrics['wedge'].update(
            path=path, probes=probes, wedge_to_A_prime_reply_seconds=replied,
            commit_to_serving_seconds=result['rollback'].get('commit_to_serving_upper_bound_seconds'),
            rollback_bound_met=result['rollback'].get('rollback_bound_met'))
    finally:
        # Resume only our injected stop if B somehow survived, so cleanup can boot it out.
        for row in stopped:
            if forward._live(row):
                os.kill(row['pid'], signal.SIGCONT)


def test_live_successor_refuses_handover_then_late_death_recovers(rig, monkeypatch):
    r = rig
    r.start()
    marker = r.home / 'refuse-handover'
    receipt = r.home / 'handover-refused'
    # A minimal release-local fault: native loop, socket, transport and handler
    # remain real. Only health reporting and cooperative handover are faulted.
    (r.b / 'sitecustomize.py').write_text(LOCAL_NETWORK_POLICY +
        'from pathlib import Path\n'
        'from gateway.run_generation import ActiveGeneration\n'
        f'marker=Path({str(marker)!r})\nreceipt=Path({str(receipt)!r})\n'
        'original_status=ActiveGeneration.polling_status\n'
        'original_transfer=ActiveGeneration.transfer_requested\n'
        'def status(self):\n'
        '    result=original_status(self)\n'
        '    if marker.exists(): result["healthy"]=False\n'
        '    return result\n'
        'async def transfer(self,new_id,**kwargs):\n'
        '    if marker.exists():\n'
        '        receipt.write_text(self.identity.id)\n'
        '        raise RuntimeError("native live loop refuses handover")\n'
        '    return await original_transfer(self,new_id,**kwargs)\n'
        'ActiveGeneration.polling_status=status\n'
        'ActiveGeneration.transfer_requested=transfer\n', encoding='utf-8')
    signals = []
    real_kill = os.kill
    def traced_kill(pid, sig):
        if sig:
            signals.append((pid, int(sig)))
        return real_kill(pid, sig)
    monkeypatch.setattr(os, 'kill', traced_kill)
    from hermes_cli import gateway as gateway_cli
    def no_escalation(*args, **kwargs):
        raise AssertionError('alive refusing successor must never be signalled')
    monkeypatch.setattr(gateway_cli, '_escalate_wedged_gateway', no_escalation)
    successor_fault(r, monkeypatch, lambda row: marker.touch())
    result = r.promote()
    b = r.row(result['new_id'])
    assert result['outcome'] == 'blocked' and result['alert'], result
    assert 'neither handed over nor proved wedged' in result['failure']
    assert receipt.read_text(encoding='utf-8') == b['id']
    assert r.holder()['id'] == b['id'] and forward._live(b)
    assert gateway_cli.probe_gateway_loop_liveness(b['pid'], home=r.home) == gateway_cli.GATEWAY_LOOP_ALIVE
    assert not [s for s in signals if s[0] == b['pid']]
    r.send(8002, 'fresh while blocked', 4)
    wait(lambda: len(r.seen('fresh-final')) == 2, 20, 'blocked B still replies')
    assert r.admitted(8002)[0]['owner_id'] == b['id']
    wait(lambda: r.api.confirmed >= 8002, 10, 'blocked cursor confirmed')
    r.poller_proof([8001, 8002])
    with closing(r.db.connect()) as conn:
        assert conn.execute('SELECT COUNT(*) FROM lease_moves').fetchone()[0] == 1
    # Crossing the original deadline is observed from the real clock, not a
    # mocked clock or a sleep standing in for liveness/death/polling evidence.
    wait(lambda: time.monotonic() > result['commit_clock'] + 60, 60, 'original rollback budget expired')
    assert not [s for s in signals if s[0] == b['pid']]
    died = time.monotonic()
    os.kill(b['pid'], SIGKILL)
    wait(lambda: parked(r.supervisor, b) and r.db._owner_is_dead(b), 15, 'late B death and parked respawn')
    with ThreadPoolExecutor(max_workers=1) as pool:
        task = pool.submit(forward.recover_forward, r.home, supervisor=r.supervisor)
        fresh = wait(lambda: next((row for row in r.db.generations()
            if row['release_sha'] == r.a.name and row['state'] == 'serving'
            and row['id'] != result['old_id'] and r.supervisor.ready(row)
            and any(e['generation_id'] == row['id'] and e['event'] == 'poller_started'
                    for e in r.db.poller_journal())), None), 45, 'late A-prime serving')
        started = [e['monotonic_at'] for e in r.db.poller_journal()
                   if e['generation_id'] == fresh['id'] and e['event'] == 'poller_started']
        death_to_poller = max(started) - died
        assert 0 <= death_to_poller <= MAX_HANDOVER_GAP_SECONDS
        r.send(8003, 'fresh after late death', 3)
        wait(lambda: len(r.seen('fresh-final')) == 3, max(.1, 60 - (time.monotonic() - died)), 'late A-prime reply')
        replied = time.monotonic() - died
        recovery = task.result(timeout=max(.1, 60 - (time.monotonic() - died)))
    assert recovery['outcome'] == 'rolled_back', recovery
    assert recovery['late_rollback']['bound_missed'] is True
    assert recovery['rollback']['rollback_bound_met'] is False
    assert recovery['alert'] is True
    assert r.admitted(8003)[0]['owner_id'] == fresh['id']
    assert (r.home / 'current').resolve() == r.a
    assert r.row(b['id'])['verdict'] == 'failed'
    assert replied <= 60
    wait(lambda: r.api.confirmed >= 8003, 10, 'late recovery cursor')
    r.poller_proof([8001, 8002, 8003])
    ended = [e for e in r.db.poller_journal() if e['generation_id'] == b['id']
             and e['event'] in {'poller_stopped', 'lock_released'}]
    assert [e['event'] for e in ended] == ['poller_stopped', 'lock_released']
    assert all(e['monotonic_at'] >= died for e in ended)
    with closing(r.db.connect()) as conn:
        assert conn.execute('SELECT COUNT(*) FROM lease_moves').fetchone()[0] == 2
    r.metrics.update(late_recovery=recovery, late_death_to_reply_seconds=replied,
                     late_death_to_poller_seconds=death_to_poller,
                     successor_signals_before_death=[], signals=signals)


def test_old_draining_sigkill_interrupts_once_without_replay(rig):
    r = rig
    old = r.start()
    r.send(9001, 'inflight-old', 1)
    wait(lambda: (r.home / 'inflight.start').exists(), 30, 'old in-process tool executing')
    result = r.promote()
    assert result['outcome'] == 'success', result
    b = r.row(result['new_id'])
    assert r.row(old['id'])['state'] == 'draining' and forward._live(old)
    assert not (r.home / 'inflight.done').exists()
    r.send(9002, '/queue followup-old', 1)
    wait(lambda: r.admitted(9002) and r.admitted(9002)[0]['state'] == 'accepted',
         15, 'queued command accepted by draining A')
    assert r.admitted(9002)[0]['owner_id'] == old['id']
    assert r.admitted(9002)[0]['state'] == 'accepted'
    with closing(r.db.connect()) as conn, conn:
        session = dict(conn.execute('SELECT * FROM sessions WHERE generation_id=?', (old['id'],)).fetchone())
        assert session['state'] == 'owned' and session['outstanding_work'] > 0
        conn.executescript('''
            CREATE TABLE interruption_audit(kind TEXT, item TEXT, owner TEXT, at REAL);
            CREATE TRIGGER audit_session_interruption AFTER UPDATE OF state ON sessions
            WHEN NEW.state='interrupted' AND OLD.state!='interrupted'
            BEGIN INSERT INTO interruption_audit VALUES('session',OLD.session_key,OLD.generation_id,
                julianday('now')); END;
            CREATE TRIGGER audit_inbox_interruption AFTER UPDATE OF state ON inbox
            WHEN NEW.state='interrupted' AND OLD.state!='interrupted'
            BEGIN INSERT INTO interruption_audit VALUES('inbox',OLD.source_event_id,OLD.owner_id,
                julianday('now')); END;
        ''')
    assert not r.seen('inflight-final') and not r.seen('followup-final')
    died = time.monotonic()
    os.kill(old['pid'], SIGKILL)
    wait(lambda: parked(r.supervisor, old) and r.db._owner_is_dead(old), 15, 'draining A death proof')
    def interrupted():
        with closing(r.db.connect()) as conn:
            return conn.execute('SELECT state FROM sessions WHERE session_key=?',
                                (session['session_key'],)).fetchone()[0] == 'interrupted'
    wait(interrupted, 15, 'native B fences dead A in-process work')
    retired = r.row(old['id'])
    assert (retired['state'], retired['verdict'], retired['verdict_evidence']) == ('exited', 'failed', 'admission_owner_dead')
    def audit():
        with closing(r.db.connect()) as conn:
            return [tuple(row) for row in conn.execute(
                'SELECT kind,item,owner FROM interruption_audit ORDER BY kind')]
    expected = [('session', session['session_key'], old['id'])]
    assert audit() == expected
    assert r.db.hold_dead_owner(old['id']) == 0
    assert r.db.hold_dead_owner(old['id']) == 0
    assert audit() == expected
    assert r.row(old['id'])['verdict_at'] == retired['verdict_at']
    r.send(9003, 'fresh B after old death', 3)
    wait(lambda: r.seen('fresh-final'), 20, 'B unaffected by draining death')
    replied = time.monotonic() - died
    assert r.admitted(9003)[0]['owner_id'] == b['id']
    assert r.holder()['id'] == b['id'] and forward._live(b)
    r.send(9004, 'fresh recovery of interrupted session', 1)
    wait(lambda: len(r.seen('fresh-final')) == 2, 20, 'fresh input recovers interrupted session on B')
    assert r.admitted(9004)[0]['owner_id'] == b['id']
    assert not r.seen('inflight-final') and not r.seen('followup-final')
    # Both admissions stay pinned to A. B never invokes the interrupted tool.
    assert r.admitted(9001)[0]['owner_id'] == old['id']
    assert r.admitted(9002)[0]['state'] == 'accepted'
    tool_requests = [request for request in r.llm.requests
                     if request['kind'] == 'main' and request.get('response') == 'ToolCall'
                     and 'inflight-old' in str(next((message.get('content')
                         for message in reversed(request['body']['messages'])
                         if message.get('role') == 'user'), ''))]
    assert len(tool_requests) == 1
    assert audit() == expected
    wait(lambda: r.api.confirmed >= 9004, 10, 'draining death cursor')
    r.poller_proof([9001, 9002, 9003, 9004])
    forward.cleanup_exited(r.home, supervisor=r.supervisor)
    assert r.supervisor.job(old).returncode != 0
    r.metrics.update(old_death_to_B_reply_seconds=replied, interruption_audit=expected)
