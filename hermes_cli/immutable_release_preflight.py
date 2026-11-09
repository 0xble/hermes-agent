"""The dependency gate for every immutable pointer transition, including recovery."""
from __future__ import annotations

import json
import logging
import subprocess
import time
from pathlib import Path

_LOG = logging.getLogger(__name__)
_ROLLBACK_OPERATIONS = {"rollback", "restore", "first-migration-rollback"}


# Run under the interpreter PM will actually boot. No Hermes import is needed
# for the extras probe, so an old candidate can still be rejected precisely.
_PROBE = '''
import importlib, importlib.metadata as m, json, sys
out = {}
for name in json.loads(sys.argv[1]):
    try:
        dist = m.distribution(name)
        roots = (dist.read_text('top_level.txt') or '').split()
        if not roots:
            roots = sorted({str(p).split('/')[0].removesuffix('.py') for p in (dist.files or ())
                            if not str(p).startswith(('.', '/')) and
                            '.dist-info' not in str(p) and '.data' not in str(p) and
                            ('/' in str(p) or str(p).endswith('.py'))})
        roots = [root for root in roots if root.isidentifier()]
        normalized = name.replace('-', '_')
        root = normalized if normalized in roots else next(iter(roots), normalized)
        missing = []
        try:
            importlib.import_module(root)
        except Exception as exc:
            missing.append(root + ': ' + str(exc))
        out[name] = {'version': dist.version, 'missing': missing}
    except (ImportError, ValueError) as exc:
        out[name] = {'error': str(exc)}
print(json.dumps(out))
'''
_PROVIDER_PROBE = '''
import json, sys
from plugins.memory import load_memory_provider
errors = []
for name in json.loads(sys.argv[1]):
    provider = load_memory_provider(name, register_skills=False)
    if provider is None:
        errors.append(name + ': configured memory provider could not be loaded')
    elif not provider.is_available():
        reason = getattr(provider, 'unavailable_reason', lambda: '')()
        if reason:
            errors.append(name + ': ' + reason)
print(json.dumps(errors))
'''


def probe_timeout(deadline: float | None) -> float:
    remaining = 60.0 if deadline is None else deadline - time.monotonic()
    if remaining <= 0:
        raise TimeoutError("release activation preflight budget exhausted")
    return min(60.0, remaining)


def _probe(candidate: Path, script: str, values: list[str], home: Path,
           deadline: float | None = None) -> dict | list:
    from hermes_cli import immutable_releases as releases
    from pm.environments import project_python
    env = releases._release_subprocess_env(candidate)
    env["HERMES_HOME"] = str(home)
    try:
        result = subprocess.run(
            [str(project_python(candidate)), "-c", script, json.dumps(values)],
            cwd=candidate, env=env, capture_output=True, text=True, encoding="utf-8", errors="replace",
            check=True, timeout=probe_timeout(deadline))
        return json.loads(result.stdout)
    except (TimeoutError, subprocess.TimeoutExpired) as exc:
        raise TimeoutError("release activation preflight probe budget exhausted") from exc
    except (OSError, subprocess.SubprocessError, ValueError) as exc:
        detail = getattr(exc, "stderr", None) or str(exc)
        raise RuntimeError(
            f"release activation refused: candidate dependency probe failed for {', '.join(values)} "
            f"using {project_python(candidate)}: {detail}. Repair the candidate PM environment and retry.") from exc


def _verify_extras(candidate: Path, previous: Path | None, *, operation: str,
                   deadline: float, carry_deadline: float | None) -> list[str]:
    from hermes_cli import immutable_releases as releases
    from packaging.requirements import Requirement
    from packaging.utils import canonicalize_name
    declared = releases._optional_dependencies(candidate)
    active = releases._recorded_extras(previous) if previous is not None else set()
    removed = active - set(declared)
    if removed:
        _LOG.warning(
            "RELEASE %s: target %s no longer declares active PM extras: %s. "
            "These extras will not be carried. Operator choice: proceed without these features, "
            "or select a release/plugin that provides them.", operation, candidate, ', '.join(sorted(removed)))
    active.intersection_update(declared)
    active.update(releases._committed_extras(previous, candidate, deadline=deadline))
    # From a frozen updater this deliberately does nothing. Readback must refuse
    # activation rather than trusting the skipped sync or the payload's .venv.
    # PM sync has no caller-deadline contract. Budgeted activations must use
    # an already-prepared environment, never leave a repair running after expiry.
    if operation not in _ROLLBACK_OPERATIONS and carry_deadline is None:
        releases.carry_active_extras(candidate, previous)
    probe_timeout(deadline)
    leaves = releases._leaf_requirements(candidate)
    names = sorted({name for extra in active for name in leaves.get(extra, ())})
    if not names:
        return []
    observed = _probe(candidate, _PROBE, names, candidate.parent.parent, deadline)
    if not isinstance(observed, dict):
        raise RuntimeError("candidate dependency probe did not return distribution metadata")
    requirements = [Requirement(raw) for extra in active for raw in declared[extra]]
    errors = []
    for name in names:
        item = observed.get(name, {})
        if not item.get("version") or item.get("missing"):
            errors.append(f"{name} missing or not importable")
        elif any(req.specifier and item["version"] not in req.specifier
                 for req in requirements if canonicalize_name(req.name) == name
                 and (req.marker is None or req.marker.evaluate())):
            errors.append(f"{name} {item['version']} does not satisfy candidate requirements")
    return [f"active PM extras {', '.join(sorted(active))}: {'; '.join(errors)}"] if errors else []


def activation_preflight(candidate: Path, previous: Path | str | None, home: Path, *,
                         operation: str = "promote", deadline: float | None = None) -> None:
    recovery = operation in _ROLLBACK_OPERATIONS
    carry_deadline = deadline
    deadline = deadline if deadline is not None else time.monotonic() + 60.0
    try:
        _check_activation(candidate, previous, home, operation, deadline, carry_deadline)
        probe_timeout(deadline)
    except (TimeoutError, subprocess.TimeoutExpired):
        if not recovery:
            raise RuntimeError("release activation refused: preflight budget exhausted; prepare the candidate and retry")
        _LOG.warning("RELEASE %s: preflight budget exhausted for %s. Proceeding with recovery "
                     "without complete dependency readback; operator must verify restored features.", operation, candidate)


def _check_activation(candidate: Path, previous: Path | str | None, home: Path,
                      operation: str, deadline: float, carry_deadline: float | None) -> None:
    from pm.plugins_state import dependency_homes, read_home_selection
    errors = _verify_extras(candidate, Path(previous) if previous else None, operation=operation,
                            deadline=deadline, carry_deadline=carry_deadline)
    for profile in dict.fromkeys([home, *dependency_homes()]):
        if errors:
            break
        config = read_home_selection(profile) or {}
        provider = (config.get("memory") or {}).get("provider")
        if provider and provider.strip():
            errors.extend(_probe(candidate, _PROVIDER_PROBE, [provider.strip()], profile, deadline))
    if errors:
        raise RuntimeError(
            f"release activation refused: {'; '.join(errors)}. "
            "Prepare the candidate with its own PM (hermes pm install --extra <extra>), "
            "then retry activation. The current release was not changed.")
