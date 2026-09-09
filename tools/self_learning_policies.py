"""Read-only, profile-local policy resolution for review forks."""


def _config():
    from hermes_cli.config import load_config_readonly
    return load_config_readonly()


def memory_policy():
    try:
        value = _config().get("memory", {}).get("background_policy", "approve_changes")
    except Exception:
        return "approve_changes"
    return value if value in ("automatic", "approve_changes", "observe_only") else "approve_changes"


def skill_mode(task_cfg=None):
    try:
        cfg = task_cfg if task_cfg is not None else _config().get("auxiliary", {}).get("background_review", {})
        value = cfg.get("skill_mode", "direct")
    except Exception:
        return "off"
    return value if value in ("direct", "observe", "off") else "off"
