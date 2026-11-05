"""Archive safety and old-Python compatibility at the image installer boundary."""
from contextlib import contextmanager, redirect_stderr
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import tarfile
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
import urllib.error

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
            self.assertEqual(executable.read_text(encoding='utf-8-sig'), '#!/bin/sh\necho installed\n')

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


@contextmanager
def download_fixture():
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        with archive_with([('uv/uv', tarfile.REGTYPE, 'tool binary')]) as archive:
            data = archive.fileobj.getvalue()
        artifact = {'url': 'https://example.invalid/uv.tar', 'sha256': hashlib.sha256(data).hexdigest(), 'kind': 'uv'}
        (root / 'linux-artifacts.json').write_text(json.dumps({'amd64': [artifact]}), encoding='utf-8')
        clock = SimpleNamespace(now=0.0, sleeps=[])

        def sleep(seconds):
            clock.sleeps.append(seconds)
            clock.now += seconds

        installed = []
        with patch.object(installer, 'HERE', root), \
             patch.object(installer, 'time', SimpleNamespace(monotonic=lambda: clock.now, sleep=sleep), create=True), \
             patch.object(installer.shutil, 'copy2', side_effect=lambda source, target: installed.append(Path(source).read_bytes())):
            yield root, data, artifact, clock, installed


class LinuxToolDownloadTests(unittest.TestCase):
    def test_cache_hit_miss_and_corruption_keep_checksum_boundary(self):
        for state in ['miss', 'hit', 'corrupt']:
            with self.subTest(state=state), download_fixture() as (root, data, artifact, clock, installed):
                cache = root / 'cache'
                cache.mkdir()
                cached = cache / artifact['sha256']
                if state != 'miss':
                    cached.write_bytes(data if state == 'hit' else b'corrupt cache')
                with patch.object(installer.urllib.request, 'urlopen', return_value=io.BytesIO(data)) as opener:
                    if state == 'corrupt':
                        with self.assertRaisesRegex(RuntimeError, 'Checksum mismatch'):
                            installer.main('amd64', cache_directory=cache)
                    else:
                        installer.main('amd64', cache_directory=cache)
                        self.assertEqual(installed, [b'tool binary'])
                        self.assertEqual(cached.read_bytes(), data)
                    self.assertEqual(opener.call_count, 1 if state == 'miss' else 0)
                self.assertEqual(clock.sleeps, [])
                if state == 'corrupt':
                    self.assertFalse(installed)

    def test_transient_download_retries_before_install(self):
        for failure in [504, 500, 503, 429, 'url', 'timeout', 'reset', 'read-reset']:
            with self.subTest(failure=failure), download_fixture() as (_, data, artifact, clock, installed):
                if isinstance(failure, int):
                    error = urllib.error.HTTPError(artifact['url'], failure, 'transient', None, None)
                else:
                    error = {'url': urllib.error.URLError('connection lost'), 'timeout': TimeoutError(),
                             'reset': ConnectionResetError(), 'read-reset': ConnectionResetError()}[failure]
                if failure == 'read-reset':
                    broken = io.BytesIO(b'partial')
                    broken.read = Mock(side_effect=[b'partial', error])
                    attempts = [broken, io.BytesIO(data)]
                else:
                    attempts = [error, io.BytesIO(data)]
                log = io.StringIO()
                with patch.object(installer.urllib.request, 'urlopen', side_effect=attempts) as opener, redirect_stderr(log):
                    installer.main('amd64')
                self.assertEqual(opener.call_count, 2)
                self.assertEqual(installed, [b'tool binary'])
                self.assertEqual(len(clock.sleeps), 1)
                self.assertTrue(0 < clock.sleeps[0] <= 3)
                self.assertIn(artifact['url'], log.getvalue())
                self.assertIn('retry', log.getvalue().lower())
                if isinstance(failure, int):
                    self.assertIn(str(failure), log.getvalue())

    def test_download_failure_is_bounded_and_never_installs(self):
        for failure in ['persistent', 'deadline', 404, 403, 'checksum']:
            with self.subTest(failure=failure), download_fixture() as (_, data, artifact, clock, installed):
                attempts = []

                def open_url(url, timeout):
                    attempts.append(timeout)
                    if failure == 'checksum':
                        return io.BytesIO(b'untrusted payload')
                    if failure == 'deadline':
                        clock.now += timeout
                        raise TimeoutError('socket timed out')
                    code = failure if isinstance(failure, int) else 504
                    raise urllib.error.HTTPError(url, code, 'failed', None, None)

                expected = RuntimeError if failure == 'checksum' else (TimeoutError if failure == 'deadline' else urllib.error.HTTPError)
                with patch.object(installer.urllib.request, 'urlopen', side_effect=open_url), redirect_stderr(io.StringIO()):
                    with self.assertRaises(expected):
                        installer.main('amd64')
                self.assertEqual(len(attempts), 5 if failure == 'persistent' else (2 if failure == 'deadline' else 1))
                self.assertLessEqual(clock.now, 180)
                self.assertFalse(installed)
                self.assertEqual(len(clock.sleeps), len(attempts) - 1)
                self.assertTrue(all(0 < timeout <= 120 for timeout in attempts))
                self.assertEqual(clock.sleeps, sorted(clock.sleeps))
                if failure == 'persistent':
                    for attempt, delay in enumerate(clock.sleeps, 1):
                        self.assertTrue(2 ** attempt <= delay <= 2 ** attempt + 1)


if __name__ == '__main__':
    unittest.main()
