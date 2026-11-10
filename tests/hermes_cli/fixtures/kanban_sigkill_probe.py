"""FIFO-controlled real dispatcher/submitter for the SIGKILL registration contract."""
from contextlib import closing
import os
from pathlib import Path
import sys


def barrier(root, name):
    # Open the reader before publishing readiness, so releasing a barrier cannot race its open.
    with (root / name).open('r+b', buffering=0) as gate:
        (root / (name + '.ready')).touch()
        assert gate.read(1) == b'1'


def main():
    root = Path(sys.argv[2])
    if sys.argv[1] == 'worker':
        (root / 'worker.pid').write_text(str(os.getpid()), encoding='utf-8')
        barrier(root, 'worker')
        from hermes_cli.kanban_worker_client import main as worker_main
        return worker_main()

    from hermes_cli import kanban_db_dispatch as dispatch
    from hermes_cli.kanban_db_connect import connect
    window = sys.argv[3]
    original_record = dispatch._set_worker_pid
    original_spawn = dispatch._call_spawn_fn

    def spawn(*args, **kwargs):
        if window == 'claimed':
            barrier(root, 'dispatcher')
        return original_spawn(*args, **kwargs)

    def record(*args, **kwargs):
        if window.startswith('spawned'):
            barrier(root, 'dispatcher')
        original_record(*args, **kwargs)
        if window.startswith('recorded'):
            barrier(root, 'dispatcher')

    dispatch._call_spawn_fn = spawn
    dispatch._set_worker_pid = record
    dispatch._worker_argv = lambda *_: [sys.executable, __file__, 'worker', str(root)]
    with closing(connect()) as conn:
        dispatch.dispatch_once(conn, max_spawn=1)
    raise AssertionError('dispatcher passed the kill barrier')


if __name__ == '__main__':
    raise SystemExit(main())
