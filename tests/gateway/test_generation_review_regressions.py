"""Review #274 regressions: profile claims and observational coordinator opens."""
import os
from dataclasses import replace

from gateway.generation import GenerationCoordinator, GenerationIdentity


def test_named_profile_first_claim_uses_own_label(tmp_path, monkeypatch):
    from hermes_cli.gateway_launchd import get_launchd_label
    from gateway.run_generation import claim_active_generation
    home = tmp_path / 'profiles' / 'research'
    home.mkdir(parents=True)
    monkeypatch.setenv('HERMES_HOME', str(home))
    monkeypatch.setenv('HERMES_LAUNCHD_LABEL', 'untrusted-label')
    (home / 'config.yaml').write_text('gateway:\n  forward_only_handover:\n    enabled: true\n', encoding='utf-8')
    monkeypatch.setattr('gateway.status._get_process_start_time', lambda pid: 123)
    monkeypatch.setattr('gateway.generation._boot_id', lambda: 'boot')
    expected = get_launchd_label()
    db, identity = claim_active_generation()
    assert identity.label == expected
    assert db.service_label() == expected
    assert len(db.generations()) == 1


def test_current_coordinator_open_is_read_only_and_retains_history(tmp_path, monkeypatch):
    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    (tmp_path / 'config.yaml').write_text('gateway:\n  forward_only_handover:\n    enabled: true\n', encoding='utf-8')
    monkeypatch.setattr('gateway.generation._boot_id', lambda: 'boot')
    db = GenerationCoordinator(tmp_path)
    old = GenerationIdentity.create(release_sha='r', label='old', boot_id='prior', pid=os.getpid())
    db.register(replace(old, started_at=1), state='exited')
    with db.connect() as conn:
        conn.execute('UPDATE generations SET heartbeat_at=1')
        schema = conn.execute('PRAGMA schema_version').fetchone()[0]
    statements = []
    original = GenerationCoordinator.connect
    def traced(self):
        conn = original(self)
        conn.set_trace_callback(statements.append)
        return conn
    monkeypatch.setattr(GenerationCoordinator, 'connect', traced)
    reader = GenerationCoordinator(tmp_path)
    assert [row['id'] for row in reader.generations()] == [old.id]
    with reader.connect() as conn:
        assert conn.execute('PRAGMA schema_version').fetchone()[0] == schema
    assert not any(sql.lstrip().upper().startswith(('BEGIN IMMEDIATE', 'CREATE ', 'DROP ', 'DELETE ', 'UPDATE ', 'INSERT '))
                   for sql in statements)
    assert reader.prune_history()['generations'] == 1
