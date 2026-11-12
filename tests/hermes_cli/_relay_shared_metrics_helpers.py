"""Synchronization shared by the real and CLI metrics probes."""

import faulthandler
import threading


def join_export_workers() -> None:
    """Wait for asynchronous exports before reading the store, retaining timeout evidence."""
    while workers := [t for t in threading.enumerate() if t.name == "hermes-shared-metrics-flush"]:
        for worker in workers:
            worker.join(timeout=10)
            if worker.is_alive():
                faulthandler.dump_traceback()
            assert not worker.is_alive()
