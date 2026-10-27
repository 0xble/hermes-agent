#!/usr/bin/env python3
"""One status-only GET for an explicit, unambiguous providers.<key> entry.

Run with the checkout's Python and exactly one config key, never a credential.
This is a dedicated process, not an in-process API or general runtime resolver.
"""

from contextlib import redirect_stderr, redirect_stdout
import json
import logging
import os
from pathlib import Path
import re
import sys
from urllib.parse import unquote, urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def _has_persisted_pool(pool_keys):
    from hermes_constants import get_default_hermes_root, get_hermes_home

    # Auth's recovering loader can copy corruption and return an empty store.
    # Admission must read both stores without recovery, locking, or migrations;
    # only absence is safe to treat as empty. Any other read/parse failure refuses.
    for home in {get_hermes_home(), get_default_hermes_root()}:
        try:
            store = json.loads((home / "auth.json").read_text(encoding="utf-8-sig"))
        except FileNotFoundError:
            continue
        if not isinstance(store, dict):
            raise ValueError
        pool = store.get("credential_pool", {})
        if (not isinstance(pool, dict) or any(
                not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows)
                for rows in pool.values())):
            raise ValueError
        # No merged credential selection is needed: either store's matching pool
        # makes this route unsupported, regardless of profile shadowing.
        if any(pool.get(name) for name in pool_keys):
            return True
    return False


def _endpoint(requested):
    from agent.bedrock_adapter import is_bedrock_openai_base_url
    from agent.credential_pool import custom_provider_pool_key_candidates
    from agent.gemini_native_adapter import is_native_gemini_base_url
    from agent.secret_scope import load_env_file
    from hermes_cli.auth import has_usable_secret
    from hermes_cli.config import is_provider_enabled, load_config_readonly
    from hermes_cli.config_read_errors import FailedConfigRead
    from hermes_cli.config_providers import _canonical_api_mode
    from hermes_cli.providers import custom_provider_aliases, is_actual_route
    from hermes_cli.runtime_provider import _VERTEX_NAMES, _detect_api_mode_for_url, _host_gated_env_key_candidates
    from hermes_cli.runtime_provider_custom import _match_new_style_provider, _opencode_family_for_custom, _shadowed_by_builtin
    from hermes_constants import get_hermes_home

    # CLI startup's process-env-over-dotenv boundary. No general env routing.
    for name, value in load_env_file(get_hermes_home() / ".env").items():
        os.environ.setdefault(name, value)
    config = load_config_readonly()
    if isinstance(config, FailedConfigRead):
        raise ValueError
    providers = config.get("providers") or {}
    entry = providers.get(requested)
    if (not isinstance(entry, dict) or not is_provider_enabled(entry)
            or requested in {"custom", "auto", "moa", *_VERTEX_NAMES}
            or _shadowed_by_builtin(requested)):
        raise ValueError
    aliases = custom_provider_aliases(entry.get("name") or requested, requested)
    # Refuse all overlapping declarations, even identical duplicates. This is
    # admission, not a second implementation of runtime precedence/deduplication.
    declarations = [(key, item) for key, item in providers.items() if key != requested]
    legacy = config.get("custom_providers") or []
    if not isinstance(legacy, list):
        raise ValueError
    declarations += [(item.get("provider_key", ""), item) for item in legacy]
    if any(aliases & custom_provider_aliases(item.get("name") or key, key)
           for key, item in declarations):
        raise ValueError
    key_env = str(entry.get("key_env") or entry.get("api_key_env") or "").strip()
    if any(value and (name.endswith("_BASE_URL") or name in {
            "HERMES_INFERENCE_PROVIDER", "CUSTOM_API_KEY", "OPENAI_API_KEY",
            "OPENROUTER_API_KEY", "OLLAMA_API_KEY",
            } or (name.endswith("_API_KEY") and name != key_env))
           for name, value in os.environ.items()):
        raise ValueError
    mode = _canonical_api_mode(entry.get("api_mode") or entry.get("transport") or "")
    model = config.get("model") or {}
    if (entry.get("key_cmd") or entry.get("extra_headers")
            or mode not in {"", "chat_completions", "codex_responses"}
            or str(model.get("openai_runtime") or "").strip().lower() == "codex_app_server"):
        raise ValueError
    # Reuse the runtime's keyed-entry lookup for URL/key_env/inline precedence.
    # Never call load_pool, select, or the full runtime builder (they can write,
    # refresh credentials, execute key_cmd, and auto-detect local models).
    matched = _match_new_style_provider(requested, {requested: entry})
    # The runtime helper returns the resolved row alone on older forks and
    # ``(row, config_key)`` on the current frozen-route surface. The probe only
    # needs the row, and must remain read-only across both shapes.
    resolved = matched[0] if isinstance(matched, tuple) else matched
    resolved = resolved or {}
    base = str(resolved.get("base_url") or "").strip().rstrip("/")
    key = resolved.get("api_key") or ""
    if ((resolved.get("api_mode") or _detect_api_mode_for_url(base) or "chat_completions")
            not in {"chat_completions", "codex_responses"}
            or is_native_gemini_base_url(base) or is_bedrock_openai_base_url(base)
            or is_actual_route("custom", base) or _opencode_family_for_custom(requested, base)
            or any(_host_gated_env_key_candidates(base, ollama=True))):
        raise ValueError
    # Runtime pool lookup tries the durable provider_key, legacy display slug,
    # then URL matches. Also guard every current/legacy identity and bare custom.
    identities = {*aliases, "custom"}
    pool_keys = set(identities)
    for identity in identities:
        pool_keys.update(custom_provider_pool_key_candidates(base, identity))
    if _has_persisted_pool(pool_keys):
        raise ValueError
    parsed = urlsplit(base)
    if (not has_usable_secret(key) or "\r" in key or "\n" in key or not parsed.hostname
            or parsed.username is not None or parsed.password is not None
            or parsed.query or parsed.fragment or key in unquote(base)
            or (parsed.scheme != "https" and not (
                parsed.scheme == "http" and parsed.hostname in {"localhost", "127.0.0.1", "::1"}))):
        raise ValueError
    parsed.port
    return base + "/models", key


def _emit(message):
    try:
        from agent.redact import redact_for_egress
        message = redact_for_egress(message)
    except Exception:
        message = "Provider probe unavailable (redaction failed)."
    print(message)


def main():
    # HTTP DEBUG logs include headers and reflected reason phrases. This standalone
    # process needs only a status, not logs or config-loader parse-warning excerpts.
    logging.disable(logging.CRITICAL)
    if len(sys.argv) != 2 or not re.fullmatch(r"[a-z][a-z0-9_-]*", sys.argv[1]):
        _emit("Provider probe refused (use exactly one providers.<key> config key; never a credential).")
        return 2
    with open(os.devnull, "w", encoding="utf-8") as sink, redirect_stdout(sink), redirect_stderr(sink):
        try:
            url, key = _endpoint(sys.argv[1])
            import httpx
        except Exception:
            code, message = 2, "Provider probe refused (configuration ambiguous or unsupported; use an unambiguous keyed providers entry)."
        else:
            try:
                with httpx.Client(timeout=5.0, follow_redirects=False, trust_env=False) as client:
                    # Close after headers: even an error/redirect body can reflect a credential.
                    with client.stream("GET", url, headers={"Authorization": f"Bearer {key}"}) as response:
                        status = response.status_code
                code, message = (0 if 200 <= status < 300 else 1), f"Provider probe: HTTP {status}."
            except Exception:
                code, message = 1, "Provider probe failed (network error)."
    _emit(message)
    return code


if __name__ == "__main__":
    sys.exit(main())
