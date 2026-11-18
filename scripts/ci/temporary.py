"""Select CI scratch parents without silently abandoning a requested volume."""
from __future__ import annotations

import os
from pathlib import Path
import tempfile


def temporary_parent(parent: Path | None = None) -> Path:
    """Keep explicit parents, but validate ambient TMPDIR before stdlib fallback."""
    if parent is not None:
        return parent.resolve()
    requested = os.environ.get('TMPDIR')
    if not requested:
        return Path(tempfile.gettempdir()).resolve()
    try:
        path = Path(requested).resolve()
        if not path.is_dir():
            raise RuntimeError(f'Requested TMPDIR is missing or not a directory: {requested}')
        # Probe actual creation, rather than mode bits that can disagree with ACLs.
        with tempfile.TemporaryFile(dir=path):
            pass
    except OSError as error:
        raise RuntimeError(f'Requested TMPDIR is not writable: {requested}: {error}') from error
    return path
