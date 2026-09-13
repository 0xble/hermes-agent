"""Directory initialization and storage diagnostics for the active Hermes home."""

import os
from pathlib import Path


class HomeInitializationError(RuntimeError):
    """The home skeleton is unavailable, not an invalid YAML document."""


def _directory_links(path: Path, boundary: Path | None = None) -> list[Path]:
    """Symlinks on ``path`` that the operator plausibly owns.

    Only links at or below ``boundary`` (the Hermes home) count. Walking every
    ancestor instead lets an unrelated SYSTEM symlink far above the home decide
    that nobody secures a credential directory: on macOS ``/var -> /private/var``
    makes that true for any home under a temp dir, and for any real home reached
    through a symlinked volume. Without a boundary the home silently keeps its
    umask mode (0755) instead of 0700. Fork contract HERMES-134.
    """
    parts = [*reversed(path.parents), path]
    if boundary is not None:
        try:
            parts = [part for part in parts if part == boundary or boundary in part.parents]
        except (OSError, ValueError):
            pass
    return [part for part in parts if part.is_symlink()]


def _ensure_directory(path: Path, *, create: bool, secure: bool, boundary: Path | None = None) -> None:
    from hermes_cli.config import _secure_dir

    detail = ""
    try:
        links = _directory_links(path, boundary)
        detail = "; ".join(f"{link} -> {link.readlink()}" for link in links)
        # Never materialize a missing mount's target on the underlying disk.
        for link in links:
            if not link.is_dir():
                raise FileNotFoundError(f"Directory link is unavailable: {link}")
        if create:
            # mkdir also refuses dangling ancestor links outside the permission
            # boundary. It cannot create a missing target through such a link.
            path.mkdir(parents=True, exist_ok=True)
        elif not path.is_dir():
            raise FileNotFoundError(f"Required directory does not exist: {path}")
        # The operator owns permissions beyond a link, including logs/curator.
        if secure and not links:
            _secure_dir(path)
    except OSError as exc:
        raise HomeInitializationError(
            f"Cannot initialize Hermes directory {path}: {exc}. "
            + (f"Directory links: {detail}. " if detail else "")
            + "Check the directory/link target, mount availability and access permissions; "
            "restore the mount or repair the link before retrying. "
            "Hermes has not replaced the link or created its missing target."
        ) from exc


def initialize_home(home: Path, subdirs: tuple[str, ...], ensured: set[str]) -> None:
    from hermes_cli.config import _ensure_default_soul_md, is_managed

    managed = is_managed()
    old_umask = os.umask(0o007) if managed else None
    try:
        _ensure_directory(home, create=not managed, secure=not managed, boundary=home)
        required = ("cron", "sessions", "logs", "memories") if managed else subdirs
        for subdir in required:
            _ensure_directory(home / subdir, create=not managed, secure=not managed, boundary=home)
        if managed:
            _ensure_directory(home / "logs" / "curator", create=True, secure=False, boundary=home)
        try:
            _ensure_default_soul_md(home)
        except OSError as exc:
            raise HomeInitializationError(
                f"Cannot initialize Hermes home {home}: {exc}. "
                "Check storage availability and access permissions."
            ) from exc
    finally:
        if old_umask is not None:
            os.umask(old_umask)
    # Plugin discovery rebinds the resolved home. Do not repeat chmod through
    # that spelling after the operator-owned symlink boundary has been lost.
    ensured.update((str(home), str(home.resolve())))


def config_load_issue(exc: Exception):
    from hermes_cli.config import ConfigIssue

    if isinstance(exc, (HomeInitializationError, OSError)):
        return ConfigIssue(
            "error", f"Hermes storage is unavailable: {exc}",
            "Check the reported path, link target, mount and permissions; keep config.yaml unchanged.",
        )
    return ConfigIssue("error", "Could not load config.yaml", "Run 'hermes setup' to create a valid config")
