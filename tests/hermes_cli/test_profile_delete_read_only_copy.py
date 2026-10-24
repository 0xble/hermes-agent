"""A read-only profile copy (0555 dirs, 0444 files) can still be deleted (N106): the owner check
cannot reserve gateway.lock there, so it proves the lock unheld through a read-only flock."""
import os
import select
import subprocess
import sys

import pytest

pytestmark = [pytest.mark.platforms("linux", "macos"),
              pytest.mark.skipif(hasattr(os, 'geteuid') and os.geteuid() == 0, reason="root writes 0555")]


def _read_only_copy(home, *, slots, read_only):
    from gateway.gateway_slot import slot_filename
    (home / 'sub').mkdir(parents=True)
    (home / 'config.yaml').write_text('model: x\n')
    for slot in slots:
        (home / slot_filename('gateway.lock', slot)).write_text('')
    if read_only:
        for parent, _dirs, files in os.walk(home, topdown=False):
            for name in files:
                os.chmod(os.path.join(parent, name), 0o444)
            os.chmod(parent, 0o555)


@pytest.mark.parametrize('slots', [(), ('a',), ('b',), ('a', 'b')],
                         ids=['no-lock', 'slot-a', 'slot-b', 'both-slots'])
@pytest.mark.parametrize('read_only', [False, True], ids=['writable', 'read-only'])
def test_read_only_copy_is_unowned_unless_its_lock_is_held(tmp_path, slots, read_only):
    from gateway.gateway_slot import slot_filename
    from hermes_cli.profiles_remove import wait_profile_unowned
    home = tmp_path / 'profiles' / 'ro'
    _read_only_copy(home, slots=slots, read_only=read_only)
    try:
        assert wait_profile_unowned(home, timeout=0) is True
        if slots:  # Every runtime slot, including a read-only sibling, blocks removal.
            paths = [home / slot_filename('gateway.lock', slot) for slot in slots]
            holder = subprocess.Popen(
                [sys.executable, '-c', 'import fcntl,sys;fs=[open(p) for p in sys.argv[1:]];'
                 '[fcntl.flock(f,fcntl.LOCK_SH) for f in fs];print(1,flush=True);sys.stdin.read()',
                 *map(str, paths)],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            try:
                assert holder.stdout is not None
                assert select.select([holder.stdout], [], [], 20)[0], 'lock holder did not report readiness'
                assert holder.stdout.readline().strip() == '1'
                assert wait_profile_unowned(home, timeout=0) is False
            finally:
                try:
                    _stdout, stderr = holder.communicate('', timeout=10)
                except subprocess.TimeoutExpired:
                    holder.kill()
                    holder.communicate(timeout=10)
                    raise
                assert holder.returncode == 0, stderr
            # An unopenable slot is unknown, not proof that the entire home is unowned.
            paths[-1].chmod(0)
            try:
                assert wait_profile_unowned(home, timeout=0) is False
            finally:
                paths[-1].chmod(0o444 if read_only else 0o600)
    finally:
        for parent, _dirs, _files in os.walk(tmp_path):
            os.chmod(parent, 0o755)
