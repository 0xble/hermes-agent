"""1Password (`op` CLI) secret source.

Users map env-var names to ``op://vault/item/field`` references in
``secrets.onepassword.env``; refs are resolved in one ``op run`` batch with
per-reference ``op read`` fallback using whatever auth the user's ``op`` already has (``OP_SERVICE_ACCOUNT_TOKEN``
headless, ``OP_SESSION_*`` interactive) — Hermes never authenticates on the
user's behalf, and failures never block startup. Complete pulls are cached
in-process and under ``<hermes_home>/cache/op_cache.json`` (values only; auth
material is fingerprinted, never stored).
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess  # noqa: F401 — tests monkeypatch ``op.subprocess.run``
import sys
import tempfile
import time
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from agent.secret_sources._cache import (
    CachedFetch, SecretCache, atomic_write_json, fingerprint as _fingerprint, resolve_cache_home,
)
from agent.secret_sources.base import (
    ErrorKind, FetchResult, SecretSource, classify_cli_error, coerce_float,
    get_source_environment, is_valid_env_name, run_cli,
)

logger = logging.getLogger(__name__)

_OP_RUN_TIMEOUT = 30

# `op` itself reads OP_SERVICE_ACCOUNT_TOKEN; `service_account_token_env` lets
# the user source it from another name, and _op_child_env normalizes it back.
_DEFAULT_TOKEN_ENV = "OP_SERVICE_ACCOUNT_TOKEN"

# Minimal allowlisted child env (never the full post-dotenv os.environ, which
# holds every provider credential). OP_SESSION_* and the token are added
# dynamically in _op_child_env().
_OP_ENV_ALLOWLIST = (
    "PATH", "HOME", "USERPROFILE", "APPDATA", "LOCALAPPDATA", "SystemRoot",
    "TMPDIR", "TMP", "TEMP", "XDG_CONFIG_HOME", "XDG_RUNTIME_DIR", "OP_CONFIG_DIR",
    "OP_ACCOUNT", "OP_CONNECT_HOST", "OP_CONNECT_TOKEN",
    # Lets a user skip op's desktop-app integration probe (which can hang with
    # no timeout on a wedged desktop container) and go straight to token auth.
    "OP_LOAD_DESKTOP_APP_SETTINGS",
)

# L1 key folds in str(home_path) so a HERMES_HOME switch inside one long-lived
# process (the gateway) can't return another profile's secrets. The disk key
# omits home because the file already lives under <home>/cache/.
_CacheKey = Tuple[str, str, str, str]  # (auth_fp, account, home, refs_fp)
_DISK_CACHE_BASENAME = "op_cache.json"


def _disk_key_str(cache_key: _CacheKey) -> str:
    auth_fp, account, _home, refs_fp = cache_key
    return f"{auth_fp}|{account}|{refs_fp}"


_STORE: SecretCache[_CacheKey] = SecretCache(_DISK_CACHE_BASENAME, key_serializer=_disk_key_str)
_CACHE = _STORE.memory  # tests flush L1 directly

_MISSING_BINARY_HINT = (
    "Install the 1Password CLI (https://developer.1password.com/docs/cli/get-started/) "
    "or set secrets.onepassword.binary_path."
)

# First matching rule wins.
_OP_ERROR_RULES = (
    (ErrorKind.TIMEOUT, ("timed out",)),
    (ErrorKind.BINARY_MISSING, ("not found on path", "not an executable", "failed to invoke")),
    (ErrorKind.AUTH_FAILED, ("unauthorized", "not signed in", "session expired",
                             "authentication", "401", "403")),
    # After AUTH_FAILED: a message naming both is a rejection, which must evict rather than
    # fall back to the last good values (_STALE_OK_KINDS).
    (ErrorKind.RATE_LIMITED, ("too many requests", "rate-limited", "rate limited")),
    (ErrorKind.EMPTY_VALUE, ("empty value",)),
    (ErrorKind.NETWORK, ("network", "connection", "resolve host", "dns", "no such host", "dial tcp")),
)


def _classify_op_error(message: str) -> ErrorKind:
    return classify_cli_error(message, _OP_ERROR_RULES)


# Kinds that mean the credential itself was refused. Whether that refusal is about the
# IDENTITY or about one ITEM is decided by scope at the call site, not by the kind: `op`
# reports a revoked token and an item the token may not read with the same wording.
# Slow or unreachable backends are a different class and invalidate nothing entirely
# (see the ErrorKind docstring in ``base``).
_AUTH_ERROR_KINDS = frozenset({ErrorKind.AUTH_FAILED, ErrorKind.AUTH_EXPIRED})
# Failures that say nothing about the values already resolved: serving the last good value
# for them keeps startup working through an outage or an exhausted account quota.
_STALE_OK_KINDS = frozenset({ErrorKind.NETWORK, ErrorKind.TIMEOUT, ErrorKind.RATE_LIMITED})


def _validate_references(references: Optional[Dict[str, str]]) -> Tuple[Dict[str, str], List[str]]:
    """``(valid_refs, warnings)``: keep valid env names bound to stripped ``op://`` strings."""
    valid: Dict[str, str] = {}
    warnings: List[str] = []
    for name, ref in (references or {}).items():
        if not is_valid_env_name(name):
            warnings.append(f"Skipping {name!r}: not a valid env-var name")
        elif not isinstance(ref, str):
            warnings.append(f"Skipping {name!r}: reference is not a string")
        elif not ref.strip().startswith("op://"):
            warnings.append(f"Skipping {name!r}: {ref!r} is not an op:// secret reference")
        else:
            valid[name] = ref.strip()
    return valid, warnings


