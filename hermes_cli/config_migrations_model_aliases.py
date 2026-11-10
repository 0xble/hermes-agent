"""Retire fork preset authoring in favour of upstream aliases and model_map.

The public module command supports a read-only dry run against a selected home.
The config registry uses the same transformation during an authorized update.
Only the selected home's mutable routing rows are changed. Transcripts are untouched.
"""
from __future__ import annotations

import argparse
import copy
import json
from pathlib import Path
import re
from typing import Any

_REFERENCE = re.compile(r"\A@([A-Za-z0-9_-]+)\Z")
_ALIAS_FIELDS = ("model", "provider", "base_url", "api_key", "key_env", "reasoning_effort", "service_tier")


def subscription_routes(value: Any, changes: list[str], path: str = "") -> None:
    """Codex labels use the OAuth provider, never an API-key proxy's fallback pool."""
    if isinstance(value, list):
        for i, item in enumerate(value):
            subscription_routes(item, changes, f"{path}[{i}]")
    elif isinstance(value, dict):
        model = str(value.get("model") or value.get("default") or "")
        if value.get("provider") == "custom:openai-proxy" and model.startswith("gpt-"):
            value["provider"] = "openai-codex"
            for key in ("base_url", "api_key", "key_env", "api_mode"):
                value.pop(key, None)
            changes.append(f"{path}: Codex subscription OAuth, API-key proxy removed")
        for key, item in value.items():
            if key != "backends":
                subscription_routes(item, changes, f"{path}.{key}" if path else key)


def rewrite_references(value: Any, aliases: dict, changes: list[str], path: str = "") -> Any:
    """Rewrite exact old route references, leaving prompts and unrelated text alone."""
    if isinstance(value, list):
        result = copy.deepcopy(value)
        for i, item in enumerate(value):
            result[i] = rewrite_references(item, aliases, changes, f"{path}[{i}]")
        return result
    if not isinstance(value, dict):
        return value
    result = copy.deepcopy(value)
    for key, item in value.items():
        here = f"{path}.{key}" if path else key
        if key == "model_preset":
            name = str(item).strip().lower()
            if name not in aliases:
                raise ValueError(f"{here}: unknown model alias {name!r}")
            result.pop(key)
            result["model"] = name
            changes.append(f"{here} -> model: {name}")
        elif key in {"model", "default", "model_role"} and isinstance(item, str) and (m := _REFERENCE.fullmatch(item)):
            name = m[1].lower()
            if name not in aliases:
                raise ValueError(f"{here}: unknown model alias {name!r}")
            result[key] = name
            changes.append(f"{here}: {item} -> {name}")
        else:
            result[key] = rewrite_references(item, aliases, changes, here)
    if any(isinstance(value.get(k), str) and _REFERENCE.fullmatch(value[k]) for k in ("model", "default")):
        for key in ("provider", "base_url", "api_key", "api_mode"):
            result.pop(key, None)
    # Topic roles already store a bare name. Promote old expanded overrides into that same owner.
    override = value.get("model_override")
    if isinstance(override, dict) and isinstance(override.get("model"), str):
        m = _REFERENCE.fullmatch(override["model"])
        if m:
            result["model_role"] = m[1].lower()
            result["model_override"] = None
            changes.append(f"{path}.model_override -> model_role: {m[1].lower()} (resolve on each request)")
    return result


def _migrate_aliases(cfg: dict, changes: list[str]) -> dict:
    presets = cfg.pop("model_presets", {}) or {}
    aliases = cfg.setdefault("model_aliases", {})
    if not isinstance(aliases, dict) or not isinstance(presets, dict):
        raise ValueError("model_aliases and the retiring model_presets must be mappings")
    for name, entry in presets.items():
        name = name.strip().lower()
        if not isinstance(entry, dict) or not entry.get("model"):
            raise ValueError(f"model_presets.{name} has no model")
        route = {k: copy.deepcopy(entry[k]) for k in _ALIAS_FIELDS if k in entry}
        if name in aliases and any(aliases[name].get(k) != route.get(k) for k in ("provider", "model")):
            raise ValueError(f"model_aliases.{name} conflicts with the retiring preset")
        aliases[name] = {**route, **aliases.get(name, {})}
        changes.append(f"model_presets.{name} -> model_aliases.{name}")
    model = cfg.get("model") or {}
    attended = model.get("attended") if isinstance(model, dict) else None
    if isinstance(attended, dict) and attended.get("model") and "attended" not in aliases:
        aliases["attended"] = {k: copy.deepcopy(attended[k]) for k in _ALIAS_FIELDS if k in attended}
        changes.append("model.attended -> model_aliases.attended (stored references)")
    return aliases


def _migrate_delegation(delegation: dict, changes: list[str]) -> bool:
    retired = False
    for key in ("by_parent", "inherit_parent_aliases"):
        if key in delegation:
            delegation.pop(key)
            changes.append(f"delegation.{key}: removed without replacement")
            retired = True
    if delegation.get("subagents"):
        raise ValueError("Named custom subagents need explicit model_map descriptions before migration")
    delegation.pop("subagents", None)
    if delegation.pop("model_preset", None):
        retired = True
    return retired


