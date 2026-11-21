"""A managed worker's agent gets a RuntimeSessionStore as its session_db. Every store method the agent core calls must exist on it, and its tail reads (the failed-turn boundary's tail role and a row's role in its own transcript) answer like the owner's store."""
import ast
from pathlib import Path
import re

import pytest

from hermes_agent.compat.paths import current_path
from agent.runtime_session_store import RuntimeSessionStore
from persistence_assembly import open_persistence, profile_scope
from tests.fixtures.storage_read import transcript_rows
<<<<<<< HEAD
from hermes_state_runtime import mutate_worker_execution, register_worker_execution
||||||| dff103e05878
from hermes_state_runtime import begin_runtime_epoch, mutate_worker_execution, register_worker_execution
=======
from tests.fixtures.storage_seed import seed_messages, seed_session
>>>>>>> 32dbc1dcda2255fd35969ec99cfc0170988c3eca
from tests.fakes.runtime_owner import begin_runtime_epoch

ROOT = Path(__file__).resolve().parents[2]
# The agent core: everything a worker's AIAgent runs (tools open their own handles, or refuse).
CORE = [ROOT / current_path('run_agent.py'), *sorted((ROOT / current_path('agent')).glob('*.py'))]


def _core_session_db_calls():
    """Names called on the agent's store: ``x._session_db`` / ``x.db`` / ``session_db``, and a local
    ``db`` where the module binds it from ``_session_db`` (a module opening its own SessionDB is not)."""
    names = set()
    for path in CORE:
        text = path.read_text(encoding='utf-8')
        local_db = re.search(r'\bdb\b[^=\n]*=\s*getattr\([^)\n]*_session_db', text) is not None
        for node in ast.walk(ast.parse(text)):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                owner = node.func.value
                if ((isinstance(owner, ast.Attribute) and owner.attr in ('_session_db', 'db'))
                        or (isinstance(owner, ast.Name) and (owner.id == 'session_db' or (owner.id == 'db' and local_db)))):
                    names.add(node.func.attr)
    return sorted(names)


@pytest.mark.parametrize('name', _core_session_db_calls())
def test_store_offers_every_core_session_db_call(name):
    assert hasattr(RuntimeSessionStore, name), f'RuntimeSessionStore lacks {name}'


def test_tail_reads_answer_like_the_owner(tmp_path):
    db = open_persistence(profile_scope(home=tmp_path))
    try:
        seed_session(db, 's', 'cli')
        seed_messages(db, 's', {'role': 'user', 'content': 'q'}, {'role': 'assistant', 'content': 'a'},
                      {'role': 'user', 'content': 'failed turn'})
        seed_session(db, 'other', 'cli')
        seed_messages(db, 'other', {'role': 'assistant', 'content': 'x'})
        db.set_session_title('s', 'Straße')
        db.set_session_title('other', 'Another Subject')
        rows = [r['id'] for session_id in ('s', 'other') for r in transcript_rows(db, session_id)]
        epoch = db.runtime.begin_epoch(instance_id='fixture')
        scope = dict(epoch=epoch, execution_id='w', session_id='s', generation=0)
        db.worker_executions.register(**scope, kind='compute', adoption_secret='secret')
        store = RuntimeSessionStore(lambda method, **p: db.worker_executions.persist_operation(**p), scope,
                                    tmp_path / 'outbox')
        try:
            from agent.runtime_session_store import WorkerPersistenceError
            for title in ('STRASSE', 'Another Subject', 'No Match', ''):
                for exclude in (None, 's'):
                    assert store.has_auto_title_collision(title, exclude_session_id=exclude) == (
                        db.has_auto_title_collision(title, exclude_session_id=exclude))
            with pytest.raises(WorkerPersistenceError, match='permission_denied'):
                store.has_auto_title_collision('Another Subject', exclude_session_id='other')
            assert store.latest_conversation_role('s') == db.messages.latest_conversation_role('s') == 'user'
            for row_id in [*rows, 10**6]:  # another transcript's row is not this session's
                assert store.get_message_role('s', row_id) == db.messages.message_role('s', row_id)
        finally:
            store.close()
    finally:
        db.close()
