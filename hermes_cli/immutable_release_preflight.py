"""Refuse dependency regressions on update, keep every recovery probe advisory."""
from __future__ import annotations

import json
import logging
import subprocess
import time
from pathlib import Path

logger = logging.getLogger(__name__)
_ROLLBACK_OPERATIONS = {"rollback", "restore", "first-migration-rollback"}
# Bootout and reload share the caller's overall deadline, never the probe's.
_RELOAD_RESERVE_SECONDS = 15.0


# Run under the interpreter PM will actually boot. No Hermes import is needed
# for the extras probe, so old and new releases can be compared precisely.
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
        errors.append(name + ': ' + (reason or 'provider is unavailable'))
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
            f"dependency probe failed for {', '.join(values)} "
            f"using {project_python(candidate)}: {detail}") from exc


def _verify_extras(candidate: Path, previous: Path | None, *, operation: str,
                   deadline: float, carry_deadline: float | None) -> list[str]:
    from hermes_cli import immutable_releases as releases
    declared = releases._optional_dependencies(candidate)
    active = releases._recorded_extras(previous) if previous is not None else set()
    active.update(releases._committed_extras(previous, previous, deadline=deadline) if previous else ())
    removed = active - set(declared)
    if removed:
        logger.warning(
            "RELEASE %s: target %s no longer declares active PM extras: %s. "
            "These extras will not be carried. Operator choice: proceed without these features, "
            "or select a release/plugin that provides them.", operation, candidate, ', '.join(sorted(removed)))
    active.intersection_update(declared)
    # PM sync has no caller-deadline contract. Budgeted activations must use
    # an already-prepared environment, never leave a repair running after expiry.
    if operation not in _ROLLBACK_OPERATIONS and carry_deadline is None:
        try:
            releases.carry_active_extras(candidate, previous)
        except Exception:
            logger.warning("RELEASE %s: candidate carry failed for %s. Comparing selected environments anyway",
                           operation, candidate, exc_info=True)
    probe_timeout(deadline)
    leaves = releases._leaf_requirements(candidate)
    names = sorted({name for extra in active for name in leaves.get(extra, ())})
    if not names:
        return []
    observed = _probe(candidate, _PROBE, names, candidate.parent.parent, deadline)
    if not isinstance(observed, dict):
        raise RuntimeError("candidate dependency probe did not return distribution metadata")
    origin_leaves = releases._leaf_requirements(previous) if previous else {}
    origin_names = sorted({name for extra in active for name in origin_leaves.get(extra, ())})
    origin = (_probe(previous, _PROBE, origin_names, candidate.parent.parent, deadline)
              if origin_names and operation not in _ROLLBACK_OPERATIONS else {})
    errors = []
    for extra in sorted(active):
        missing = [name for name in sorted(leaves.get(extra, ())) if not _importable(observed, name)]
        if not missing:
            continue
        issue = f"active PM extra {extra}: {', '.join(missing)} missing or not importable"
        if operation not in _ROLLBACK_OPERATIONS and origin_leaves.get(extra) and all(
                _importable(origin, name) for name in origin_leaves[extra]):
            errors.append(issue)
        else:
            logger.warning("RELEASE %s: %s. %s", operation, issue,
                           "Proceeding with recovery" if operation in _ROLLBACK_OPERATIONS
                           else "Origin already degraded or availability unknown. Proceeding with update")
    return errors


def _importable(observed: dict, name: str) -> bool:
    item = observed.get(name, {})
    return bool(item.get("version")) and not item.get("missing") and not item.get("error")


def activation_preflight(candidate: Path, previous: Path | str | None, home: Path, *,
                         operation: str = "promote", deadline: float | None = None) -> None:
    recovery = operation in _ROLLBACK_OPERATIONS
    carry_deadline = deadline
    now = time.monotonic()
    deadline = min(now + 60.0, deadline - _RELOAD_RESERVE_SECONDS) if deadline is not None else now + 60.0
    try:
        errors = _check_activation(candidate, previous, home, operation, deadline, carry_deadline)
    except Exception as exc:
        logger.warning(_probe_failure_message(operation, candidate, exc), exc_info=True)
        return
    if errors and not recovery:
        raise RuntimeError(
            f"release activation refused: {'; '.join(errors)}. "
            f"Please re-run the update so candidate-owned carry rebuilds the candidate environment at {candidate} "
            "with the full applicable extra set. If that keeps failing, repair the candidate environment explicitly. "
            "The current release was not changed.")


def _check_activation(candidate: Path, previous: Path | str | None, home: Path,
                      operation: str, deadline: float, carry_deadline: float | None) -> list[str]:
    from pm.plugins_state import dependency_homes, read_home_selection
    recovery = operation in _ROLLBACK_OPERATIONS
    previous = Path(previous) if previous else None
    errors = []
    try:
        probe_timeout(deadline)
        errors.extend(_verify_extras(candidate, previous, operation=operation,
                                     deadline=deadline, carry_deadline=carry_deadline))
    except Exception as exc:
        logger.warning(_probe_failure_message(operation, candidate, exc), exc_info=True)
    if errors:
        return errors
    for profile in dict.fromkeys([home, *dependency_homes()]):
        try:
            # No more probes once the preflight slice ends, even during recovery.
            probe_timeout(deadline)
        except TimeoutError as exc:
            logger.warning(_probe_failure_message(operation, candidate, exc), exc_info=True)
            break
        try:
            config = read_home_selection(profile) or {}
            provider = (config.get("memory") or {}).get("provider")
            if provider and provider.strip():
                issues = _probe(candidate, _PROVIDER_PROBE, [provider.strip()], profile, deadline)
                if not issues:
                    continue
                if not recovery and previous is not None and not _probe(
                        previous, _PROVIDER_PROBE, [provider.strip()], profile, deadline):
                    errors.extend(issues)
                else:
                    logger.warning("RELEASE %s [profile=%s]: %s. %s", operation, profile, '; '.join(issues),
                                   "Proceeding with recovery" if recovery else
                                   "Origin already degraded or availability unknown. Proceeding with update")
        except Exception as exc:
            logger.warning(_probe_failure_message(operation, candidate, exc), exc_info=True)
    return errors


def _probe_failure_message(operation: str, candidate: Path, exc: Exception) -> str:
    detail = "preflight budget exhausted" if isinstance(exc, (TimeoutError, subprocess.TimeoutExpired)) else str(exc)
    disposition = ("Proceeding with recovery" if operation in _ROLLBACK_OPERATIONS else
                   "Availability comparison incomplete. Proceeding with update")
    return f"RELEASE {operation}: preflight probe failed for {candidate}: {detail}. {disposition}"
