"""Single-use supervisor scopes and parked service recovery under the write fence."""
from contextlib import closing
import time


from gateway.deadline import begin_immediate


class GenerationClaimsMixin:
    def _ensure_live_label_index(self, conn):
        """Enable the forward-only fence under the caller's BEGIN IMMEDIATE.

        Legacy overlap may leave crashed same-label rows. Preserve the lease's
        named row and retire only proven-dead duplicates. An unsafe duplicate
        aborts the whole write, including any earlier dead-row retirements.
        """
        from gateway.generation import _boot_id, _is_unclaimed
        if conn.execute("SELECT 1 FROM sqlite_master WHERE type='index' "
                        "AND name='generations_live_label'").fetchone():
            return
        lease = conn.execute("SELECT generation_id FROM leases WHERE resource='active_generation'").fetchone()
        labels = conn.execute("SELECT label FROM generations WHERE state<>'exited' "
                              "GROUP BY label HAVING COUNT(*)>1").fetchall()
        for label in labels:
            rows = conn.execute("SELECT * FROM generations WHERE label=? AND state<>'exited' "
                                "ORDER BY started_at,id", (label['label'],)).fetchall()
            for row in rows:
                if lease and row['id'] == lease['generation_id']:
                    continue
                if (_is_unclaimed(row) or not row['start_fingerprint'] or not self._owner_is_dead(row)):
                    raise RuntimeError(f"cannot enable forward-only label uniqueness for {label['label']}: "
                                       "duplicate is alive or identity unknown")
                if not self._retire_in_transaction(conn, row['id'], expected_pid=row['pid'],
                        expected_start_fingerprint=row['start_fingerprint'],
                        evidence='boot_changed' if row['boot_id'] != _boot_id() else 'dead'):
                    raise RuntimeError(f"duplicate generation retirement failed for {label['label']}")
        conn.execute("CREATE UNIQUE INDEX generations_live_label ON generations(label) WHERE state <> 'exited'")

    def _service_label(self, conn):
        row = conn.execute("SELECT g.label FROM leases l JOIN generations g ON g.id=l.generation_id "
                           "WHERE l.resource='active_generation'").fetchone()
        if row:
            return row['label']
        from hermes_cli.gateway import get_launchd_label
        from hermes_constants import set_hermes_home_override, reset_hermes_home_override
        token = set_hermes_home_override(self.home)
        try:
            return get_launchd_label()
        finally:
            reset_hermes_home_override(token)

    def claim_process(self, process, scope_nonce):
        from gateway.generation import GenerationIdentity, _is_unclaimed
        with closing(self.connect()) as conn, conn:
            begin_immediate(conn)
            self._ensure_live_label_index(conn)
            if conn.execute('SELECT 1 FROM generations WHERE label=? AND boot_id=? AND scope_nonce=?',
                            (process.label, process.boot_id, scope_nonce)).fetchone():
                return None
            row = conn.execute("SELECT * FROM generations WHERE label=? AND state<>'exited'",
                               (process.label,)).fetchone()
            service = process.label == self._service_label(conn)
            if row and _is_unclaimed(row):
                if row['release_sha'] != process.release_sha:
                    raise RuntimeError('reserved generation release differs')
                generation_id = row['id']
            else:
                if row:
                    if row['boot_id'] == process.boot_id and not self._owner_is_dead(row):
                        return None
                    self._retire_in_transaction(conn, row['id'], expected_pid=row['pid'],
                        expected_start_fingerprint=row['start_fingerprint'],
                        evidence='boot_changed' if row['boot_id'] != process.boot_id else 'dead')
                if not service:
                    conn.commit()  # A dead non-service claimant is retired, never replaced.
                    return None
                identity = GenerationIdentity.create(release_sha=process.release_sha, label=process.label,
                                                      boot_id=process.boot_id)
                self._reserve_in_transaction(conn, identity)
                generation_id = identity.id
            if not self._claim_in_transaction(conn, generation_id, process.pid, process.start_fingerprint,
                                               process.boot_id, scope_nonce, time.time()):
                conn.commit()
                return None
            result = conn.execute('SELECT * FROM generations WHERE id=?', (generation_id,)).fetchone()
            conn.commit()
            return GenerationIdentity(**{key: result[key] for key in GenerationIdentity.__dataclass_fields__})

    def service_label(self):
        with closing(self.connect()) as conn:
            return self._service_label(conn)

    def prepare_parked_repair(self, label, *, retire=True):
        """Retire only the parked service's dead claimant, leaving its lease for takeover."""
        from gateway.generation import _boot_id, _is_unclaimed
        with closing(self.connect()) as conn, conn:
            begin_immediate(conn)
            rows = conn.execute('SELECT * FROM generations WHERE label=?', (label,)).fetchall()
            if label != self._service_label(conn):
                return 'cleanup' if rows and all(row['state'] == 'exited' for row in rows) else 'waiting'
            serving = conn.execute("SELECT * FROM generations WHERE state='serving'").fetchall()
            if any(row['label'] != label for row in serving):
                return 'waiting'
            lease = conn.execute("SELECT g.*,l.state AS lease_state FROM leases l JOIN generations g "
                                 "ON g.id=l.generation_id WHERE l.resource='active_generation'").fetchone()
            live = [row for row in rows if row['state'] != 'exited']
            claimants = {row['id']: row for row in live}
            if lease and lease['lease_state'] == 'active':
                claimants[lease['id']] = lease
            for row in claimants.values():
                if _is_unclaimed(row) or not self._owner_is_dead(row):
                    return 'waiting'
            for row in claimants.values():
                if retire and row['state'] != 'exited':
                    self._retire_in_transaction(conn, row['id'], expected_pid=row['pid'],
                        expected_start_fingerprint=row['start_fingerprint'],
                        evidence='boot_changed' if row['boot_id'] != _boot_id() else 'dead')
            conn.commit()
            return 'repair'