def _auth_fingerprint(token_env: str) -> str:
    """SHA-256 prefix over everything `op` would authenticate with (token, account,
    Connect host/token, ``OP_SESSION_*``), so a new identity never sees old cached values."""
    source_env = get_source_environment()
    parts: List[str] = [f"{label}={source_env.get(var, '')}" for label, var in (
        ("token", token_env), ("account", "OP_ACCOUNT"),
        ("connect_host", "OP_CONNECT_HOST"), ("connect_token", "OP_CONNECT_TOKEN"))]
    parts += [f"{key}={source_env[key]}" for key in sorted(source_env) if key.startswith("OP_SESSION_")]
    return _fingerprint("\n".join(parts))


def _refs_fingerprint(references: Dict[str, str]) -> str:
    return _fingerprint("\n".join(f"{name}={references[name]}" for name in sorted(references)))


# A 429 from a service account means the identity's hourly budget or the account's shared
# daily budget is spent. Neither refills in seconds, and 1Password does not document whether
# refused requests are counted, so later processes hold off instead of re-probing on every start.
_RATE_LIMIT_COOLDOWN_SECONDS = 15 * 60
_COOLDOWN_BASENAME = "op_rate_limit.json"


def _cooldown_key(auth_fp: str, account: str) -> str:
    # Scoped to the identity (token fingerprint + account), not the reference set: the quota
    # belongs to the token, so a different env map under the same token is equally refused.
    return _fingerprint(f"{auth_fp}|{account}")


def _cooldown_path(home_path: Optional[Path] = None) -> Path:
    return resolve_cache_home(home_path) / "cache" / _COOLDOWN_BASENAME


