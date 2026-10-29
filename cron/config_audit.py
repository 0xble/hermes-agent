"""Observe config changes over a cron run, without attributing concurrent edits."""
from __future__ import annotations

import hashlib
import logging
from functools import wraps
from pathlib import Path

logger = logging.getLogger(__name__)


def _revision(path: Path) -> tuple:
    try:
        stat = path.stat()
        return (stat.st_mtime_ns, hashlib.sha256(path.read_bytes()).hexdigest())
    except FileNotFoundError:
        return (None, None)
    except OSError as exc:
        return (None, type(exc).__name__)


def _changed_config_warning(job: dict, path: Path, before: tuple, *, failed: bool) -> str | None:
    after = _revision(path)
    if before == after:
        return None
    from hermes_cli.config import require_readable_config_before_write, validate_config_for_write

    invalid = None
    try:
        if not path.exists():
            raise ValueError("config.yaml was removed")
        validate_config_for_write(require_readable_config_before_write(path))
    except Exception as exc:
        invalid = str(exc)
    if not failed and invalid is None:
        return None
    name = str(job.get("name") or job.get("id") or "unnamed")
    job_id = str(job.get("id") or "unknown")
    condition = f"config is invalid: {invalid}" if invalid else "the run failed"
    warning = f"config.yaml changed while cron job '{name}' ({job_id}) ran and {condition}; inspect config.yaml"
    # One check at the end of each run, not one warning per field write or per tick.
    logger.warning("%s (config revisions: %s -> %s)", warning, before, after)
    return warning


def audit_config_changes(run):
    """Cover agent/no-agent runs and early returns at the shared execution boundary."""
    @wraps(run)
    def audited(job, *args, **kwargs):
        if not job.get("no_agent"):
            from gateway.session_cron import current_execution
            if current_execution() is None:
                # The outer API invocation only hands off to the canonical worker.
                # Observe the actual owner, not both sides of that handoff.
                return run(job, *args, **kwargs)
        from hermes_cli.config import get_config_path

        path = get_config_path()
        before = _revision(path)
        try:
            success, output, response, error = run(job, *args, **kwargs)
        except BaseException:
            _changed_config_warning(job, path, before, failed=True)
            raise
        warning = _changed_config_warning(job, path, before, failed=not success)
        if warning is not None:
            # Feed the existing failure/incident delivery lane as well as the operator log.
            # A script's zero exit must not report success after breaking config.yaml.
            return False, f"{output}\n\n{warning}\n", "", f"{error}\n{warning}" if error else warning
        return success, output, response, error
    return audited
