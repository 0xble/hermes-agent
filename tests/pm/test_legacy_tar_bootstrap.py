"""Historical Python can stage the pinned interpreter without losing containment."""
import io
import tarfile

import pytest

from pm.store import extract_tar


@pytest.fixture(autouse=True)
def historical_tarfile(monkeypatch):
    """3.11.0 lacks the filter keyword, data_filter, and TarInfo.replace."""
    original_extract = tarfile.TarFile.extract
    original_extractall = tarfile.TarFile.extractall
    original_chown = tarfile.TarFile.chown
    original_chmod = tarfile.TarFile.chmod

    def chown(self, member, targetpath, numeric_owner):
        assert numeric_owner and isinstance(member.uid, int) and isinstance(member.gid, int)
        return original_chown(self, member, targetpath, numeric_owner)

    def chmod(self, member, targetpath):
        assert isinstance(member.mode, int), "old chmod cannot skip None"
        return original_chmod(self, member, targetpath)

    def extract(self, member, path="", set_attrs=True, *, numeric_owner=False):
        return original_extract(self, member, path, set_attrs,
                                numeric_owner=numeric_owner, filter="fully_trusted")

    def extractall(self, path="", members=None, *, numeric_owner=False):
        return original_extractall(self, path, members, numeric_owner=numeric_owner,
                                   filter="fully_trusted")

    monkeypatch.setattr(tarfile.TarFile, "extract", extract)
    monkeypatch.setattr(tarfile.TarFile, "extractall", extractall)
    monkeypatch.setattr(tarfile.TarFile, "chown", chown)
    monkeypatch.setattr(tarfile.TarFile, "chmod", chmod)
    monkeypatch.delattr(tarfile, "data_filter")
    monkeypatch.delattr(tarfile.TarInfo, "replace")


def archive(entries):
    stream = io.BytesIO()
    with tarfile.open(fileobj=stream, mode="w") as tf:
        for name, kind, value in entries:
            member = tarfile.TarInfo(name)
            member.mode = 0o6755
            member.uid = member.gid = 123456
            member.uname = member.gname = "archive-owner"
            member.type = kind
            if kind == tarfile.REGTYPE:
                member.size = len(value)
                tf.addfile(member, io.BytesIO(value))
            else:
                member.linkname = value
                tf.addfile(member)
    stream.seek(0)
    return stream


@pytest.mark.platforms("posix")
def test_historical_tarfile_extracts_executable_and_relative_terminfo_link(tmp_path):
    dest = tmp_path / "python"
    extract_tar(archive([
        ("bin/", tarfile.DIRTYPE, ""),
        ("bin/python3", tarfile.REGTYPE, b"python"),
        ("share/terminfo/a/adm1178", tarfile.REGTYPE, b"entry"),
        ("share/terminfo/1/1178", tarfile.SYMTYPE, "../a/adm1178"),
        ("bin/python", tarfile.LNKTYPE, "bin/python3"),
    ]), dest)
    assert (dest / "bin/python3").read_bytes() == b"python"
    assert (dest / "bin/python3").stat().st_mode & 0o7777 == 0o755
    assert (dest / "bin/python3").stat().st_uid == dest.stat().st_uid
    assert (dest / "bin").stat().st_mode & 0o7777 == 0o755
    assert (dest / "bin/python").read_bytes() == b"python"
    assert (dest / "share/terminfo/1/1178").read_bytes() == b"entry"


@pytest.mark.parametrize("name,kind,value", [
    ("../victim", tarfile.REGTYPE, b"overwritten"),
    ("/absolute", tarfile.REGTYPE, b"overwritten"),
    ("escape", tarfile.SYMTYPE, "../victim"),
    ("escape/", tarfile.SYMTYPE, "../victim"),
    ("escape", tarfile.LNKTYPE, "../victim"),
    ("pipe", tarfile.FIFOTYPE, ""),
    ("device", tarfile.CHRTYPE, ""),
])
def test_historical_tarfile_refuses_unsafe_members(tmp_path, name, kind, value):
    victim = tmp_path / "victim"
    victim.write_bytes(b"original")
    with pytest.raises(tarfile.TarError):
        extract_tar(archive([(name, kind, value)]), tmp_path / "dest")
    assert victim.read_bytes() == b"original"


@pytest.mark.platforms("posix")
def test_historical_tarfile_rechecks_preexisting_symlink_parent(tmp_path):
    dest = tmp_path / "dest"
    dest.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (dest / "bin").symlink_to(outside, target_is_directory=True)
    with pytest.raises(tarfile.TarError):
        extract_tar(archive([("bin/tool", tarfile.REGTYPE, b"escaped")]), dest)
    assert not (outside / "tool").exists()
