"""Run the real Kanban reaper between terminal EOF and its reader's wait()."""
import json
import os
import runpy

from hermes_constants import get_hermes_home
from tools.process_registry import ProcessRegistry


finish_reader = ProcessRegistry._finish_reader


def reap_before_reader_wait(self, session, decoder, append, label, wait, exit_code):
    reaped = []
    if os.name == "posix" and label == "Process":
        from hermes_cli.kanban_db_dispatch import reap_worker_zombies
        from tests.waiting import process_alive, wait_until
        # Observing a zombie through psutil does not collect its exit status. No Popen
        # poll/wait may run before the competing reaper gets this finished child.
        wait_until(lambda: not process_alive(session.process.pid), timeout=10,
                   what="terminal child exit before reader wait")
        reaped = reap_worker_zombies()

    def collect_exit():
        result = wait()
        # Publish the probe before the reader publishes completion, so the note
        # assertion always has the exit-status evidence that produced it.
        (get_hermes_home() / "terminal-exit-order.json").write_text(json.dumps({
            "pid": session.process.pid, "reaped_by_kanban": reaped, "exit_code": exit_code(),
        }), encoding="utf-8")
        return result
    finish_reader(self, session, decoder, append, label, collect_exit, exit_code)


ProcessRegistry._finish_reader = reap_before_reader_wait
runpy.run_module("gateway.run", run_name="__main__")
