"""Path normalization and bounded regular-file reads for the lifecycle guard."""

from __future__ import annotations

import os
import stat
from pathlib import Path
from typing import Optional

_MAX_REFERENCED_SCRIPT_BYTES = 1024 * 1024

# Directory names directly under `Library` that mark a FileProvider-backed subtree: `Mobile
# Documents` is iCloud Drive; `CloudStorage` hosts third-party providers (Dropbox, OneDrive, Google
# Drive, ...).
_CLOUD_PLACEHOLDER_MARKERS = frozenset({"Mobile Documents", "CloudStorage"})

# Bytes sniffed before reading a referenced file in full (see _BINARY_MAGICS).
_BINARY_SNIFF_BYTES = 4096

_BINARY_MAGICS = (
    b"\x7fELF",              # ELF — Linux/BSD executables and shared objects
    b"\xfe\xed\xfa\xce",     # Mach-O 32-bit
    b"\xfe\xed\xfa\xcf",     # Mach-O 64-bit
    b"\xce\xfa\xed\xfe",     # Mach-O 32-bit, byte-swapped
    b"\xcf\xfa\xed\xfe",     # Mach-O 64-bit, byte-swapped
    b"\xca\xfe\xba\xbe",     # Mach-O universal ("fat") binary
    b"MZ",                   # PE/COFF — Windows .exe/.dll
    b"!<arch>",              # static archive (.a)
    b"\x1f\x8b",             # gzip
    b"PK\x03\x04",           # zip (also .jar/.whl/.egg)
)


def _capped_read_limit(max_bytes: Optional[int]) -> int:
    """Per-read byte cap: never above the per-file cap, never negative. One definition so local and
    remote reads cannot diverge.

    See #76762, #77703.
    """
    if max_bytes is None:
        return _MAX_REFERENCED_SCRIPT_BYTES
    return min(_MAX_REFERENCED_SCRIPT_BYTES, max(0, int(max_bytes)))


def _unreadable_reason(path: Path) -> str:
    """Name why an *executed* script failed closed without being scanned (live SQLite, device,
    oversized). Message-only: the fail-closed verdict itself came from the bounded reader."""
    from hermes_cli.sqlite_safe_read import has_live_connection

    if has_live_connection(path):
        return f"`{path}` is a SQLite database open in this gateway process"
    try:
        metadata = os.stat(path)
    except OSError:
        return f"`{path}` could not be read"
    if not stat.S_ISREG(metadata.st_mode):
        return f"`{path}` is not a regular file"
    return f"`{path}` is larger than the scan cap ({_MAX_REFERENCED_SCRIPT_BYTES} bytes) or the remaining walk budget"


def _resolve_lenient(path: Path) -> Path:
    """``path.resolve(strict=False)``, falling back to *path* on OSError (unreadable/long) or
    ValueError (embedded NUL from decoded binary tokenized as a path) — never crash the guard."""
    try:
        return path.resolve(strict=False)
    except (OSError, ValueError):
        return path


def _is_cloud_placeholder_path(path: Path) -> bool:
    """True for paths inside a macOS FileProvider-backed subtree. ``O_NONBLOCK`` does not make
    regular-file reads non-blocking, so opening an evicted placeholder can wait indefinitely for
    hydration before any command timeout starts: identify the boundary from the path alone."""
    parts = path.parts
    return any(
        parts[index - 1] == "Library" and part in _CLOUD_PLACEHOLDER_MARKERS
        for index, part in enumerate(parts)
        if index
    )


def _on_cloud_path(path: Path) -> bool:
    """Lexical OR resolved cloud check: covers direct cloud paths and local symlinks into one."""
    return _is_cloud_placeholder_path(path) or _is_cloud_placeholder_path(_resolve_lenient(path))


def _expand_candidate_path(candidate: str) -> Optional[Path]:
    """Sanitize a tokenized path candidate at the ingestion boundary. Tokens from shlex-splitting
    arbitrary (possibly binary-decoded) text can carry NUL or junk that each downstream ``Path`` op
    rejects differently (ValueError, RuntimeError when HOME is unset under launchd, OSError); reject
    once here. ``None`` = not a real path, nothing to scan.

    Every OS-facing ``Path`` operation downstream (``expanduser``, ``os.open``, ``resolve``) raises a
    *different* exception for the same junk (``ValueError: embedded null byte``, ``RuntimeError: Could not
    determine home directory`` when HOME is unset under launchd, OSError for over-long paths). Rejecting
    here — once, before any OS call — is the whole-class fix; catching per-syscall was the whack-a-mole that
    produced #76762, #77703, #77780, and #78256.
    """
    if not candidate or "\x00" in candidate:
        return None
    try:
        return Path(candidate).expanduser()
    except (ValueError, RuntimeError, OSError):
        return None


