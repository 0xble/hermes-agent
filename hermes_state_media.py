"""Durable media retirement candidates survive transcript deletion and failed cleanup."""
import hashlib
import json
import logging

from hermes_state_keys import prefix_range
from hermes_state_prune_budget import check_prune_budget, prune_sql

PREFIX = 'gateway.media.retired.v1.'
SPILLOVER_PREFIX = 'gateway.spillover.retired.v1.'


def collect_retired_media_bounded(db):
    """Spend at most 2s of the remaining pass on durable media candidates."""
    from hermes_state_prune_budget import PruneBudget, PruneBudgetExpired, current_budget, prune_budget
    parent = current_budget()
    parent.check()
    budget = PruneBudget(max_seconds=2.0)
    budget.deadline = min(budget.deadline, parent.deadline)
    try:
        with prune_budget(budget):
            collect_retired_media(db)
    except PruneBudgetExpired:
        logging.getLogger(__name__).info("Session prune media collection deferred at its budget")


def _retired_candidates(db, prefix):
    with db._read_ctx() as conn, prune_sql(conn):
        cursor = conn.execute('SELECT key,value FROM state_meta WHERE key>=? AND key<?', prefix_range(prefix))
        rows = []
        for row in cursor:
            check_prune_budget()
            rows.append(row)
        return rows


def retire_media(conn, payload, request_id=''):
    from gateway.session_ingress_media import admission_media_references, hosted_document_references
    references = [*admission_media_references(payload), *hosted_document_references(request_id, payload),
                  *payload.get('api_turn_v1', {}).get('media', [])]
    for reference in references:
        key = PREFIX + hashlib.sha256(reference['path'].encode()).hexdigest()
        conn.execute('INSERT OR IGNORE INTO state_meta(key,value) VALUES(?,?)',
                     (key, json.dumps(reference)))


def retire_spillover(conn, session_ids, *, message_ids=None):
    """Record the spillover archives the doomed sessions' messages point at, inside the delete
    transaction (the pointers vanish with the rows), as durable candidates (#126351)."""
    from hermes_state_common import _id_chunks, _placeholders
    from tools.tool_result_storage import SPILLOVER_SQL_FILTER, spillover_names_in_text
    for chunk in _id_chunks(list(session_ids)):
        page = f" AND id IN ({_placeholders(message_ids)})" if message_ids is not None else ""
        for content, api_content in conn.execute(
                f"SELECT content, api_content FROM messages WHERE session_id IN ({_placeholders(chunk)})"
                f" AND {SPILLOVER_SQL_FILTER}{page}", [*chunk, *(message_ids or ())]).fetchall():
            conn.executemany('INSERT OR IGNORE INTO state_meta(key,value) VALUES(?,?)',
                             [(SPILLOVER_PREFIX + n, json.dumps(n)) for n in spillover_names_in_text(content, api_content)])


def collect_retired_spillover(db):
    """Unlink retired archives no surviving message references (a branch/compression sibling sharing
    the pointer keeps it until it dies too). Unknown reference set: keep files and candidates."""
    from tools.tool_result_storage import referenced_spillover_names
    rows = _retired_candidates(db, SPILLOVER_PREFIX)
    spill_dir = db.db_path.parent / 'cache' / 'spillover'
    if not rows or (referenced := referenced_spillover_names(spill_dir)) is None:
        return
    done = []
    for key, raw in rows:
        check_prune_budget()
        try:
            if (name := json.loads(raw)) not in referenced:
                (spill_dir / name).unlink(missing_ok=True)
        except OSError:
            continue  # stays a candidate for the next delete/prune pass
        done.append((key,))
    db._execute_write(lambda conn: conn.executemany('DELETE FROM state_meta WHERE key=?', done))


def collect_retired_media(db):
    """Release retired candidates no admission or retained transcript row owns; drop collected keys.
    Synchronous SQLite plus one messages pass: async callers must run it off the event loop."""
    from gateway.session_ingress_media import release_unheld_media
    collect_retired_spillover(db)
    rows = _retired_candidates(db, PREFIX)
    if rows:
        release_unheld_media(db, [json.loads(row['value']) for row in rows])
        from pathlib import Path
        gone = []
        for row in rows:
            check_prune_budget()
            if not Path(json.loads(row['value'])['path']).exists():
                gone.append((row['key'],))
        if gone:
            db._execute_write(lambda conn: conn.executemany('DELETE FROM state_meta WHERE key=?', gone))
