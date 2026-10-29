"""Owned-row versions stay byte-exact without large-hash GIL handoffs."""

import hashlib
from types import SimpleNamespace

import pytest

from agent import transcript_repair


@pytest.mark.parametrize("size", [0, 2038, 2039, 2047, 2048, 70000])
def test_snapshot_keeps_digest_and_bounds_hash_updates(monkeypatch, size):
    values = [None, "\u65e5\u672c\u8a9e" + "x" * size, bytes(range(256)) * (size // 256 + 1),
              0, -0.0, 1.2345678901234567, "nul\x00text", "\ud800"]
    row = dict(zip(transcript_repair._OWNED_COLUMNS,
                   (values * len(transcript_repair._OWNED_COLUMNS))[:len(transcript_repair._OWNED_COLUMNS)]))
    expected = hashlib.blake2b(digest_size=16)
    for value in row.values():
        if value is None:
            expected.update(b"N")
            continue
        if isinstance(value, bytes):
            tag, data = b"B", value
        elif isinstance(value, str):
            tag, data = b"S", value.encode("utf-8", "surrogatepass")
        else:
            tag, data = (b"F" if isinstance(value, float) else b"I"), repr(value).encode("ascii")
        expected.update(tag + len(data).to_bytes(8, "big") + data)
    sizes = []

    class ObservedDigest:
        def __init__(self, **kwargs):
            self.digest = hashlib.blake2b(**kwargs)

        def update(self, data):
            sizes.append(len(data))
            self.digest.update(data)

        def hexdigest(self):
            return self.digest.hexdigest()

    monkeypatch.setattr(transcript_repair, "hashlib", SimpleNamespace(blake2b=ObservedDigest))
    assert transcript_repair.transcript_row_snapshot(row) == expected.hexdigest()
    assert max(sizes) <= 2047
