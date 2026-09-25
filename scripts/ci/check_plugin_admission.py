#!/usr/bin/env python3
"""Validate changed catalog entries at their exact pins in disposable clones.

Run with the checkout's Python after setup, in a credentialless integration
sandbox: validation installs third-party dependencies and can execute code.
The caller owns sandboxing and whole-job cancellation. No GitHub API is needed.
"""

from __future__ import annotations

import argparse
import os
from pathlib import Path, PurePosixPath, PureWindowsPath
import re
import signal
import subprocess
import sys
import tempfile

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from validate_plugin_catalog import validate_entry  # noqa: E402
sys.path.insert(0, str(Path(__file__).resolve().parent))
from release_baseline import accepted_release_baseline  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parents[2]


def run(command: list[str], *, cwd: Path, timeout: float = 120) -> str:
    """Run an argv without a shell; stop its descendants when the budget expires."""
    process = subprocess.Popen(
        command, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, encoding="utf-8", errors="replace", start_new_session=os.name == "posix",
    )
    try:
        stdout, stderr = process.communicate(timeout=timeout)
    except (subprocess.TimeoutExpired, KeyboardInterrupt):
        if os.name == "posix":
            try:
                os.killpg(process.pid, signal.SIGKILL)  # windows-footgun: ok (POSIX-only branch)
            except ProcessLookupError:
                pass
        else:
            try:
                subprocess.run(
                    ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                    capture_output=True, timeout=10, check=False,
                )
            finally:
                process.kill()
        process.communicate()
        raise
    if process.returncode:
        raise RuntimeError(f"{command[0]} exited {process.returncode}: {stderr.strip() or stdout.strip()}")
    return stdout


def changed_entries(root: Path, base: str, head: str | None) -> tuple[str | None, list[str]]:
    # Resolve first, so caller refs cannot become options or ambiguous paths.
    base_sha = run(["git", "rev-parse", "--verify", "--end-of-options", f"{base}^{{commit}}"], cwd=root).strip()
    head_sha = run(["git", "rev-parse", "--verify", "--end-of-options", f"{head or 'HEAD'}^{{commit}}"], cwd=root).strip()
    ancestor = run(["git", "merge-base", base_sha, head_sha], cwd=root).strip()
    patterns = ["plugin-catalog/*.yaml", "plugin-catalog/*.yml"]
    names = run([
        "git", "diff", "--no-ext-diff", "--no-textconv", "--no-renames", "--name-only", "-z", "--diff-filter=AMT",
        ancestor, *([head_sha] if head is not None else []), "--", *patterns,
    ], cwd=root)
    if head is None:
        names += run(["git", "ls-files", "--others", "--exclude-standard", "-z", "--", *patterns], cwd=root)
    selected = {name for name in names.split("\0") if name and not name.endswith("/removed.yaml")}
    return head_sha if head is not None else None, sorted(
        name for name in selected if not _matches_release(root, name, head_sha if head is not None else None)
    )


def _matches_release(root: Path, name: str, head_sha: str | None) -> bool:
    """True when the entry is byte-identical to the accepted upstream release.

    A release sync brings upstream-admitted entries, whose pins may no longer be
    fetchable anonymously. Fork CI admits fork changes only.
    """
    release = accepted_release_baseline(root)
    if not release:
        return False
    try:
        released = run(["git", "rev-parse", "--verify", "--quiet", f"{release}:{name}"], cwd=root).strip()
        current = (run(["git", "rev-parse", f"{head_sha}:{name}"], cwd=root) if head_sha
                   else run(["git", "hash-object", "--", name], cwd=root)).strip()
    except RuntimeError:
        return False
    return bool(released) and released == current


