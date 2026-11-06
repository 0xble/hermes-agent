"""Socket-sized private test directories without escaping the configured scratch root.

Promoted from test_scale_to_zero.short_sock_dir. The file runner nests TMPDIR
under HERMES_TEST_SCRATCH_ROOT; use that explicit root when the per-file temp
path leaves no room for the real AF_UNIX address. Never fall back to /tmp.
"""
from contextlib import contextmanager
import os
from pathlib import Path
import tempfile


@contextmanager
def short_socket_dir(relative_socket="gateway.sock"):
    """Yield a private directory whose requested socket fits the 100-byte budget."""
    candidates = [tempfile.gettempdir()]
    scratch = os.environ.get("HERMES_TEST_SCRATCH_ROOT")
    if scratch and scratch not in candidates:
        candidates.append(scratch)
    for base in candidates:
        # TemporaryDirectory provides exclusive creation, mode 0700, and cleanup.
        # No prefix: descriptive fixture names needlessly consume sun_path bytes.
        with tempfile.TemporaryDirectory(prefix="", dir=base) as directory:
            path = Path(directory)
            if os.name == "nt" or len(os.fsencode(path / relative_socket)) <= 100:
                yield path
                return
    raise ValueError(f"no configured scratch root fits AF_UNIX socket {relative_socket!r}: {candidates}")
