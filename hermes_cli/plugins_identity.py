"""Registration-time source identity, captured by the loaders that compile plugin code.

The digest is SHA-256 of source bytes concatenated in relative POSIX-path order. Version and
UTC load time are separate metadata. Lazy imports after register() returns are outside the
snapshot. No disk reads occur when reporting identity. Native, sourceless or already imported
entry points without captured source cannot claim a source identity.
"""

from __future__ import annotations

import hashlib
import sys
import types
from contextlib import contextmanager
from datetime import datetime, timezone
from importlib.machinery import PathFinder, SourceFileLoader
from pathlib import Path
from typing import Any


class PluginSourceLoader(SourceFileLoader):
    """Compile exactly the bytes recorded, bypassing potentially stale bytecode caches."""

    def __init__(self, name: str, path: str, capture: PluginSourceCapture):
        super().__init__(name, path)
        self.capture = capture

    def get_code(self, fullname: str) -> types.CodeType:
        source = self.get_data(self.path)
        code = self.source_to_code(source, self.path)
        relative = Path(self.path).absolute().relative_to(self.capture.root).as_posix()
        self.capture.sources[relative] = source
        return code


class PluginSourceCapture:
    """A temporary finder limited to a directory, or an entry point's package namespace."""

    def __init__(self, module_name: str, root: Path | None = None):
        self.module_name = module_name
        self.directory_plugin = root is not None
        self.root = root.absolute() if root is not None else None
        self.sources: dict[str, bytes] = {}

    def find_spec(self, fullname, path=None, target=None):
        if not self.directory_plugin and fullname != self.module_name and not fullname.startswith(self.module_name + "."):
            return None
        spec = PathFinder.find_spec(fullname, path, target)
        if spec is None or not isinstance(spec.loader, SourceFileLoader) or spec.origin is None:
            return None
        origin = Path(spec.origin).absolute()
        if self.root is None:
            self.root = origin.parent
        if not origin.is_relative_to(self.root):
            return None
        spec.loader = PluginSourceLoader(fullname, spec.origin, self)
        return spec

    def __enter__(self):
        sys.meta_path.insert(0, self)
        return self

    def __exit__(self, *_exc):
        sys.meta_path.remove(self)

    def identity(self, version: str) -> dict[str, Any] | None:
        if not self.sources:
            return None
        digest = hashlib.sha256()
        for path in sorted(self.sources):
            digest.update(self.sources[path])
        return {"digest": digest.hexdigest(), "version": version,
                "loaded_at": datetime.now(timezone.utc).isoformat(), "module_count": len(self.sources)}


@contextmanager
def registration_sources(target):
    """Resume the original loader's capture through register(), including deferred pre-imports."""
    module = target if isinstance(target, types.ModuleType) else sys.modules.get(getattr(target, "__module__", ""))
    loader = getattr(getattr(module, "__spec__", None), "loader", None)
    capture = loader.capture if isinstance(loader, PluginSourceLoader) else None
    if capture is None:
        yield None
    else:
        with capture:
            yield capture


def compact_plugin_rows(manager) -> list[dict[str, Any]]:
    """Reuse the existing plugin entities without discovery, dispatch or filesystem reads."""
    return [{"key": row["key"], "name": row["name"], "code_identity": row["code_identity"]}
            for row in manager.list_plugins()]
