"""Value checks for single config sections (voice, timezone, quoted containers), run by
``hermes_cli.config.validate_config_structure`` for doctor and the startup warnings."""

from typing import Any

from hermes_cli.config_issues import ConfigIssue, _issue


from hermes_cli.fallback_config import (
    MIN_FALLBACK_ENTRIES, MIN_UPSTREAM_FAMILIES, route_identity, upstream_family,
)


def _route_label(route: dict[str, Any]) -> str:
    return f"{route.get('provider') or '?'}:{route.get('model') or '?'}"


def _missing_family_hint(path: str, families: set[str]) -> str:
    known = ("openai", "anthropic", "meta", "xai", "google", "mistral", "deepseek", "alibaba")
    missing = [name for name in known if name not in families]
    examples = ", ".join(missing[: max(1, MIN_UPSTREAM_FAMILIES - len(families))])
    return (f"Fix {path}: add at least {MIN_UPSTREAM_FAMILIES} distinct upstream families; "
            f"present: {', '.join(sorted(families)) or 'none'}; missing examples: {examples or 'another independent family'}")


def _validate_chain(path: str, primary: dict[str, Any], raw_chain: Any,
                    config: dict[str, Any], issues: list[ConfigIssue]) -> None:
    """Validate one ordered fallback map.

    An absent or null chain declares nothing (the route inherits or has no fallback), and an
    explicit [] is the documented opt-out, so only a declared list is held to the bar.
    """
    if raw_chain is None or raw_chain == []:
        return
    if not isinstance(raw_chain, list):
        _issue(issues, "error", f"{path} must be a list of fallback routes",
               f"Set {path} to an ordered list of at least {MIN_FALLBACK_ENTRIES} routes, or [] to explicitly disable fallback")
        return
    if len(raw_chain) < MIN_FALLBACK_ENTRIES:
        _issue(issues, "error", f"{path} needs at least {MIN_FALLBACK_ENTRIES} fallback entries (found {len(raw_chain)})",
               f"Fix {path}: add {MIN_FALLBACK_ENTRIES - len(raw_chain)} more ordered fallback route(s)")
    primary_id = route_identity(primary, config)
    families = {upstream_family(primary, config)}
    for index, route in enumerate(raw_chain):
        if not isinstance(route, dict):
            _issue(issues, "error", f"{path}[{index}] must be a route mapping",
                   f"Replace {path}[{index}] with provider/model fields")
            continue
        if route_identity(route, config) == primary_id:
            _issue(issues, "error", f"{path}[{index}] duplicates the primary route {_route_label(primary)}",
                   f"Remove {path}[{index}] or choose a different provider/model/base_url")
        families.add(upstream_family(route, config))
    if len(families) < MIN_UPSTREAM_FAMILIES:
        _issue(issues, "error", f"{path} spans only {len(families)} upstream families (need {MIN_UPSTREAM_FAMILIES})",
               _missing_family_hint(path, families))


def _main_route(model: Any) -> dict[str, Any]:
    """The main ``model`` setting as a route. Its usual shape is ``{default, provider, base_url}``:
    the model name sits under ``default``, which route identity and family checks do not read."""
    if not isinstance(model, dict):
        return {"model": model}
    return {**model, "model": model.get("default") or model.get("model") or model.get("name")}


def _validate_model_map_options(options: Any, issues: list[ConfigIssue], path: str,
                                config: dict[str, Any] | None = None) -> None:
    if not isinstance(options, list):
        _issue(issues, "error", f"{path}.options must be a list", "Provide ordered model-map options")
        return
    config = config or {}
    for index, option in enumerate(options):
        if not isinstance(option, dict):
            _issue(issues, "error", f"{path}.options[{index}] must be a route mapping", "Provide provider and model fields")
            continue
        _validate_chain(f"{path}.options[{index}].fallback_providers", option,
                        option.get("fallback_providers"), config, issues)


def _validate_model_maps(config: dict[str, Any], issues: list[ConfigIssue]) -> None:
    """Validate model maps plus top-level, cron, delegation, and auxiliary fallback chains."""
    def walk(value: Any, path: str) -> None:
        if isinstance(value, dict):
            for key, child in value.items():
                child_path = f"{path}.{key}" if path else str(key)
                if key == "model_map" and isinstance(child, dict):
                    _validate_model_map_options(child.get("options"), issues, child_path, config)
                walk(child, child_path)
        elif isinstance(value, list):
            for index, child in enumerate(value):
                walk(child, f"{path}[{index}]")
    walk(config, "")

    primary = _main_route(config.get("model"))
    _validate_chain("fallback_providers", primary, config.get("fallback_providers"), config, issues)
    # ``fallback_model`` is the legacy single-route setting; keep accepting it for
    # backwards compatibility. The resilience policy applies to ordered chains
    # (`fallback_providers`), not that legacy scalar.

    cron = config.get("cron")
    if isinstance(cron, dict):
        # Cron runs the main route unless it names its own; the main endpoint belongs to the
        # main provider only, so a cron provider never inherits it.
        cron_provider = cron.get("model_provider") or cron.get("provider")
        cron_primary = {"provider": cron_provider or primary.get("provider"),
                        "model": cron.get("model") or primary.get("model"),
                        "base_url": None if cron_provider else primary.get("base_url")}
        _validate_chain("cron.fallback_providers", cron_primary, cron.get("fallback_providers"), config, issues)

    delegation = config.get("delegation")
    if isinstance(delegation, dict):
        # The delegation-wide chain backs the delegation default route, which is the main route
        # unless delegation names its own. As with cron, a named provider never borrows the
        # main endpoint.
        child_provider = delegation.get("provider")
        child_primary = {"provider": child_provider or primary.get("provider"),
                         "model": delegation.get("model") or primary.get("model"),
                         "base_url": delegation.get("base_url") or (None if child_provider else primary.get("base_url"))}
        _validate_chain("delegation.fallback_providers", child_primary, delegation.get("fallback_providers"),
                        config, issues)

    auxiliary = config.get("auxiliary")
    if isinstance(auxiliary, dict):
        for task, task_cfg in auxiliary.items():
            if not isinstance(task_cfg, dict) or "fallback_chain" not in task_cfg:
                continue
            _validate_chain(f"auxiliary.{task}.fallback_chain", task_cfg, task_cfg.get("fallback_chain"), config, issues)


