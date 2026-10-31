"""Epoch fencing must not depend on pytest's import-time isolation markers."""
import os

import pytest

from hermes_state import SessionDB
from hermes_state_runtime import RuntimeStoreError, begin_runtime_epoch


@pytest.mark.parametrize('existing', [False, True])
def test_unreserved_custom_home_cannot_advance_epoch(tmp_path, monkeypatch, existing):
    home = tmp_path / 'custom-home'
    home.mkdir()
    monkeypatch.setenv('HERMES_HOME', str(home))
    with SessionDB(home / 'state.db') as db:
        if existing:
            db._execute_write(lambda conn: conn.execute(
                "INSERT INTO runtime_epoch(singleton,epoch,instance_id) VALUES(1,35,'gateway')"))
        before = [tuple(row) for row in db._conn.execute('SELECT * FROM runtime_epoch')]
        refusal = None
        with monkeypatch.context() as unmarked:
            for name in tuple(os.environ):
                if name.startswith('PYTEST_') or name == 'HERMES_TEST_ISOLATION':
                    unmarked.delenv(name)
            try:
                begin_runtime_epoch(db, instance_id='ad-hoc-probe')
            except RuntimeStoreError as exc:
                refusal = exc.reason
        after = [tuple(row) for row in db._conn.execute('SELECT * FROM runtime_epoch')]
        assert (refusal, after) == ('not_owner', before)
