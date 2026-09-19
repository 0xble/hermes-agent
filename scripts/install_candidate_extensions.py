#!/usr/bin/env python3
"""Install the Hermes Agent Next candidate extensions into a profile.

The source tree is intentionally kept outside Hermes's normal plugin search
path. This command copies the pinned extension set into ``$HERMES_HOME`` and
updates only the candidate-owned ``plugins.enabled`` entries, making discovery
testable in a fresh process and making repeated installs idempotent.
"""

from __future__ import annotations

import argparse
import shutil
import tempfile
from pathlib import Path

import yaml


EXTENSIONS = ("goal-lifecycle", "memory-journal", "request-update", "review-candidate")


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


def install(home: Path, source: Path | None = None) -> list[str]:
    source = (source or _repo_root() / "candidate-extensions").resolve()
    home = home.expanduser().resolve()
    if not source.is_dir():
        raise SystemExit(f"candidate extension source does not exist: {source}")
    destination = home / "plugins"
    destination.mkdir(parents=True, exist_ok=True)

    for name in EXTENSIONS:
        src = source / name
        if not (src / "plugin.yaml").is_file() or not (src / "__init__.py").is_file():
            raise SystemExit(f"candidate extension is incomplete: {src}")

    # Copy each extension through a temporary sibling and replace it only after
    # the complete copy succeeds. A failed install therefore leaves the prior
    # installed version intact.
    for name in EXTENSIONS:
        src = source / name
        target = destination / name
        with tempfile.TemporaryDirectory(prefix=f".{name}.", dir=destination) as tmp:
            staged = Path(tmp) / name
            shutil.copytree(src, staged)
            if target.is_symlink() or target.is_file():
                target.unlink()
            elif target.exists():
                shutil.rmtree(target)
            staged.rename(target)

    config_path = home / "config.yaml"
    config: dict = {}
    if config_path.exists():
        loaded = yaml.safe_load(config_path.read_text(encoding="utf-8"))
        if loaded is not None and not isinstance(loaded, dict):
            raise SystemExit(f"profile config must be a mapping: {config_path}")
        config = loaded or {}
    plugins = config.setdefault("plugins", {})
    if not isinstance(plugins, dict):
        raise SystemExit(f"profile plugins config must be a mapping: {config_path}")
    enabled = plugins.setdefault("enabled", [])
    if not isinstance(enabled, list):
        raise SystemExit(f"profile plugins.enabled must be a list: {config_path}")
    for name in EXTENSIONS:
        if name not in enabled:
            enabled.append(name)
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(yaml.safe_dump(config, sort_keys=False), encoding="utf-8")
    return list(EXTENSIONS)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--home", type=Path, required=True, help="Target HERMES_HOME profile")
    parser.add_argument("--source", type=Path, help="Candidate extensions directory")
    args = parser.parse_args()
    names = install(args.home, args.source)
    print(f"installed {', '.join(names)} into {args.home.expanduser().resolve()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