def _resolve_script_path(script_path: str) -> Optional[Path]:
    """Resolve a cron ``script`` value the way ``cron.scheduler`` does (relative paths live under
    ``<HERMES_HOME>/scripts/``) so the guard scans the file that will actually run."""
    from hermes_constants import get_hermes_home

    raw = _expand_candidate_path(script_path)
    if raw is None:
        return None
    if raw.is_absolute():
        return raw
    try:
        return get_hermes_home() / "scripts" / raw
    except (RuntimeError, OSError):
        # get_hermes_home() falls back to Path.home(), which raises when neither HERMES_HOME nor
        # HOME is resolvable (launchd/systemd) — same ingestion contract: nothing to scan.
        return None


def _has_binary_magic(data: bytes) -> bool:
    """True when *data* starts with a known compiled-binary signature. Deliberately narrower than
    "contains a NUL": ``bash`` still executes a NUL-bearing script, so a padded script must not
    bypass the scan. A shebang always wins (interpreted, never binary). Extensions are not
    consulted: a suffixless script must still be scanned and fail closed if oversized."""
    if data.startswith(b"#!"):
        return False
    return data.startswith(_BINARY_MAGICS)


def _read_regular_script(path: Path, byte_limit: int) -> tuple[Optional[str], bool]:
    """Read up to the supplied cap; the caller owns device checks and SQLite exclusion."""
    if _on_cloud_path(path):
        return None, True
    flags = os.O_RDONLY | getattr(os, "O_NONBLOCK", 0)
    try:
        descriptor = os.open(path, flags)
    except (OSError, ValueError):
        # OSError: unreadable/missing/over-long. ValueError: embedded NUL in *path*. Either is
        # "nothing to scan" — never crash the guard.
        return None, False
    try:
        # ValueError: an embedded NUL byte in *path* itself — a binary's decoded bytes tokenized into a
        # bogus script path by the recursion (#77703).
        metadata = os.fstat(descriptor)
        # Directories are not scripts. Docker Desktop writes ``fpath=(~/.docker/completions …)`` into
        # ``~/.zshrc``; the walk then treats that dir as a referenced script and used to fail-closed,
        # blocking ``source ~/.zshrc`` (#86753). Devices/sockets stay fail-closed.
        if not stat.S_ISREG(metadata.st_mode):
            # Directories are not scripts (`fpath=(~/.docker/completions …)` in ~/.zshrc must not
            # block `source ~/.zshrc`). Devices/sockets stay fail-closed.
            return None, not stat.S_ISDIR(metadata.st_mode)
        # Sniff a small prefix first: compiled binaries are never shell scripts, so skip them
        # WITHOUT reading the rest or feeding decoded garbage into the recursion.
        # Deliberately NOT keyed on the mere presence of a NUL byte (#77927): bash executes a text script
        # straight past an embedded NUL, so NUL-bearing text must fall through to the magic-number check +
        # NUL-strip below.
        data = os.read(descriptor, _BINARY_SNIFF_BYTES)
        if _has_binary_magic(data):
            return None, False
        # A regular file whose size already exceeds the cap fails closed without reading it (the
        # walk budget can be far below 1 MiB).
        if metadata.st_size > byte_limit:
            return None, True
        # Read the remainder (bounded); loop because os.read may return short.
        while len(data) <= byte_limit:
            chunk = os.read(descriptor, byte_limit + 1 - len(data))
            if not chunk:
                break
            data += chunk
    except OSError:
        return None, False
    finally:
        os.close(descriptor)
    if _has_binary_magic(data):
        return None, False
    # Size check BEFORE NUL stripping: stripping shrinks the buffer and would let an oversized file
    # slip under the threshold past this fail-closed branch.
    if len(data) > byte_limit:
        return None, True
    if b"\x00" in data:
        data = data.replace(b"\x00", b"")
    return data.decode("utf-8", errors="replace"), False
