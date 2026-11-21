"""SessionDB permission tightening tolerates SQLite's last-holder cleanup."""

import errno
import os
import stat

import pytest


@pytest.mark.platforms("posix")
@pytest.mark.parametrize("suffix", ["-wal", "-shm"])
def test_startup_survives_sidecar_removed_after_lstat(tmp_path, monkeypatch, suffix):
    import hermes_state as hs

    db = tmp_path / "state.db"
    damaged = bytes(4096)
    db.write_bytes(damaged)
    owner = hs.SessionDB(db_path=db)
    sidecar = db.with_name(db.name + suffix)
    real_chmod = os.chmod
    closed = False

    def close_last_holder_before_chmod(path, mode, *args, **kwargs):
        nonlocal closed
        if path == sidecar and not closed:
            closed = True
            owner.close()
            assert not sidecar.exists(), "SQLite must remove the sidecar itself"
        return real_chmod(path, mode, *args, **kwargs)

    try:
        owner.create_session("before-close", source="cli")
        assert sidecar.exists()
        monkeypatch.setattr(hs.os, "chmod", close_last_holder_before_chmod)

        with hs.SessionDB(db_path=db) as reopened:
            assert closed, "The opener must reach chmod after a successful lstat"
            before = reopened.get_session("before-close")
            assert before is not None
            assert before["source"] == "cli"
            reopened.create_session("after-close", source="cli")
            after = reopened.get_session("after-close")
            assert after is not None
            assert after["source"] == "cli"
            for ending in ("", "-wal", "-shm"):
                path = db.with_name(db.name + ending)
                assert stat.S_IMODE(path.stat().st_mode) == 0o600

        backups = list(tmp_path.glob("state.db.zeroed-*.bak"))
        assert len(backups) == 1
        assert backups[0].read_bytes() == damaged
    finally:
        owner.close()


@pytest.mark.platforms("posix")
@pytest.mark.parametrize("suffix", ["-wal", "-shm"])
@pytest.mark.parametrize("error_number", [errno.EACCES, errno.EIO])
def test_startup_preserves_sidecar_chmod_errors(
    tmp_path, monkeypatch, suffix, error_number
):
    import hermes_state as hs

    db = tmp_path / "state.db"
    sidecar = db.with_name(db.name + suffix)
    real_chmod = os.chmod
    failure = OSError(error_number, os.strerror(error_number), str(sidecar))

    def refuse_sidecar_chmod(path, mode, *args, **kwargs):
        if path == sidecar:
            raise failure
        return real_chmod(path, mode, *args, **kwargs)

    with hs.SessionDB(db_path=db):
        assert sidecar.exists()
        monkeypatch.setattr(hs.os, "chmod", refuse_sidecar_chmod)
        with pytest.raises(OSError) as caught:
            with hs.SessionDB(db_path=db):
                pass
        assert caught.value is failure
