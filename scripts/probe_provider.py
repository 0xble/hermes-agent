#!/usr/bin/env python3
"""One status-only GET to the configured custom OpenAI-compatible provider.

Run with the checkout's Python and no arguments. Credentials are read here,
never accepted on argv. This is a dedicated process, not an in-process API.
"""

from contextlib import redirect_stderr, redirect_stdout
import logging
import os
from pathlib import Path
import sys
from urllib.parse import unquote, urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def _endpoint():
    from agent.credential_pool import custom_provider_pool_key_candidates
    from agent.secret_scope import load_env_file
    from hermes_cli.auth import has_usable_secret, read_credential_pool
    from hermes_cli.config import load_config_readonly
    from hermes_cli.config_providers import _canonical_api_mode, get_compatible_custom_providers
    from hermes_cli.runtime_provider import _get_named_custom_provider, _raise_if_provider_disabled
    from hermes_cli.runtime_provider_custom import _key_env_secret
    from hermes_constants import get_hermes_home

    # Match CLI startup's process-env-over-dotenv boundary before the runtime
    # lookup resolves key_env (including when an inline key is also present).
    for name, value in load_env_file(get_hermes_home() / ".env").items():
        os.environ.setdefault(name, value)
    config = load_config_readonly()
    model = config.get("model") or {}
    requested = str(model.get("provider") or "").strip().lower()
    _raise_if_provider_disabled(requested)
    entry = _get_named_custom_provider(requested) or {}
    if not entry and requested == "custom":
        entry = model
        safety_entry = model
    else:
        # This is the agent's actual read-only endpoint/credential lookup, not
        # the legacy-first picker view. It preserves providers-first routing
        # and key_env-over-inline precedence for keyed providers.
        # Runtime lookup intentionally omits legacy key_cmd and invalid modes;
        # recover safety metadata only, never endpoint/credential selection.
        safety_entry = next((candidate for candidate in get_compatible_custom_providers(config)
                             if (entry.get("provider_key") and candidate.get("provider_key") == entry["provider_key"])
                             or (not entry.get("provider_key") and not candidate.get("provider_key")
                                 and candidate.get("name") == entry.get("name")
                                 and candidate.get("base_url") == entry.get("base_url"))), {})
    mode = _canonical_api_mode(str(safety_entry.get("api_mode") or safety_entry.get("transport") or ""))
    if (not entry or not safety_entry or safety_entry.get("key_cmd") or safety_entry.get("extra_headers")
            or mode not in {"", "chat_completions", "codex_responses"}):
        raise ValueError

    base = str(entry.get("base_url") or "").strip().rstrip("/")
    # Do not call the full runtime builder: load_pool seeds/persists auth.json
    # (agent/credential_pool.py:3087-3119), and select can refresh credentials
    # (:1992-1998). Persisted pools are outside this static diagnostic boundary.
    if any(read_credential_pool(name) for name in custom_provider_pool_key_candidates(base, requested)):
        raise ValueError
    # Mirror only the no-pool named-runtime tail's configured-key candidates
    # (hermes_cli/runtime_provider_custom.py:560-566), using its own key-env
    # reader. Host-derived/fallback credentials remain unsupported/fail-closed.
    inline_key = str(entry.get("api_key") or "").strip()
    key = inline_key if has_usable_secret(inline_key) else _key_env_secret(entry, requested)
    parsed = urlsplit(base)
    if (not key or "\r" in key or "\n" in key or not parsed.hostname
            or parsed.username is not None or parsed.password is not None
            or parsed.query or parsed.fragment or key in unquote(base)
            or (parsed.scheme != "https" and not (
                parsed.scheme == "http" and parsed.hostname in {"localhost", "127.0.0.1", "::1"}))):
        raise ValueError
    # Validate the port before giving a malformed URL to the transport.
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
    if sys.argv[1:]:
        _emit("Provider probe refused (arguments unsupported; use configuration).")
        return 2
    with open(os.devnull, "w", encoding="utf-8") as sink, redirect_stdout(sink), redirect_stderr(sink):
        try:
            url, key = _endpoint()
            import httpx
        except Exception:
            code, message = 2, "Provider probe refused (configuration unsupported or incomplete)."
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
