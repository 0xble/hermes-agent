"""Archive safety and old-Python compatibility at the image installer boundary."""
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import tarfile
import tempfile
import unittest
from unittest.mock import patch

MODULE_PATH = Path(__file__).resolve().parents[3] / 'ci/install-linux-tools.py'
spec = importlib.util.spec_from_file_location('linux_tool_installer', MODULE_PATH)
installer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(installer)


def archive_with(entries):
    data = io.BytesIO()
    with tarfile.open(fileobj=data, mode='w') as archive:
        for name, kind, value in entries:
            member = tarfile.TarInfo(name)
            member.type = kind
            member.mode = 0o6755
            if kind == tarfile.REGTYPE:
                body = value.encode()
                member.size = len(body)
                archive.addfile(member, io.BytesIO(body))
            else:
                member.linkname = value
                archive.addfile(member)
    data.seek(0)
    return tarfile.open(fileobj=data)


class LinuxToolInstallerTests(unittest.TestCase):
    def test_extracts_executable_and_internal_links_without_filter_api(self):
        entries = [
            ('tool/bin/run', tarfile.REGTYPE, '#!/bin/sh\necho installed\n'),
            ('tool/run-link', tarfile.SYMTYPE, 'bin/run'),
            ('tool/run-hardlink', tarfile.LNKTYPE, 'tool/bin/run'),
        ]
        with tempfile.TemporaryDirectory() as directory, archive_with(entries) as archive:
            destination = Path(directory) / 'unpacked'
            # Match the pre-filter API exactly, even when tests run on newer Python.
            extractall = archive.extractall

            def old_extractall(path, members=None, *, numeric_owner=False):
                return extractall(path, members=members, numeric_owner=numeric_owner)

            with patch.object(archive, 'extractall', side_effect=old_extractall):
                installer.extract_archive(archive, destination)
            executable = destination / 'tool/bin/run'
            self.assertEqual(executable.stat().st_mode & 0o7777, 0o755)
            self.assertEqual((destination / 'tool/run-link').read_bytes(), executable.read_bytes())
            self.assertTrue((destination / 'tool/run-hardlink').samefile(executable))
            self.assertEqual(executable.read_text(encoding='utf-8'), '#!/bin/sh\necho installed\n')

    def test_rejects_unsafe_archives_before_writing_members(self):
        bad_archives = [
            [('ordinary', tarfile.REGTYPE, 'safe'), ('../escape', tarfile.REGTYPE, 'bad')],
            [('/absolute', tarfile.REGTYPE, 'bad')],
            [('tool/link', tarfile.SYMTYPE, '../../escape')],
            [('link', tarfile.SYMTYPE, '/absolute')],
            [('link', tarfile.SYMTYPE, 'inside'), ('link/payload', tarfile.REGTYPE, 'bad')],
            [('link/payload', tarfile.REGTYPE, 'bad'), ('link', tarfile.SYMTYPE, 'inside')],
            [('link', tarfile.LNKTYPE, '../escape')],
            [('link', tarfile.LNKTYPE, 'missing')],
            [('a', tarfile.SYMTYPE, 'b'), ('hardlink', tarfile.LNKTYPE, 'a')],
            [('device', tarfile.CHRTYPE, '')],
            [('fifo', tarfile.FIFOTYPE, '')],
            [('duplicate', tarfile.REGTYPE, 'one'), ('./duplicate', tarfile.REGTYPE, 'two')],
        ]
        for entries in bad_archives:
            with self.subTest(entries=entries), tempfile.TemporaryDirectory() as directory, archive_with(entries) as archive:
                destination = Path(directory) / 'unpacked'
                with self.assertRaises(RuntimeError):
                    installer.extract_archive(archive, destination)
                self.assertEqual(list(destination.iterdir()), [])

    def test_rejects_symlink_chain_that_changes_parent_resolution(self):
        entries = [('a', tarfile.SYMTYPE, '.'), ('b', tarfile.SYMTYPE, 'a/../outside')]
        with tempfile.TemporaryDirectory() as directory, archive_with(entries) as archive:
            with self.assertRaisesRegex(RuntimeError, 'link chain escapes'):
                installer.extract_archive(archive, Path(directory) / 'unpacked')

    def test_checksum_mismatch_stops_before_extraction_or_install(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            payload = root / 'payload'
            payload.write_bytes(b'not the authenticated archive')
            manifest = {'arm64': [{'kind': 'rust', 'url': payload.as_uri(), 'sha256': hashlib.sha256(b'expected').hexdigest()}]}
            (root / 'linux-artifacts.json').write_text(json.dumps(manifest), encoding='utf-8')
            with patch.object(installer, 'HERE', root), patch.object(installer, 'extract_archive') as extract, patch.object(installer.subprocess, 'run') as run:
                with self.assertRaisesRegex(RuntimeError, 'Checksum mismatch'):
                    installer.main('arm64')
                extract.assert_not_called()
                run.assert_not_called()


if __name__ == '__main__':
    unittest.main()
