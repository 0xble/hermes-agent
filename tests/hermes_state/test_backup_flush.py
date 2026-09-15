"""Forensic copies flush writable handles without losing read-only source metadata."""
import os
import stat
from pathlib import Path

import pytest

import hermes_state_repair as repair


@pytest.mark.parametrize("readonly", [False, True])
def test_backup_flush_is_writable_and_preserves_bundle(tmp_path, monkeypatch, readonly):
    source = tmp_path / "state.db"
    sources = [source, Path(str(source) + "-wal"), Path(str(source) + "-shm")]
    for index, path in enumerate(sources):
        path.write_bytes(f"forensic bytes {index}".encode())
        os.utime(path, ns=(1_700_000_000_000_000_000, 1_700_000_000_000_000_000))
        path.chmod(0o444 if readonly else 0o600)
    expected = [(path.read_bytes(), stat.S_IMODE(path.stat().st_mode), path.stat().st_mtime_ns) for path in sources]
    flushed = []
    promoted = []
    real_fsync, real_replace = os.fsync, os.replace

    def fsync(fd):
        if stat.S_ISREG(os.fstat(fd).st_mode):
            # Zero-byte write requires a writable handle on the native OS but
            # cannot change the forensic payload. No OS identity is mocked.
            os.write(fd, b"")
            flushed.append(fd)
        return real_fsync(fd)

    def replace(src, dst):
        promoted.append(Path(dst))
        return real_replace(src, dst)

    monkeypatch.setattr(repair.os, "fsync", fsync)
    monkeypatch.setattr(repair.os, "replace", replace)
    backup = tmp_path / "backup.db"
    try:
        repair._publish_backup_bundle(source, tmp_path / "staging", backup)
        assert len(flushed) == 3
        assert promoted == [Path(str(backup) + "-wal"), Path(str(backup) + "-shm"), backup]
        for path, (content, mode, mtime) in zip([backup, Path(str(backup) + "-wal"), Path(str(backup) + "-shm")], expected):
            assert path.read_bytes() == content
            assert stat.S_IMODE(path.stat().st_mode) == mode
            assert path.stat().st_mtime_ns == mtime
    finally:
        # Native Windows cannot delete files carrying the read-only attribute.
        for path in tmp_path.iterdir():
            path.chmod(0o600)