def _read_cooldowns(home_path: Optional[Path]) -> Dict[str, float]:
    try:
        payload = json.loads(_cooldown_path(home_path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(payload, dict):
        return {}
    return {k: float(v) for k, v in payload.items() if isinstance(k, str) and isinstance(v, (int, float))}


def _rate_limit_cooldown_active(key: str, home_path: Optional[Path]) -> bool:
    until = _read_cooldowns(home_path).get(key)
    return until is not None and time.time() < until


def _record_rate_limit_cooldown(key: str, home_path: Optional[Path]) -> None:
    now = time.time()
    # Keep other identities' live cooldowns; drop expired ones so the file stays small.
    cooldowns = {k: v for k, v in _read_cooldowns(home_path).items() if v > now}
    cooldowns[key] = now + _RATE_LIMIT_COOLDOWN_SECONDS
    try:
        atomic_write_json(_cooldown_path(home_path), cooldowns)
    except OSError:
        pass  # best-effort: without the marker the next start just probes once and stops


def find_op(binary_path: str = "") -> Optional[Path]:
    """Resolve a usable ``op`` binary, or None. A pinned ``binary_path`` is used
    verbatim — pinned-but-missing returns None rather than falling back to PATH."""
    found = binary_path or shutil.which("op")
    if not found or (binary_path and not os.access(binary_path, os.X_OK)):
        return None
    return Path(found)


def _scrub(text: str) -> str:
    """Full ECMA-48 ANSI strip (so a control sequence can't hide text after a redaction marker) + trim."""
    from tools.ansi_strip import strip_ansi

    return strip_ansi(text).replace("\x1b", "").strip()


def _op_child_env(token_value: str) -> Dict[str, str]:
    source_env = get_source_environment()
    env = {k: source_env[k] for k in _OP_ENV_ALLOWLIST if k in source_env}
    env.update((k, v) for k, v in source_env.items() if k.startswith("OP_SESSION_"))
    if token_value:
        env["OP_SERVICE_ACCOUNT_TOKEN"] = token_value
    env["NO_COLOR"] = "1"
    return env


def _run_op_batch(op: Path, references: Dict[str, str], *, account: str = "",
                  token_value: str = "") -> Dict[str, str]:
    """Resolve unique refs in one op invocation, never putting values on stdout.

    The temporary directory is private; both files are 0600 and removed on every
    path. The child receives only synthetic env keys, not Hermes's other secrets.
    """
    if any("\n" in ref or "\r" in ref for ref in references.values()):
        raise RuntimeError("op run cannot encode a newline in a secret reference")
    unique = list(dict.fromkeys(references.values()))
    names = [f"HERMES_OP_BATCH_{i}" for i in range(len(unique))]
    scratch = Path(os.environ.get("TMPDIR") or tempfile.gettempdir())
    with tempfile.TemporaryDirectory(prefix="op_batch_", dir=scratch) as directory:
        env_file = Path(directory) / "refs.env"
        output = Path(directory) / "resolved.json"
        for path in (env_file, output):
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                if path == env_file:
                    stream.write("".join(f"{name}={ref}\n" for name, ref in zip(names, unique)))
        # No stdout/stderr values: op run masks those streams, but a JSON file
        # preserves multiline and arbitrary printable secret content unchanged.
        child = (
            "import json, os, sys; "
            "names = sys.argv[2:]; "
            "values = {n: os.environ[n] for n in names}; "
            "f = open(sys.argv[1], 'w', encoding='utf-8'); "
            "json.dump(values, f); f.close()"
        )
        cmd = [str(op), "run", "--env-file", str(env_file)]
        if account:
            cmd += ["--account", account]
        cmd += ["--", sys.executable, "-c", child, str(output), *names]
        proc = run_cli(cmd, env=_op_child_env(token_value), timeout=_OP_RUN_TIMEOUT,
                       label="op", timeout_message=f"op run timed out after {_OP_RUN_TIMEOUT}s", stdin=None)
        if proc.returncode != 0:
            err = _scrub(proc.stderr or "")[-300:]
            raise RuntimeError(f"op run failed: {err or f'exited {proc.returncode}'}")
        try:
            payload = json.loads(output.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise RuntimeError("op run did not produce a valid result") from exc
        if not isinstance(payload, dict) or any(
            not isinstance(payload.get(name), str) or not payload[name].strip() for name in names
        ):
            raise RuntimeError("op run returned an empty or incomplete result")
        values = dict(zip(unique, (payload[name] for name in names)))
        return {name: values[ref] for name, ref in references.items()}


def _run_op_read(op: Path, reference: str, *, account: str = "", token_value: str = "") -> str:
    """Resolve one ``op://`` reference; raises ``RuntimeError`` on any failure, including
    an exit-0 empty value (applying it would clobber a good credential with ``""``)."""
    cmd: List[str] = [str(op), "read"]
    if account:
        cmd += ["--account", account]
    cmd += ["--", reference]  # `--` so a reference can never parse as an op flag

    proc = run_cli(cmd, env=_op_child_env(token_value), timeout=_OP_RUN_TIMEOUT, label="op",
                   timeout_message=f"op read timed out after {_OP_RUN_TIMEOUT}s for {reference!r}", stdin=None)

    if proc.returncode != 0:
        err = _scrub(proc.stderr or "").strip()
        # op puts the reason last ("could not read secret '<ref>': could not get item
        # <vault>/<item>: Too many requests…"); a long item path pushes it past a head cut,
        # and an unclassifiable reason disables the last-good fallback for the whole pull.
        if len(err) > 300:
            err = "…" + err[-300:]
        if err:
            raise RuntimeError(f"op read failed for {reference!r}: {err}")
        raise RuntimeError(f"op read exited {proc.returncode} for {reference!r}")

    # Strip only op's trailing newline so intentional edge spaces survive.
    value = (proc.stdout or "").rstrip("\r\n")
    if not value.strip():
        raise RuntimeError(f"op read returned an empty value for {reference!r}")
    return value


def fetch_onepassword_secrets(
    *, references: Dict[str, str], account: str = "", token_env: str = _DEFAULT_TOKEN_ENV,
    binary: Optional[Path] = None, binary_path: str = "", use_cache: bool = True,
    cache_ttl_seconds: float = 300, home_path: Optional[Path] = None,
) -> Tuple[Dict[str, str], List[str]]:
    """Resolve ``references`` (name → ``op://…``) to ``(secrets, warnings)``.

    Raises ``RuntimeError`` only when no ``op`` binary is available; per-ref
    failures become warnings. Only *resolved* values are cached: a reference that
    failed is simply absent from the entry, so it is retried on the next call
    rather than frozen in for the whole TTL window. A partial entry is still
    reused for the references it does cover, so one flaky reference cannot force
    a full cold pull of every reference on each startup.
    """
    valid, warnings = _validate_references(references)
    if not valid:
        return {}, warnings

    token_value = get_source_environment().get(token_env, "").strip()
    cache_key: _CacheKey = (_auth_fingerprint(token_env), account or "",
                            str(home_path) if home_path is not None else "", _refs_fingerprint(valid))

    # Values carried over from a fresh-but-incomplete cache entry, and the fetch time
    # they were recorded under (reused so carried-over values never get a fresh lease).
    prefetched: Dict[str, str] = {}
    prefetched_at: Optional[float] = None
    if use_cache:
        cached = _STORE.lookup(cache_key, cache_ttl_seconds, home_path)
        if cached is not None:
            if all(name in cached.secrets for name in valid):
                return dict(cached.secrets), warnings
            prefetched = {n: v for n, v in cached.secrets.items() if n in valid}
            prefetched_at = cached.fetched_at

    op = binary or find_op(binary_path)
    if op is None:
        raise RuntimeError("op CLI not found.  Install the 1Password CLI "
                           "(https://developer.1password.com/docs/cli/get-started/) or set "
                           "secrets.onepassword.binary_path to its absolute location.")

    secrets: Dict[str, str] = dict(prefetched)
    failure_kinds: List[ErrorKind] = []
    cooldown_key = _cooldown_key(cache_key[0], cache_key[1])
    if use_cache and _rate_limit_cooldown_active(cooldown_key, home_path):
        # Another process was refused within the cooldown: every read now would be refused
        # too, and may still count against the quota. Go straight to the last-good path.
        failure_kinds.append(ErrorKind.RATE_LIMITED)
        warnings.append("1Password rate-limit cooldown active; skipping live reads")
    else:
        pending = {name: valid[name] for name in sorted(valid) if name not in secrets}
        if pending:
            try:
                secrets.update(_run_op_batch(op, pending, account=account, token_value=token_value))
            except (RuntimeError, OSError) as exc:
                kind = _classify_op_error(str(exc))
                if kind is ErrorKind.RATE_LIMITED:
                    # A 429 is identity-wide. Do not spend another request on fallback.
                    warnings.append(str(exc))
                    failure_kinds.append(kind)
                    if use_cache:
                        _record_rate_limit_cooldown(cooldown_key, home_path)
                else:
                    # One invalid ref makes op run fail wholesale; isolate the failure
                    # with the existing per-ref path and preserve partial cache semantics.
                    for name, ref in pending.items():
                        try:
                            secrets[name] = _run_op_read(op, ref, account=account, token_value=token_value)
                        except RuntimeError as read_exc:
                            warnings.append(str(read_exc))
                            read_kind = _classify_op_error(str(read_exc))
                            failure_kinds.append(read_kind)
                            if read_kind is ErrorKind.RATE_LIMITED:
                                if use_cache:
                                    _record_rate_limit_cooldown(cooldown_key, home_path)
                                break

    # An IDENTITY rejection fails every read it is asked to make; a single item the
    # identity may not read is a permission on that item, and `op` reports both as
    # "unauthorized"/403. Distinguish them by scope, because the two demand opposite
    # handling: an identity we no longer trust must invalidate everything it ever
    # resolved, while one forbidden item must not stop the other 91 being cached.
    # Requires that NOTHING resolved: no value carried over from cache, no successful read.
    # Scoping this to the re-attempted refs alone inverted it — with 91 refs cached and one
    # forbidden item, the single retry was "every attempted read", so one unreadable item
    # was judged a revoked identity and took all 91 good credentials with it.
    identity_rejected = (
        bool(failure_kinds)
        and all(k in _AUTH_ERROR_KINDS for k in failure_kinds)
        and not secrets
    )

    if identity_rejected:
        # The rejection was observed in THIS call, so continuing to hand back values the
        # rejected identity resolved would be knowingly serving them. Drop the carried-over
        # values and evict the entry rather than letting it age out on its own TTL.
        for name in prefetched:
            secrets.pop(name, None)
        if use_cache:
            # Evict THIS identity's entry only. `_STORE.clear()` would also drop every
            # other home's L1 entry, which a multiplexing gateway holds alongside ours.
            _STORE.memory.pop(cache_key, None)
            _STORE.disk.clear(home_path)
        return secrets, warnings

    stale_used: List[str] = []
    if use_cache and cache_ttl_seconds > 0 and failure_kinds and all(k in _STALE_OK_KINDS for k in failure_kinds):
        # Every failure was transient, so the last good value for a ref is still the
        # best answer. Served for this process only: the stale entry is never re-stored
        # under a fresh timestamp, so the next start retries the backend.
        stale = _STORE.disk.read(cache_key, float("inf"), home_path)
        if stale is not None:
            stale_used = [n for n in valid if n not in secrets and n in stale.secrets]
            for name in stale_used:
                secrets[name] = stale.secrets[name]
            if stale_used:
                age = int(max(0.0, time.time() - stale.fetched_at))
                warnings.append(f"1Password unavailable ({failure_kinds[0].value}); served {len(stale_used)} "
                                f"value(s) from the last good cache ({age}s old)")

    if use_cache and secrets and not stale_used:
        # Age the entry from the oldest value it carries, so reusing a partial entry
        # cannot extend a carried-over value past the configured TTL.
        fetched_at = prefetched_at if prefetched and prefetched_at is not None else time.time()
        _STORE.store(cache_key, CachedFetch(secrets=dict(secrets), fetched_at=fetched_at),
                     cache_ttl_seconds, home_path)

    return secrets, warnings


def _missing_binary_error(binary_path: str) -> str:
    if binary_path:
        return f"secrets.onepassword.binary_path ({binary_path!r}) is not an executable op binary."
    return ("secrets.onepassword.enabled is true but the op CLI was not found on PATH.  Install it "
            "(https://developer.1password.com/docs/cli/get-started/) or set secrets.onepassword.binary_path.")


def apply_onepassword_secrets(
    *, enabled: bool, env: Optional[Dict[str, str]] = None, account: str = "",
    service_account_token_env: str = _DEFAULT_TOKEN_ENV, binary_path: str = "",
    override_existing: bool = True, cache_ttl_seconds: float = 300, home_path: Optional[Path] = None,
) -> FetchResult:
    """Resolve configured ``op://`` references and set them on ``os.environ``
    (``hermes secrets onepassword sync --apply``). Never raises. Refs already
    satisfied by the env (when ``override_existing`` is false) and the token var
    are skipped *before* fetching, so ``op`` never runs for a discarded value."""
    result = FetchResult()
    if not enabled:
        return result

    valid, warnings = _validate_references(env)
    result.warnings.extend(warnings)

    def _guarded(name: str) -> bool:
        """True when ``name`` must not be applied (token var or env already set)."""
        return name == service_account_token_env or (not override_existing and bool(os.environ.get(name)))

    result.skipped.extend(n for n in valid if _guarded(n))
    refs_to_fetch = {n: ref for n, ref in valid.items() if not _guarded(n)}
    if not refs_to_fetch:
        return result

    binary = find_op(binary_path)
    result.binary_path = binary
    if binary is None:
        result.error = _missing_binary_error(binary_path)
        return result

    try:
        secrets, fetch_warnings = fetch_onepassword_secrets(
            references=refs_to_fetch, account=account, token_env=service_account_token_env,
            binary=binary, cache_ttl_seconds=cache_ttl_seconds, home_path=home_path)
    except RuntimeError as exc:
        result.error = str(exc)
        return result

    result.secrets = secrets
    result.warnings.extend(fetch_warnings)
    for name, value in secrets.items():
        if _guarded(name):  # defensive re-check: keys should already be ⊆ refs_to_fetch
            if name not in result.skipped:
                result.skipped.append(name)
            continue
        os.environ[name] = value
        result.applied.append(name)
    return result


class OnePasswordSource(SecretSource):
    """1Password as a registered **mapped** source (explicit per-var bindings, so
    its claims outrank bulk sources on contested vars)."""

    name = "onepassword"
    label = "1Password"
    shape = "mapped"
    scheme = "op"
    token_env_key = "service_account_token_env"
    default_token_env = _DEFAULT_TOKEN_ENV
    # override_existing defaults True: an explicit VAR→op:// binding is the
    # strongest user intent; a stale .env line must not silently defeat it.
    override_existing_default = True
    _AUTH_HINT = ("Run `hermes secrets onepassword token` to paste a fresh service-account token "
                  "({token_env}), or `op signin` for an interactive session.")
    remediation_hints = {ErrorKind.AUTH_FAILED: _AUTH_HINT, ErrorKind.AUTH_EXPIRED: _AUTH_HINT,
                         ErrorKind.BINARY_MISSING: _MISSING_BINARY_HINT}

    def config_schema(self) -> dict:
        return {
            "enabled": {"description": "Master switch", "default": False},
            "env": {"description": "Map of ENV_VAR -> op://vault/item/field reference", "default": {}},
            "account": {"description": "op --account shorthand (empty = default account)", "default": ""},
            "service_account_token_env": {"description": "Env var holding the service-account token "
                                                         "(unset = desktop/interactive session)",
                                          "default": _DEFAULT_TOKEN_ENV},
            "binary_path": {"description": "Pin the op binary (empty = resolve via PATH)", "default": ""},
            "cache_ttl_seconds": {"description": "Disk+memory cache TTL; 0 disables", "default": 300},
            "override_existing": {"description": "Resolved values overwrite .env/shell values", "default": True},
        }

    def fetch(self, cfg: dict, home_path: Path) -> FetchResult:
        cfg = cfg if isinstance(cfg, dict) else {}
        result = FetchResult()

        env_map = cfg.get("env")
        valid, warnings = _validate_references(env_map if isinstance(env_map, dict) else None)
        result.warnings.extend(warnings)
        if not valid:
            if not warnings:
                result.fail("secrets.onepassword.enabled is true but the env: map is "
                            "empty.  Add ENV_VAR: op://vault/item/field entries.", ErrorKind.NOT_CONFIGURED)
            return result

        binary_path = str(cfg.get("binary_path") or "")
        binary = find_op(binary_path)
        result.binary_path = binary
        if binary is None:
            return result.fail(_missing_binary_error(binary_path), ErrorKind.BINARY_MISSING)

        try:
            secrets, fetch_warnings = fetch_onepassword_secrets(
                references=valid, account=str(cfg.get("account") or ""), token_env=self.token_env(cfg),
                binary=binary, cache_ttl_seconds=coerce_float(cfg.get("cache_ttl_seconds", 300), 300.0),
                home_path=home_path)
        except RuntimeError as exc:
            return result.fail(str(exc), _classify_op_error(str(exc)))

        result.secrets = secrets
        result.warnings.extend(fetch_warnings)
        # Per-reference failures are warnings, not a source error, so the resolved values
        # still apply. Surface the worst kind among them: without this a reference that
        # timed out is indistinguishable from one the user never configured.
        missing = [n for n in valid if n not in secrets]
        if missing:
            # EVERY kind, not the worst one. Collapsing to a single kind loses the answer
            # the orchestrator actually needs: a run that times out on the bot token and
            # returns an empty value elsewhere must still count as retryable.
            result.degraded_kinds = frozenset(_classify_op_error(m) for m in fetch_warnings)
        return result


def clear_caches(home_path: Optional[Path] = None) -> None:
    """Drop in-process AND disk caches (after a token rotation, so the next
    startup resolves fresh instead of serving values cached under the old token)."""
    _STORE.clear(home_path)
    try:
        _cooldown_path(home_path).unlink()
    except OSError:
        pass


_reset_cache_for_tests = clear_caches


# ---- BEGIN PLUGIN-COMPAT (revert-scheduled; see COMPAT_MANIFEST.md) ----
# Names external plugins imported from this module before the Sep 2026 decomposition.
# Internal code MUST NOT use these (scripts/check_compat_pointers.py fails CI if it does).
# The whole block is removed by reverting the commit that added it.
import hashlib  # noqa: F401,E402


_PLUGIN_COMPAT_LAZY = {
    'DiskCache': ('agent.secret_sources._cache', 'DiskCache'),
}


def __getattr__(name):  # PEP 562 — lazy so no import cycles
    target = _PLUGIN_COMPAT_LAZY.get(name)
    if target is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    import importlib
    from hermes_cli.plugin_compat import warn_once
    warn_once(__name__, name, *target)
    return getattr(importlib.import_module(target[0]), target[1])
# ---- END PLUGIN-COMPAT ----