def plugin_path(clone: Path, subdir: object) -> Path:
    if subdir is None:
        subdir = ""
    if not isinstance(subdir, str):
        raise ValueError("subdir must be a relative path string")
    path = PurePosixPath(subdir)
    if path.is_absolute() or PureWindowsPath(subdir).drive or "\\" in subdir or ".." in path.parts:
        raise ValueError("subdir must stay inside the cloned repository")
    target = (clone / path).resolve()
    if not target.is_relative_to(clone.resolve()) or not target.is_dir():
        raise ValueError("subdir must resolve to a directory inside the clone")
    # Manifest/dependency validation must not follow a checkout symlink outside
    # its disposable tree, including symlinks nested under the plugin directory.
    for candidate in target.rglob("*"):
        if candidate.is_symlink() and not candidate.resolve().is_relative_to(clone.resolve()):
            raise ValueError(f"plugin symlink escapes clone: {candidate.relative_to(target)}")
    return target


def check_self_updater(plugin: Path) -> None:
    for source in plugin.rglob("*"):
        if source.suffix not in {".js", ".mjs", ".cjs", ".ts"} or not source.is_file():
            continue
        content = source.read_text(encoding="utf-8", errors="replace")
        if re.search(r"releases/latest|raw\.githubusercontent\.com", content) and re.search(
            r"writeTextFile|renamePath|writeFile\(", content,
        ):
            raise ValueError(f"self-updating code in pinned plugin: {source.relative_to(plugin)}")


def admit(
    data: object, *, validator: list[str], temporary_parent: Path | None = None,
    validation_timeout: float = 600,
) -> None:
    errors, _warnings = validate_entry(data)
    if errors:
        raise ValueError("; ".join(errors))
    with tempfile.TemporaryDirectory(prefix="hermes-plugin-admission-", dir=temporary_parent) as temporary:
        root = Path(temporary)
        clone = root / "source"
        run(["git", "clone", "--filter=blob:none", "--no-checkout", "--depth", "1", "--", data["repo"], str(clone)], cwd=root, timeout=300)
        run(["git", "fetch", "--no-tags", "origin", data["sha"]], cwd=clone)
        run(["git", "checkout", "--detach", data["sha"]], cwd=clone)
        actual = run(["git", "rev-parse", "HEAD"], cwd=clone).strip()
        if actual != data["sha"]:
            raise ValueError("checked-out commit does not match catalog pin")
        plugin = plugin_path(clone, data.get("subdir", ""))
        if not any((plugin / name).is_file() for name in ("plugin.yaml", "plugin.yml", "plugin.json")):
            raise ValueError("plugin manifest missing at pinned commit/subdir")
        # Scan before dependency installation, so fetched dependencies cannot
        # obscure which source was admitted or create unrelated matches.
        check_self_updater(plugin)
        run([*validator, "plugins", "validate", "--install-deps", str(plugin)], cwd=REPO_ROOT, timeout=validation_timeout)


def check(root: Path, base: str, head: str | None, *, validator: list[str] | None = None) -> int:
    head_sha, entries = changed_entries(root, base, head)
    failures = 0
    for name in entries:
        try:
            if head_sha is None:
                path = (root / name).resolve()
                if not path.is_relative_to(root.resolve()):
                    raise ValueError("catalog entry escapes the working tree")
                content = path.read_text(encoding="utf-8")
            else:
                content = run(["git", "show", f"{head_sha}:{name}"], cwd=root)
            admit(yaml.safe_load(content), validator=validator or [sys.executable, "-m", "hermes_cli.main"])
            print(f"PASS: {name}", flush=True)
        except (OSError, RuntimeError, ValueError, yaml.YAMLError, subprocess.TimeoutExpired) as error:
            failures += 1
            print(f"FAIL: {name}: {error}", file=sys.stderr, flush=True)
    print(f"Plugin admission ({head_sha or 'working tree'}): {len(entries)} changed entries, {failures} failed", flush=True)
    return int(bool(failures))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", default="origin/main")
    parser.add_argument("--head", help="Inspect a committed revision instead of current working files")
    args = parser.parse_args()
    try:
        return check(REPO_ROOT, args.base, args.head)
    except (OSError, RuntimeError, ValueError, subprocess.TimeoutExpired) as error:
        print(f"Plugin admission failed: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
