"""uv archive caching preserves checksum checks before seeding setup-uv."""
import hashlib
import io
from pathlib import Path
import tarfile
import tempfile
import unittest
from unittest.mock import patch
import zipfile

from scripts.ci import pinned_tools


class UvArchiveTests(unittest.TestCase):
    def test_seed_from_miss_hit_and_corrupt_tar_or_zip(self):
        for extension in ('tar.gz', 'zip'):
            with self.subTest(extension=extension), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                body = io.BytesIO()
                suffix = '.exe' if extension == 'zip' else ''
                binaries = {'uv' + suffix: b'uv binary', 'uvx' + suffix: b'uvx binary'}
                if extension == 'zip':
                    with zipfile.ZipFile(body, 'w') as archive:
                        for name, data in binaries.items():
                            archive.writestr(name, data)
                else:
                    with tarfile.open(fileobj=body, mode='w:gz') as archive:
                        for name, data in binaries.items():
                            member = tarfile.TarInfo('uv-target/' + name)
                            member.size = len(data)
                            archive.addfile(member, io.BytesIO(data))
                data = body.getvalue()
                pin = {'url': 'https://example.invalid/uv.' + extension,
                       'sha256': hashlib.sha256(data).hexdigest(), 'version': '1.2.3', 'architecture': 'x86_64'}
                cache, store = root / 'cache', root / 'tool-cache'
                with patch.object(pinned_tools.installer.urllib.request, 'urlopen', return_value=io.BytesIO(data)) as opener:
                    pinned_tools.seed_uv(pin, cache, store)
                    self.assertEqual(opener.call_count, 1)
                # Re-extract verified bytes even if the installed tool-cache changed.
                installed = store / 'uv/1.2.3/x86_64'
                (installed / ('uv' + suffix)).write_bytes(b'stale tool cache')
                with patch.object(pinned_tools.installer.urllib.request, 'urlopen', side_effect=AssertionError('network on hit')):
                    pinned_tools.seed_uv(pin, cache, store)
                    for name, expected in binaries.items():
                        self.assertEqual((installed / name).read_bytes(), expected)
                    self.assertTrue(installed.with_suffix('.complete').is_file())
                    (cache / pin['sha256']).write_bytes(b'corrupt')
                    with self.assertRaisesRegex(RuntimeError, 'Checksum mismatch'):
                        pinned_tools.seed_uv(pin, cache, store)
