"""Read-only bridge from shell setup admission to the runtime's path owners.

Run with setup's interpreter, cwd and sanitized Python environment. No bootstrap,
imports of the CLI, or bytecode writes: this must work before dependencies exist.
"""
from __future__ import annotations

from pathlib import Path
import os
import sys

# A file invocation puts scripts/, not the checkout, on sys.path.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from hermes_constants import (
    account_default_hermes_home,
    get_default_hermes_root,
    get_hermes_home,
)
from pm.environments import dependency_home_root, store_root


def resolved(path: Path) -> Path:
    """Keep missing suffixes, but fail closed on loops and inaccessible ancestors.

    Python 3.13+ may leave a loop unresolved with strict=False rather than raise.
    Probe ancestors so that a lenient result cannot hide a lookup failure.
    """
    for candidate in (path, *path.parents):
        try:
            candidate.stat()
        except FileNotFoundError:
            continue
    return path.resolve(strict=False)


def setup_paths(home: Path | None = None) -> list[Path]:
    """Actual destinations plus canonical-profile interpretation for symlink safety."""
    effective = get_hermes_home() if home is None else home
    root = get_default_hermes_root() if home is None else get_default_hermes_root(home=home)
    paths = [effective, root, get_default_hermes_root(home=resolved(effective))]
    if home is None:
        # setup stages uv here before PM starts (its shell expansion is literal).
        bootstrap_store = Path(os.environ.get("HERMES_RUNTIME_DIR") or Path.home() / ".hermes" / "tools")
        store = store_root(Path.cwd())
        paths.extend((dependency_home_root(), store, store.parent,
                      bootstrap_store, bootstrap_store.parent))
    return [candidate for path in paths for candidate in (path.absolute(), resolved(path))]


def main() -> None:
    mode, *args = sys.argv[1:]
    if mode == "resolve":
        print(Path(args[0]).resolve(strict=False))
        return
    if mode == "account":
        print(account_default_hermes_home())
        return
    account = Path(args[1]) if len(args) > 1 else account_default_hermes_home()
    paths = setup_paths(Path(args[0]) if args else None)
    # Shell consumes newline-delimited records; do not silently split a path.
    values = [str(path) for path in (*paths, account.absolute(), resolved(account))]
    if any(any(char in value for char in "\r\n\t") for value in values):
        raise ValueError("setup paths contain record separators")
    accounts = (account.absolute(), resolved(account))
    for path in paths:
        under = any(path == root or root in path.parents for root in accounts)
        print(f"{'account' if under else 'path'}\t{path}")


if __name__ == "__main__":
    main()