def migrate_document(config: dict) -> tuple[dict, list[str]]:
    cfg = copy.deepcopy(config)
    changes: list[str] = []
    retiring_presets = "model_presets" in cfg
    aliases = _migrate_aliases(cfg, changes)
    if retiring_presets:
        subscription_routes(cfg, changes)
    delegation = cfg.get("delegation") or {}
    if not isinstance(delegation, dict):
        raise ValueError("delegation must be a mapping")
    retired = _migrate_delegation(delegation, changes)
    if delegation or retired:
        cfg["delegation"] = delegation
    if retired:
        from hermes_cli.config_migrations_delegation_roles import install_role_routes
        install_role_routes(cfg, aliases, changes)
    settings = delegation.get("backends", {}).get("model_map", {})
    for option in settings.get("options", []):
        alias = aliases.get(option.get("model"))
        if isinstance(alias, dict) and option.get("provider") != alias.get("provider"):
            option["provider"] = alias["provider"]
            changes.append(f"model_map.{option.get('name')}: provider follows model_aliases")
    cfg = rewrite_references(cfg, aliases, changes)
    if isinstance(cfg.get("model"), dict) and cfg["model"].get("provider") == "openai-codex" and cfg.get("fallback_providers"):
        cfg["fallback_providers"] = []
        changes.append("fallback_providers: [] (Codex subscription only)")
    return cfg, changes


def migrate_stores(home: Path, aliases: dict, *, dry_run: bool) -> list[str]:
    from utils import atomic_json_write
    changes = []
    pending = []
    for relative in ("cron/jobs.json", "sessions/sessions.json"):
        path = home / relative
        if not path.is_file():
            continue
        raw = json.loads(path.read_text(encoding="utf-8-sig"))
        updated = rewrite_references(raw, aliases, changes, relative)
        if updated != raw:
            pending.append((path, updated))
    from hermes_cli.config_migrations_alias_routing import plan_routing_migration, apply_routing_migration
    routing = plan_routing_migration(home, aliases, changes)
    if not dry_run:
        apply_routing_migration(home, routing)
        for path, updated in pending:
            if path.name == "jobs.json":
                from cron.jobs import save_jobs_document, use_cron_store
                with use_cron_store(home):
                    save_jobs_document(updated)
            else:
                atomic_json_write(path, updated)
    return changes


def migrate_to_54(results: dict, quiet: bool) -> None:
    from hermes_cli.config_migrations import _local_files_allowed, _persist_migration, read_raw_config
    from hermes_constants import get_hermes_home
    before = read_raw_config()
    after, changes = migrate_document(before)
    if _local_files_allowed():
        changes += migrate_stores(Path(get_hermes_home()), after.get("model_aliases", {}), dry_run=False)
    if before != after:
        _persist_migration(after)
    results["config_added"].extend(changes)
    if changes and not quiet:
        print("  Migrated fork presets to upstream model_aliases and advanced-delegation model_map.")


def main(argv=None) -> int:
    from hermes_constants import get_hermes_home, set_hermes_home_override, reset_hermes_home_override
    from hermes_cli.config import atomic_config_replace
    import hermes_yaml as yaml
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--home", type=Path, default=Path(get_hermes_home()))
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--codex-auth-source", type=Path,
                        help="Import a currently valid cached Codex OAuth grant into the selected home without refreshing it")
    args = parser.parse_args(argv)
    token = set_hermes_home_override(str(args.home.resolve()))
    try:
        path = args.home / "config.yaml"
        before = yaml.safe_load(path.read_text(encoding="utf-8-sig")) or {}
        after, changes = migrate_document(before)
        if args.codex_auth_source:
            import time
            from hermes_cli.auth_constants import _decode_jwt_claims
            grant = json.loads(args.codex_auth_source.read_text(encoding="utf-8-sig"))
            if (grant.get("type") != "codex" or grant.get("disabled")
                    or not grant.get("refresh_token")
                    or _decode_jwt_claims(grant.get("access_token", "")).get("exp", 0) < time.time() + 3600):
                raise ValueError("Codex source must contain an enabled OAuth grant valid for at least 1 hour, no refresh attempted")
            if not args.dry_run:
                from hermes_cli.auth_codex import _save_codex_tokens
                _save_codex_tokens({k: grant[k] for k in ("access_token", "refresh_token", "id_token") if k in grant},
                                   set_active=False, write_through=False)
            changes.append("Imported cached Codex subscription grant into selected home, no refresh or source write")
        changes += migrate_stores(args.home, after.get("model_aliases", {}), dry_run=args.dry_run)
        if not args.dry_run and before != after:
            atomic_config_replace(path, after)
        print("DRY RUN" if args.dry_run else "APPLIED")
        print("\n".join(changes) if changes else "No changes.")
        return 0
    finally:
        reset_hermes_home_override(token)


if __name__ == "__main__":
    raise SystemExit(main())