def _validate_voice(config: dict[str, Any], issues: list[ConfigIssue]) -> None:
    voice_cfg = config.get("voice")
    if isinstance(voice_cfg, dict) and "auto_tts_mode" in voice_cfg:
        auto_tts_mode = voice_cfg.get("auto_tts_mode")
        normalized_mode = auto_tts_mode.strip().lower() if isinstance(auto_tts_mode, str) else None
        if normalized_mode not in {"all", "voice_only"}:
            _issue(issues, "error",
                   f"voice.auto_tts_mode must be 'all' or 'voice_only', got {auto_tts_mode!r}",
                   "Set voice.auto_tts_mode to all (audio with every reply) or "
                   "voice_only (audio only after voice input)")
    if not (isinstance(voice_cfg, dict) and "submit_mode" in voice_cfg):
        return
    submit_mode = voice_cfg.get("submit_mode")
    normalized = submit_mode.strip().lower() if isinstance(submit_mode, str) else None
    if normalized not in {"direct", "draft"}:
        _issue(issues, "error", f"voice.submit_mode must be 'direct' or 'draft', got {submit_mode!r}",
               "Set voice.submit_mode to direct (submit immediately) or draft (edit before sending)")


def _validate_timezone(config: dict[str, Any], issues: list[ConfigIssue]) -> None:
    """``timezone`` must be an IANA name the runtime can load.

    ``hermes_time._get_zoneinfo()`` swallows an invalid name behind a single WARNING in the
    gateway log, then runs the agent clock AND every cron schedule on server-local time.
    Surface it here, where doctor and the startup check both look. Silent when the
    interpreter has no tz database at all (bare Windows without ``tzdata``) — nothing can be
    judged there.
    """
    if "timezone" not in config:
        return
    tz = config.get("timezone")
    hint = ("Use an IANA zone name such as America/New_York or Asia/Tokyo (see "
            "`timedatectl list-timezones`). With an invalid value the agent clock and cron "
            "schedules silently fall back to server-local time. HERMES_TIMEZONE overrides "
            "this key when set.")
    if tz is not None and not isinstance(tz, str):
        _issue(issues, "error", f"timezone must be an IANA zone name string, got {tz!r}", hint)
        return
    if not (isinstance(tz, str) and tz.strip()):
        return
    name = tz.strip()
    try:
        import zoneinfo
        zoneinfo.ZoneInfo("UTC")  # is a tz database available at all?
    except Exception:
        return
    try:
        zoneinfo.ZoneInfo(name)
    except Exception:
        _issue(issues, "error", f"timezone {name!r} is not a valid IANA zone name", hint)


def _container_slots() -> dict[str, str]:
    """Dotted key -> ``"list"``/``"mapping"`` for every slot the schema fixes to a container:
    ``DEFAULT_CONFIG`` (sections included) plus the known-container table for roots it omits."""
    from hermes_cli.config import _KNOWN_CONTAINER_TYPES, DEFAULT_CONFIG  # late: the facade imports this module
    slots: dict[str, str] = {}

    def walk(node: dict[str, Any], prefix: str) -> None:
        for key, value in node.items():
            path = f"{prefix}.{key}" if prefix else key
            if isinstance(value, dict):
                slots[path] = "mapping"
                walk(value, path)
            elif isinstance(value, list):
                slots[path] = "list"

    walk(DEFAULT_CONFIG, "")
    slots.update(_KNOWN_CONTAINER_TYPES)
    return slots


def _validate_quoted_containers(config: dict[str, Any], issues: list[ConfigIssue]) -> None:
    """A container slot holding ONE quoted string (``enabled: '["a","b"]'``) is skipped by every
    isinstance-gated reader while ``config get`` echoes it back, so plugins silently unmount and
    exclusions silently lapse (#83308, #105706). Finding only — the file is never rewritten."""
    import shlex

    import hermes_yaml as yaml
    from hermes_cli.config import _SCALAR_AS_ONE_ITEM_LIST_KEYS, _looks_structured_value, cfg_get
    for key, kind in _container_slots().items():
        # ``parse_config_string_list`` readers accept the quoted form; nothing is ignored there.
        if key in _SCALAR_AS_ONE_ITEM_LIST_KEYS:
            continue
        value = cfg_get(config, *key.split("."))
        if not isinstance(value, str) or not _looks_structured_value(value):
            continue
        try:
            parsed = yaml.safe_load(value)
        except yaml.YAMLError:
            continue
        if isinstance(parsed, (list, dict)):
            _issue(issues, "warning",
                   f"{key} is the quoted string {value!r} — Hermes expects a YAML {kind} here "
                   "and every reader ignores the string",
                   f"Run: hermes config set {key} {shlex.quote(value)}  (stores a real {kind}), "
                   "or remove the quotes in config.yaml")
