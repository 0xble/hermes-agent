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
    from agent.secret_scope import get_secret_str, load_env_file
    from hermes_cli.config import load_config_readonly
    from hermes_cli.config_providers import _canonical_api_mode, get_compatible_custom_providers
    from hermes_cli.providers import custom_provider_aliases
    from hermes_constants import get_hermes_home

    config = load_config_readonly()
    model = config.get("model") or {}
    requested = str(model.get("provider") or "").strip().lower()
    if requested == "custom":
        entry = model
    else:
        entry = next((e for e in get_compatible_custom_providers(config)
                      if requested in custom_provider_aliases(e["name"], e.get("provider_key", ""))), {})
    mode = _canonical_api_mode(str(entry.get("api_mode") or entry.get("transport") or ""))
    if (not entry or entry.get("key_cmd") or entry.get("extra_headers")
            or mode not in {"", "chat_completions", "codex_responses"}):
        raise ValueError

    key = str(entry.get("api_key") or "").strip()
    key_env = str(entry.get("key_env") or entry.get("api_key_env") or "").strip()
    if not key and key_env:
        key = get_secret_str(key_env) or load_env_file(get_hermes_home() / ".env").get(key_env, "")
    base = str(entry.get("base_url") or "").strip().rstrip("/")
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
