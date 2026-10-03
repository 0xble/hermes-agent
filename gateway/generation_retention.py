"""Conservative, bounded collection of unreferenced history from earlier boots."""
from contextlib import closing
import time

from gateway.deadline import begin_immediate

RETENTION_SECONDS = 7 * 86400

# Retain every row current claim rules can read, and every authority reference.
_PROTECTED = """OLD.state <> 'exited' OR OLD.boot_id={boot}
 OR OLD.heartbeat_at >= CAST(strftime('%s','now') AS REAL)-604800
 OR EXISTS(SELECT 1 FROM leases WHERE generation_id=OLD.id)
 OR EXISTS(SELECT 1 FROM sessions WHERE generation_id=OLD.id)
 OR EXISTS(SELECT 1 FROM inbox WHERE owner_id=OLD.id)
 OR EXISTS(SELECT 1 FROM generation_transfers WHERE old_id=OLD.id OR new_id=OLD.id)
 OR EXISTS(SELECT 1 FROM lease_moves WHERE old_id=OLD.id OR new_id=OLD.id)
 OR EXISTS(SELECT 1 FROM poller_journal WHERE generation_id=OLD.id)"""


def install_retention_fences(conn, boot_id):
    boot = "'" + boot_id.replace("'", "''") + "'" if boot_id is not None else 'NULL'
    conn.execute('DROP TRIGGER IF EXISTS generation_retain_identity')
    conn.execute('CREATE TRIGGER generation_retain_identity BEFORE DELETE ON generations WHEN '
                 + ('1' if boot_id is None else _PROTECTED.format(boot=boot)) + ' BEGIN SELECT RAISE(IGNORE); END')
    conn.execute('DROP TRIGGER IF EXISTS poller_journal_no_delete')
    conn.execute("CREATE TRIGGER poller_journal_no_delete BEFORE DELETE ON poller_journal "
                 f"WHEN {1 if boot_id is None else 0} OR OLD.boot_id IS NULL OR OLD.boot_id={boot} OR "
                 "OLD.wall_at >= CAST(strftime('%s','now') AS REAL)-604800 OR "
                 "NOT EXISTS(SELECT 1 FROM generations WHERE id=OLD.generation_id AND state='exited') "
                 "BEGIN SELECT RAISE(ABORT,'authority journal is append-only'); END")


class GenerationRetentionMixin:
    def prune_history(self, *, limit=100):
        """Keep this boot forever, plus seven days and all referenced identities.

        Only complete poller intervals from an exited prior-boot generation may
        be collected. Large or incomplete journals are conservatively retained.
        N-1 deletes pass through the same retention fence.
        """
        from gateway.generation import _boot_id, check_poller_journal
        if not 1 <= limit <= 1000:
            raise ValueError('history pruning limit must be between 1 and 1000')
        try:
            boot = _boot_id()
        except OSError:
            # Missing boot identity forbids collection, not N-1 read/write access.
            return {'generations': 0, 'journal': 0}
        cutoff = time.time() - RETENTION_SECONDS
        with closing(self.connect()) as conn, conn:
            begin_immediate(conn)
            candidates = conn.execute("SELECT j.generation_id FROM poller_journal j JOIN generations g "
                "ON g.id=j.generation_id WHERE g.state='exited' GROUP BY j.generation_id "
                "HAVING MAX(j.wall_at)<? AND COUNT(*)<=? AND "
                "SUM(CASE WHEN j.boot_id IS NULL OR j.boot_id=? THEN 1 ELSE 0 END)=0 "
                "ORDER BY MAX(j.wall_at) LIMIT ?", (cutoff, limit, boot, limit)).fetchall()
            journal = 0
            for candidate in candidates:
                rows = conn.execute('SELECT * FROM poller_journal WHERE generation_id=? ORDER BY id',
                                    (candidate['generation_id'],)).fetchall()
                if journal + len(rows) <= limit and check_poller_journal(rows)['ok']:
                    journal += conn.execute('DELETE FROM poller_journal WHERE generation_id=?',
                                            (candidate['generation_id'],)).rowcount
            eligible = _PROTECTED.format(boot='?').replace('OLD.', 'g.')
            generations = conn.execute('DELETE FROM generations WHERE id IN '
                '(SELECT g.id FROM generations g WHERE NOT (' + eligible + ') '
                'ORDER BY g.heartbeat_at LIMIT ?)', (boot, limit)).rowcount
            conn.commit()
            return {'generations': generations, 'journal': journal}
