"""Upgrade claimed-identity constraints without dropping rows or dependent objects."""
import re


def upgrade_claim_schema(conn):
    conn.execute('DROP TRIGGER IF EXISTS generation_unique_label')
    conn.execute('DROP TRIGGER IF EXISTS generation_claim_immutable')
    columns = list(conn.execute('PRAGMA table_info(generations)'))
    if not any(row['name'] in {'pid', 'start_fingerprint'} and row['notnull'] for row in columns):
        return
    objects = conn.execute("SELECT type,name,sql FROM sqlite_master WHERE sql IS NOT NULL AND "
                           "((tbl_name='generations' AND type='index') OR type='trigger')").fetchall()
    definition = conn.execute("SELECT sql FROM sqlite_master WHERE name='generations'").fetchone()[0]
    definition = re.sub(r'CREATE TABLE (?:IF NOT EXISTS )?generations',
                        'CREATE TABLE generations_upgrade', definition, count=1)
    definition = re.sub(r'(pid INTEGER|start_fingerprint TEXT) NOT NULL', r'\1', definition)
    # SQLite validates dependent triggers during RENAME. Remove and restore them
    # inside the same transaction, while no competing writer can observe a gap.
    for obj in objects:
        if obj['type'] == 'trigger':
            conn.execute('DROP TRIGGER "' + obj['name'].replace('"', '""') + '"')
    conn.execute(definition)
    names = ','.join(row['name'] for row in columns)
    values = ','.join("NULLIF(pid,0)" if row['name'] == 'pid' else
                      "CASE WHEN pid=0 THEN NULL ELSE start_fingerprint END" if row['name'] == 'start_fingerprint'
                      else row['name'] for row in columns)
    conn.execute(f'INSERT INTO generations_upgrade ({names}) SELECT {values} FROM generations')
    conn.execute('DROP TABLE generations')
    conn.execute('ALTER TABLE generations_upgrade RENAME TO generations')
    for obj in objects:
        conn.execute(obj['sql'])
    if conn.execute('PRAGMA foreign_key_check').fetchone():
        raise RuntimeError('generation claim migration broke a foreign key')


# Keep the N-1 schema version untouched. The optional live-label index belongs
# to forward-only writes, so creating it must not invalidate the open receipt.
_LAYOUT_KEY = 'forward_layout_v1'
_CORE_TABLES = ('schema_meta', 'generations', 'leases', 'sessions', 'inbox',
                'generation_transfers', 'transfer_tokens', 'poller_journal', 'lease_moves')


def _layout_receipt(conn, boot_id):
    import hashlib
    import json
    placeholders = ','.join('?' for _ in _CORE_TABLES)
    objects = [tuple(row) for row in conn.execute(
        f'SELECT type,name,sql FROM sqlite_master WHERE tbl_name IN ({placeholders}) '
        "AND name<>'generations_live_label' ORDER BY type,name", _CORE_TABLES)]
    return hashlib.sha256(json.dumps([boot_id, objects]).encode()).hexdigest()


def layout_is_current(conn, boot_id):
    if not conn.execute("SELECT 1 FROM sqlite_master WHERE name='schema_meta' AND type='table'").fetchone():
        return False
    row = conn.execute('SELECT value FROM schema_meta WHERE key=?', (_LAYOUT_KEY,)).fetchone()
    return row is not None and row['value'] == _layout_receipt(conn, boot_id)


def record_current_layout(conn, boot_id):
    conn.execute('INSERT OR REPLACE INTO schema_meta(key,value) VALUES(?,?)',
                 (_LAYOUT_KEY, _layout_receipt(conn, boot_id)))
