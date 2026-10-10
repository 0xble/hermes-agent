"""1Password Login and Credit Card items as a vault backend (``op`` CLI).

Unlock: ``op signin --raw`` with the master password on stdin (desktop-app
integration or account-level auth) mints an ``OP_SESSION_<account>`` token.
A configured service-account token skips the prompt entirely (headless).
List: ``op item list --categories Login,"Credit Card" --format json`` → title,
urls, username / masked card number. Display listings are reused for 15 minutes and fetched
single-flight. Fill authorization instead resolves exactly one handle with a fresh metadata-only
``op item get <id> --vault <vault-id> --format json``; its verified vault is then used for the
secret read. Cards carry no origin: the browser binds them to the page it is on and the user
confirms that origin per fill.

Additional accounts (``vault.onepassword.accounts``) are separate backend instances
with ``op@<alias>:`` handles. Each authenticates only with its own service-account
token (never Connect, never an interactive session), so a handle can never resolve
under another account's credential. ``browser_account`` pins an account's fills to
one named Camofox browser account.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import subprocess
import threading
import time
from concurrent.futures import Future
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from agent.secret_sources._cache import fingerprint as _fingerprint
from agent.secret_sources.base import run_cli
from agent.secret_sources.onepassword import _OP_ENV_ALLOWLIST, _scrub, find_op
from agent.vault_backends.base import (
    FILL_METADATA_NOT_APPLICABLE,
    FILL_METADATA_TTL_SECONDS,
    LoginBackend,
    MissingCredential,
    UnlockRequired,
    run_with_stdin_secret,
)
from agent.vault_backends import unlock as _unlock
from agent.vault_store import VaultItemMeta, normalize_origin, normalize_otp_secret, totp_now

logger = logging.getLogger(__name__)

_TIMEOUT = 30.0
_DEFAULT_TOKEN_ENV = "OP_SERVICE_ACCOUNT_TOKEN"
_ALIAS_RE = re.compile(r"[a-z0-9][a-z0-9-]{0,31}")
_CATEGORIES = "Login,Credit Card"  # one listing feeds both metadata and the vault selector
# `op item list` output (metadata only, never secrets), keyed per backend, account and
# credential fingerprint. Every `op` call spends the account's daily request quota.
# Display listings (browser_vault_list) reuse an answer for 15 minutes.
_LISTING_TTL_SECONDS = 900.0
_FILL_METADATA_TTL_SECONDS = FILL_METADATA_TTL_SECONDS
_LISTING_CACHE: Dict[Tuple[str, str, str], Tuple[float, str]] = {}
# Single flight: concurrent callers wanting the same listing or item metadata wait on one
# `op` call. Fill authorization does not use the display cache as authority.
_LISTING_INFLIGHT: Dict[Tuple[str, Tuple[str, str, str]], Future] = {}
_ITEM_META_INFLIGHT: Dict[Tuple[Tuple[str, str, str], str], Future] = {}
_LISTING_LOCK = threading.Lock()
_LISTING_GENERATION = [0]  # bumped on invalidation so a fetch already in flight is not stored


class _ItemMetadataRetry(RuntimeError):
    """The item may have moved or disappeared since the cached vault hint was made."""


def invalidate_listing_cache() -> None:
    """Forget every reused listing. Call after anything changes 1Password items."""
    with _LISTING_LOCK:
        _LISTING_CACHE.clear()
        _LISTING_INFLIGHT.clear()
        _ITEM_META_INFLIGHT.clear()
        _LISTING_GENERATION[0] += 1
# A bare "host[.tld][:port][/path]" website. Anything else without "://" (mailto:, user@host,
# javascript:) stays unparseable rather than being coerced into an https origin.
_BARE_HOST_RE = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?)+(?::\d{1,5})?(?:[/?#][^\s]*)?")

# 1Password Credit Card field ids → local-vault PAYMENT_FIELDS keys (agent/vault_store.py).
# ``expiry`` is YYYYMM and is split below; ZIP has no stable id so it is matched by label.
_CARD_FIELD_IDS = {"ccnum": "card_number", "cardholder": "cardholder_name", "cvv": "cvc"}


def _card_secret(fields) -> Dict[str, str]:
    """Map a Credit Card item's fields onto the PAYMENT_FIELDS shape; digits only for the number."""
    out: Dict[str, str] = {}
    for field in fields if isinstance(fields, list) else []:
        if not isinstance(field, dict):
            continue
        value = field.get("value")
        if not isinstance(value, str) or not value.strip():
            continue
        fid, label = str(field.get("id") or ""), str(field.get("label") or "").strip().lower()
        if fid in _CARD_FIELD_IDS:
            out[_CARD_FIELD_IDS[fid]] = "".join(ch for ch in value if ch.isdigit()) if fid == "ccnum" else value.strip()
        elif fid == "expiry":
            digits = "".join(ch for ch in value if ch.isdigit())
            if len(digits) == 6:  # YYYYMM as op emits it
                out["exp_year"], out["exp_month"] = digits[:4], digits[4:]
        elif label in ("zip", "zip code", "postal code", "postcode") and "billing_postal_code" not in out:
            out["billing_postal_code"] = value.strip()
    return out


class OnePasswordLoginBackend(LoginBackend):
    name = "onepassword"
    display_name = "1Password"
    prefix = "op:"
    needs_unlock = True
    binds_cards_to_page = True

    def __init__(self, cfg: Optional[Dict] = None, *, alias: str = ""):
        self.cfg = cfg or {}
        self.alias = alias
        if alias:
            # Instance attributes shadow the class defaults: a distinct name keeps unlock state
            # and diagnostics apart, and a distinct prefix routes handles without a lookup table.
            self.name = f"onepassword@{alias}"
            self.display_name = f"1Password ({alias})"
            self.prefix = f"op@{alias}:"
            self.needs_unlock = False  # service-account only; there is nothing to prompt for
            self.browser_account = str(self.cfg.get("browser_account") or "").strip().lower()
        from agent.secret_scope import get_secret
        self._token_env = str(self.cfg.get("service_account_token_env") or _DEFAULT_TOKEN_ENV)
        self._service_token = get_secret(self._token_env, "") or ""
        # A get_meta call authorizes the browser page, then the same thread's immediately
        # following secret read consumes this item metadata. It is never a general-purpose cache.
        self._fill_metadata = threading.local()

    @classmethod
    def additional_accounts(cls, cfg: Dict) -> List["OnePasswordLoginBackend"]:
        """One backend per valid ``accounts`` entry. An entry must name a unique alias, its
        account, and its OWN token env (never the primary's), so no two accounts can share a
        credential. Invalid entries are skipped with a warning: fail closed, never widen."""
        entries = cfg.get("accounts") or []
        if not isinstance(entries, list):
            logger.warning("Ignoring vault.onepassword.accounts: expected a list")
            return []
        aliases: set = set()
        token_envs = {str(cfg.get("service_account_token_env") or _DEFAULT_TOKEN_ENV)}
        out: List[OnePasswordLoginBackend] = []
        for entry in entries:
            entry = entry if isinstance(entry, dict) else {}
            alias = str(entry.get("alias") or "").strip().lower()
            account = str(entry.get("account") or "").strip()
            token_env = str(entry.get("service_account_token_env") or "").strip()
            if (not _ALIAS_RE.fullmatch(alias) or alias in aliases or not account
                    or not token_env or token_env in token_envs):
                logger.warning("Ignoring vault.onepassword.accounts entry %r: it needs a unique alias "
                               "([a-z0-9-]), an account, and its own service_account_token_env", alias)
                continue
            aliases.add(alias)
            token_envs.add(token_env)
            out.append(cls({"binary_path": cfg.get("binary_path") or "", "account": account,
                            "service_account_token_env": token_env,
                            "browser_account": entry.get("browser_account") or ""}, alias=alias))
        return out

    # ── auth ────────────────────────────────────────────────────────────────

    def _op(self) -> Path:
        op = find_op(str(self.cfg.get("binary_path") or ""))
        if op is None:
            raise RuntimeError("1Password CLI (op) not found — install it or set vault.onepassword.binary_path")
        return op

    def _env(self, session_token: Optional[str]) -> Dict[str, str]:
        from agent.secret_scope import get_secret
        env = {k: os.environ[k] for k in _OP_ENV_ALLOWLIST if k in os.environ and not k.startswith("OP_CONNECT_")}
        # Connect credentials outrank OP_SERVICE_ACCOUNT_TOKEN inside op, so they must come from the
        # profile's own secret scope like the service token does — never from the launch environment.
        # An additional account never gets them: Connect would answer for a different account.
        for k in () if self.alias else ("OP_CONNECT_HOST", "OP_CONNECT_TOKEN"):
            if v := get_secret(k, ""):
                env[k] = v
        env["NO_COLOR"] = "1"
        account = str(self.cfg.get("account") or "")
        if account:
            env["OP_ACCOUNT"] = account
        if self._service_token:
            env["OP_SERVICE_ACCOUNT_TOKEN"] = self._service_token
        elif session_token:
            # op signin --raw prints the bare token; the env var name carries the account shorthand,
            # which op also accepts as plain OP_SESSION for the default account.
            env[f"OP_SESSION_{account}" if account else "OP_SESSION"] = session_token
        return env

    def _connect_credentials(self):
        if self.alias:
            return "", ""
        from agent.secret_scope import get_secret
        host, token = get_secret("OP_CONNECT_HOST", ""), get_secret("OP_CONNECT_TOKEN", "")
        if bool(host) != bool(token):
            raise RuntimeError("1Password Connect requires both host and token")
        return host, token

    def _connect_get(self, path: str):
        # op item list does not support Connect. Use the official read-only API;
        # never leak response bodies, redirected credentials or request exceptions.
        import requests
        from urllib.parse import urlsplit
        host, token = self._connect_credentials()
        if not host or not token:
            raise UnlockRequired(self)
        url = urlsplit(host)
        if (url.username or url.password or url.query or url.fragment or
                not (url.scheme == "https" or (url.scheme == "http" and url.hostname in ("localhost", "127.0.0.1", "::1")))):
            raise RuntimeError("Connect requires HTTPS or loopback HTTP")
        try:
            with requests.Session() as session:
                session.trust_env = False
                response = session.get(host.rstrip("/") + path,
                    headers={"Authorization": "Bearer " + token}, timeout=_TIMEOUT, allow_redirects=False)
                if response.status_code != 200:
                    raise RuntimeError("Connect read refused")
                return response.json()
        except Exception:
            raise RuntimeError("1Password Connect read failed") from None

    @staticmethod
    def _connect_ids(handle: str):
        import re
        match = re.fullmatch(r"op:connect:([a-z0-9]{26}):([a-z0-9]{26})", handle)
        if not match:
            raise ValueError("Invalid Connect login handle")
        return match.groups()

    def _connect_item(self, handle: str, categories=("LOGIN",)):
        vault_id, item_id = self._connect_ids(handle)
        item = self._connect_get(f"/v1/vaults/{vault_id}/items/{item_id}")
        if (not isinstance(item, dict) or item.get("id") != item_id or
                (item.get("vault") or {}).get("id") != vault_id or
                item.get("category") not in categories or item.get("state", "ACTIVE") != "ACTIVE"):
            raise RuntimeError("Connect item identity mismatch")
        return item

    @staticmethod
    def _connect_otp_seed(item) -> Optional[str]:
        """Canonical RFC 6238 seed from a Connect item's one-time-password field, else None.

        The same helper backs both ``has_otp`` (the agent's "codes are minted automatically"
        hint) and ``resolve_otp``, so a stored item is never announced as automatic unless a
        code can actually be minted from it. Every eligible field is examined: an unusable
        candidate (blank, unnormalisable, or one the minter rejects) must not hide a later
        usable seed — the base32 alphabet check alone would accept an alphabet-valid but
        undecodable secret (e.g. "A"), or a period too large for the runtime division.
        """
        for field in item.get("fields", []):
            if field.get("type") != "OTP" and field.get("purpose") != "ONE_TIME_PASSWORD":
                continue
            raw = field.get("value")
            if not isinstance(raw, str) or not raw.strip():
                continue
            try:
                seed = normalize_otp_secret(raw)
                if seed and totp_now(seed):  # fail closed: no mintable seed, no claim
                    return seed
            except Exception:
                continue
        return None

    @staticmethod
    def _connect_meta(item, vault_id):
        handle = f"op:connect:{vault_id}:{item.get('id')}"
        OnePasswordLoginBackend._connect_ids(handle)
        if item.get("category") == "CREDIT_CARD":
            return _card_meta(handle, item, str(item.get("createdAt") or ""),
                              _card_last4(_card_secret(item.get("fields", [])).get("card_number", "")))
        origins = _all_origins([u.get("href", "") for u in item.get("urls", [])])
        if not origins:
            return None
        origin = origins[0]
        username = next((f.get("value") for f in item.get("fields", []) if f.get("purpose") == "USERNAME"), None)
        return VaultItemMeta(id=handle, kind="login", label=str(item.get("title") or origin),
                             has_otp=OnePasswordLoginBackend._connect_otp_seed(item) is not None,
                             origin=origin, created_at=str(item.get("createdAt") or ""),
                             identifier_type="username" if username else None, identifier=username,
                             allowed_origins=_web_origins(origins))

    def is_unlocked(self) -> bool:
        try:
            _, connect_token = self._connect_credentials()
        except RuntimeError:
            return False
        return bool(connect_token) or bool(self._service_token) or _unlock.is_unlocked(self.name)

    def unlock(self, master_password: str) -> None:
        """Mint a session token from the master password (consumed on stdin, never argv)."""
        if self.alias:
            raise MissingCredential(self._missing_token_error())
        generation = _unlock.begin_unlock(self.name)
        cmd = [str(self._op()), "signin", "--raw"]
        if account := str(self.cfg.get("account") or ""):
            cmd += ["--account", account]
        proc = run_with_stdin_secret(cmd, env=self._env(None), secret=master_password, timeout=_TIMEOUT, label="op")
        token = (proc.stdout or "").strip()
        if proc.returncode != 0 or not token:
            raise RuntimeError(f"1Password unlock failed: {_scrub(proc.stderr or '')[:200] or 'no session token'}")
        if not _unlock.store_session_token(self.name, token, generation):
            raise RuntimeError("1Password was locked while unlocking; try again")

    def _missing_token_error(self) -> str:
        return f"{self.display_name} needs its service-account token in {self._token_env}"

    def _run(self, *args: str) -> str:
        if self.alias and not self._service_token:
            raise MissingCredential(self._missing_token_error())
        token = None if self._service_token else _unlock.get_session_token(self.name)
        if not self._service_token and not token:
            raise UnlockRequired(self)
        proc = run_cli([str(self._op()), *args], env=self._env(token), timeout=_TIMEOUT, label="op",
                       timeout_message="op timed out", stdin=subprocess.DEVNULL)
        if proc.returncode != 0:
            err = _scrub(proc.stderr or "")
            if "session" in err.lower() or "sign in" in err.lower() or "not signed in" in err.lower():
                _unlock.lock(self.name)
                raise UnlockRequired(self)
            raise RuntimeError(f"op failed: {err[:200]}")
        return proc.stdout or ""

    def _listing_key(self) -> Tuple[str, str, str]:
        # The credential's fingerprint, so another account, token or session never sees a listing.
        credential = self._service_token or _unlock.get_session_token(self.name) or ""
        return (self.name, str(self.cfg.get("account") or ""), _fingerprint(credential))

    def _item_list_json(self) -> str:
        """Display listing: reuses any listing younger than ``_LISTING_TTL_SECONDS``."""
        return self._shared_listing(_LISTING_TTL_SECONDS)

    def _shared_listing(self, max_age: float) -> str:
        """One display ``op item list`` per credential at a time; concurrent callers share it.
        Fill authorization intentionally has a different path: it reads one item's current
        metadata instead of treating this account-wide, display-oriented cache as authority."""
        key = self._listing_key()
        with _LISTING_LOCK:
            hit = _LISTING_CACHE.get(key)
            if hit and time.monotonic() - hit[0] < max_age:
                return hit[1]
            pending = _LISTING_INFLIGHT.get(("display", key))
            owner = pending is None
            if owner:
                pending = _LISTING_INFLIGHT[("display", key)] = Future()
            generation = _LISTING_GENERATION[0]
        if not owner:
            return pending.result()
        started = time.monotonic()
        try:
            out = self._run("item", "list", "--categories", _CATEGORIES, "--format", "json")
        except BaseException as exc:
            with _LISTING_LOCK:
                if _LISTING_INFLIGHT.get(("display", key)) is pending:
                    del _LISTING_INFLIGHT[("display", key)]
            pending.set_exception(exc)
            raise
        with _LISTING_LOCK:
            if _LISTING_INFLIGHT.get(("display", key)) is pending:
                del _LISTING_INFLIGHT[("display", key)]
            if generation == _LISTING_GENERATION[0]:
                _LISTING_CACHE[key] = (started, out)
        pending.set_result(out)
        return out

    def _list_json_fresh(self) -> str:
        """Explicitly refresh the display listing for a vault hint or origin filter."""
        return self._shared_listing(0.0)

    def _metadata_handle_id(self, handle: str) -> str:
        if not handle.startswith(self.prefix):
            raise ValueError("Invalid 1Password item handle")
        item_id = handle[len(self.prefix):]
        if not item_id or not item_id[0].isalnum() or not all(
            c.isascii() and (c.isalnum() or c == "-") for c in item_id
        ):
            raise ValueError("Invalid 1Password item handle")
        return item_id

    def _listing_vault_hint(self, item_id: str, *, fresh: bool = False) -> Optional[str]:
        """Return the item's vault from the display cache, or one fresh listing when cold."""
        key = self._listing_key()
        raw_text = None
        loaded_fresh = fresh
        if not fresh:
            with _LISTING_LOCK:
                hit = _LISTING_CACHE.get(key)
                if hit and time.monotonic() - hit[0] < _LISTING_TTL_SECONDS:
                    raw_text = hit[1]
        if raw_text is None:
            raw_text = self._list_json_fresh()
            loaded_fresh = True
        try:
            raw = json.loads(raw_text or "[]")
        except (TypeError, ValueError) as exc:
            raise RuntimeError("Invalid 1Password item metadata") from exc
        if not isinstance(raw, list):
            raise RuntimeError("Invalid 1Password item metadata")
        matches = [item for item in raw if isinstance(item, dict) and item.get("id") == item_id]
        if len(matches) != 1:
            if not loaded_fresh:
                return self._listing_vault_hint(item_id, fresh=True)
            return None
        vault = matches[0].get("vault")
        if not isinstance(vault, dict):
            if not loaded_fresh:
                return self._listing_vault_hint(item_id, fresh=True)
            return None
        vault_id = vault.get("id")
        if not isinstance(vault_id, str) or not vault_id:
            if not loaded_fresh:
                return self._listing_vault_hint(item_id, fresh=True)
            return None
        return vault_id

    @staticmethod
    def _metadata_error_is_retryable(exc: BaseException) -> bool:
        text = str(exc).lower()
        return any(term in text for term in (
            "isn't an item",
            "not found",
            "not_found",
            "no item",
            "isn't in vault",
            "could not find",
            "does not exist",
            "item moved",
        ))

    def _read_item_metadata(self, item_id: str, vault_id: str, categories) -> Optional[dict]:
        try:
            raw = json.loads(self._run(
                "item", "get", item_id, "--vault", vault_id, "--format", "json"
            ) or "{}")
        except BaseException as exc:
            if self._metadata_error_is_retryable(exc):
                raise _ItemMetadataRetry(str(exc)) from exc
            raise
        if not isinstance(raw, dict):
            raise RuntimeError("Invalid 1Password item metadata")
        # Never retain or inspect concealed fields from a metadata response, even if a
        # future CLI version includes them without --reveal.
        item = {key: raw[key] for key in (
            "id", "title", "category", "state", "vault", "urls", "created_at",
            "createdAt", "additional_information") if key in raw}
        returned_vault = item.get("vault")
        returned_vault_id = returned_vault.get("id") if isinstance(returned_vault, dict) else None
        if item.get("id") != item_id or returned_vault_id != vault_id:
            raise _ItemMetadataRetry("1Password item identity or vault mismatch")
        if item.get("state", "ACTIVE") != "ACTIVE" or item.get("category") not in categories:
            return None
        return item

    def _fresh_item_metadata(self, handle: str, categories=("LOGIN", "CREDIT_CARD"), *,
                             _retry=True) -> Optional[dict]:
        """Read authoritative metadata for exactly one item, always scoped to a vault.

        The display listing supplies only a vault hint. If that hint is stale because an item
        moved or disappeared, invalidate all listing caches and retry once using a fresh list.
        """
        item_id = self._metadata_handle_id(handle)
        # Any new metadata lookup starts a new operation; do not let an authorization context
        # from an earlier, unrelated handle leak into this fill.
        self._clear_fill_metadata()
        key = (self._listing_key(), item_id)
        with _LISTING_LOCK:
            pending = _ITEM_META_INFLIGHT.get(key)
            owner = pending is None
            if owner:
                pending = _ITEM_META_INFLIGHT[key] = Future()
        if not owner:
            return pending.result()
        try:
            vault_id = self._listing_vault_hint(item_id)
            if vault_id is None:
                item = None
            else:
                try:
                    item = self._read_item_metadata(item_id, vault_id, categories)
                except _ItemMetadataRetry:
                    if not _retry:
                        item = None
                    else:
                        invalidate_listing_cache()
                        fresh_vault_id = self._listing_vault_hint(item_id, fresh=True)
                        if fresh_vault_id:
                            try:
                                item = self._read_item_metadata(item_id, fresh_vault_id, categories)
                            except _ItemMetadataRetry:
                                item = None
                        else:
                            item = None
        except BaseException as exc:
            with _LISTING_LOCK:
                if _ITEM_META_INFLIGHT.get(key) is pending:
                    del _ITEM_META_INFLIGHT[key]
            pending.set_exception(exc)
            raise
        with _LISTING_LOCK:
            if _ITEM_META_INFLIGHT.get(key) is pending:
                del _ITEM_META_INFLIGHT[key]
        pending.set_result(item)
        return item

    def _clear_fill_metadata(self, handle: Optional[str] = None) -> None:
        cached = getattr(self._fill_metadata, "item", None)
        if cached is not None and (handle is None or cached[1] == handle):
            del self._fill_metadata.item

    def discard_fill_metadata(self, handle: Optional[str] = None) -> None:
        """Drop per-fill authorization when a caller will not read a secret."""
        self._clear_fill_metadata(handle)

    def _remember_fill_metadata(self, handle: str, item: dict) -> None:
        self._fill_metadata.item = (time.monotonic(), handle, item)

    def refresh_fill_metadata(self, handle: str) -> object:
        """Refresh stale per-fill metadata without consulting the account-wide listing."""
        if self._connect_credentials()[1]:
            return FILL_METADATA_NOT_APPLICABLE
        cached = getattr(self._fill_metadata, "item", None)
        if cached is None or cached[1] != handle:
            return FILL_METADATA_NOT_APPLICABLE
        stored_at, _handle, old_item = cached
        if time.monotonic() - stored_at <= _FILL_METADATA_TTL_SECONDS:
            return None
        item_id = self._metadata_handle_id(handle)
        vault = old_item.get("vault")
        vault_id = vault.get("id") if isinstance(vault, dict) else None
        if not isinstance(vault_id, str) or not vault_id:
            self._clear_fill_metadata(handle)
            return None
        try:
            item = self._read_item_metadata(item_id, vault_id, ("LOGIN", "CREDIT_CARD"))
        except Exception:
            self._clear_fill_metadata(handle)
            return None
        if item is None or item.get("category") != old_item.get("category"):
            self._clear_fill_metadata(handle)
            return None
        self._remember_fill_metadata(handle, item)
        return self._cli_meta(handle, item)

    # ── backend contract ───────────────────────────────────────────────────
    def list_items(self, *, fresh: bool = False) -> List[VaultItemMeta]:
        self._connect_credentials()  # Misconfigured Connect must never fall back to another route.
        if self.alias and not self._service_token:
            raise MissingCredential(self._missing_token_error())  # surfaced, not an empty account
        if not self.is_unlocked():
            return []
        if self._connect_credentials()[1]:
            out = []
            for vault in self._connect_get("/v1/vaults"):
                vault_id = vault["id"]
                self._connect_ids(f"op:connect:{vault_id}:{vault_id}")
                for item in self._connect_get(f"/v1/vaults/{vault_id}/items"):
                    if item.get("category") in ("LOGIN", "CREDIT_CARD") and item.get("state", "ACTIVE") == "ACTIVE":
                        meta = self._connect_meta(item, vault_id)
                        if meta:
                            out.append(meta)
            return out + self.list_protected_fields()
        raw = json.loads((self._list_json_fresh() if fresh else self._item_list_json()) or "[]")
        out: List[VaultItemMeta] = []
        for item in raw if isinstance(raw, list) else []:
            handle = f"{self.prefix}{item.get('id')}"
            created = str(item.get("created_at") or "")
            if item.get("category") == "CREDIT_CARD":
                # The listing exposes only the masked number ("3767 **** 2009"), never the PAN.
                out.append(_card_meta(handle, item, created, _card_last4(str(item.get("additional_information") or ""))))
                continue
            urls = [str(u["href"]) for u in item.get("urls") or [] if isinstance(u, dict) and u.get("href")]
            origins = _all_origins(urls)
            if not origins:
                continue
            username = str(item.get("additional_information") or "").strip() or None
            out.append(VaultItemMeta(
                id=f"{self.prefix}{item.get('id')}", kind="login", label=str(item.get("title") or origins[0]),
                origin=origins[0], created_at=str(item.get("created_at") or ""),
                identifier_type="username" if username else None, identifier=username,
                allowed_origins=_web_origins(origins)))
        return out + self.list_protected_fields()

    def list_protected_fields(self) -> List[VaultItemMeta]:
        """Configured origin-bound fields as opaque model-facing handles."""
        # Connect cannot resolve arbitrary ``op://`` secret references. Do not
        # advertise handles whose values this backend cannot consume.
        if self._connect_credentials()[1]:
            return []
        configured = self.cfg.get("protected_fields") or []
        out: List[VaultItemMeta] = []
        for entry in configured if isinstance(configured, list) else []:
            normalized = _normalize_protected_field_entry(entry)
            if normalized is None:
                continue
            reference, semantic, value_type, label, origins = normalized
            out.append(VaultItemMeta(
                id=_protected_field_handle(self.prefix, reference, semantic, value_type, origins),
                kind="protected_field", label=label, origin=origins[0], created_at="",
                allowed_origins=tuple(origins), field_token=semantic,
            ))
        return out

    def _cli_meta(self, handle: str, item: dict) -> Optional[VaultItemMeta]:
        category = item.get("category")
        if category == "CREDIT_CARD":
            return _card_meta(handle, item, str(item.get("created_at") or item.get("createdAt") or ""),
                              _card_last4(str(item.get("additional_information") or "")))
        if category != "LOGIN":
            return None
        urls = [str(u["href"]) for u in item.get("urls") or [] if isinstance(u, dict) and u.get("href")]
        origins = _all_origins(urls)
        if not origins:
            return None
        username = str(item.get("additional_information") or "").strip() or None
        return VaultItemMeta(
            id=handle, kind="login", label=str(item.get("title") or origins[0]), origin=origins[0],
            created_at=str(item.get("created_at") or item.get("createdAt") or ""),
            identifier_type="username" if username else None, identifier=username,
            allowed_origins=_web_origins(origins))

    def _take_fill_metadata(self, handle: str, categories) -> Optional[dict]:
        cached = getattr(self._fill_metadata, "item", None)
        if cached is None:
            return None
        stored_at, cached_handle, item = cached
        if cached_handle != handle or item.get("category") not in categories:
            self._clear_fill_metadata()
            return None
        if time.monotonic() - stored_at > _FILL_METADATA_TTL_SECONDS:
            self._clear_fill_metadata(handle)
            return None
        del self._fill_metadata.item
        return item

    def get_meta(self, handle: str) -> Optional[VaultItemMeta]:
        self._clear_fill_metadata()
        if ":field:" in handle and not handle.startswith(f"{self.prefix}field:"):
            return None
        if handle.startswith(f"{self.prefix}field:"):
            return next((m for m in self.list_protected_fields() if m.id == handle), None)
        if self._connect_credentials()[1]:
            item = self._connect_item(handle, ("LOGIN", "CREDIT_CARD"))
            return self._connect_meta(item, item["vault"]["id"])
        item = self._fresh_item_metadata(handle)
        if item is None:
            self._clear_fill_metadata()
            return None
        meta = self._cli_meta(handle, item)
        if meta is None:
            self._clear_fill_metadata()
        else:
            self._remember_fill_metadata(handle, item)
        return meta

    def _item_selector(self, handle: str, categories=("LOGIN",)) -> List[str]:
        return self._locate(handle, categories)[0]

    def _locate(self, handle: str, categories) -> tuple:
        """``([item_id, "--vault", vault_id], category)`` from fresh per-item metadata."""
        item_id = self._metadata_handle_id(handle)
        item = self._take_fill_metadata(handle, categories)
        if item is None:
            item = self._fresh_item_metadata(handle)
        if item is None:
            raise RuntimeError("1Password item is missing, archived, or not an eligible category")
        category = str(item.get("category") or "")
        if item.get("id") != item_id or category not in categories:
            raise RuntimeError("1Password item is missing or not an eligible category")
        vault = item.get("vault")
        vault_id = vault.get("id") if isinstance(vault, dict) else None
        if isinstance(vault_id, str) and vault_id:
            return [item_id, "--vault", vault_id], category
        raise RuntimeError("1Password item metadata is missing its verified vault ID")

    def resolve_password(self, handle: str) -> str:
        if self._connect_credentials()[1]:
            fields = self._connect_item(handle).get("fields", [])
            passwords = [f.get("value") for f in fields if f.get("purpose") == "PASSWORD"]
            if len(passwords) != 1 or not isinstance(passwords[0], str) or not passwords[0]:
                raise RuntimeError("Connect login requires one password field")
            return passwords[0]
        return self._read_password(self._item_selector(handle))

    def _read_password(self, selector: List[str]) -> str:
        return self._run("item", "get", *selector, "--fields", "label=password", "--reveal").rstrip("\r\n")

    def resolve_otp(self, handle: str) -> Optional[str]:
        if self._connect_credentials()[1]:
            # Connect exposes the item's OTP field (an otpauth:// URI); mint the code locally,
            # never over the CLI. No stored seed → None → the user is asked.
            seed = self._connect_otp_seed(self._connect_item(handle))
            return totp_now(seed) if seed else None
        # `--otp` mints the current TOTP from the item's one-time-password field; items without one error out.
        try:
            code = self._run("item", "get", *self._item_selector(handle), "--otp").strip()
        except MissingCredential:
            raise
        except Exception:
            return None
        return code if code.isdigit() else None

    def resolve_secret(self, handle: str) -> Dict[str, str]:
        """Full payload for a Credit Card item or configured protected field."""
        if handle.startswith(f"{self.prefix}field:"):
            if self._connect_credentials()[1]:
                raise RuntimeError("Configured protected fields require 1Password CLI authentication")
            configured = self.cfg.get("protected_fields") or []
            for entry in configured if isinstance(configured, list) else []:
                normalized = _normalize_protected_field_entry(entry)
                if normalized is None:
                    continue
                reference, semantic, value_type, _label, origins = normalized
                if _protected_field_handle(self.prefix, reference, semantic, value_type, origins) != handle:
                    continue
                value = self._run("read", "--", reference).rstrip("\r\n")
                return {"value": _normalize_protected_date(value)}
            raise RuntimeError("Configured protected field is missing")
        if self._connect_credentials()[1]:
            item = self._connect_item(handle, ("LOGIN", "CREDIT_CARD"))
            if item.get("category") != "CREDIT_CARD":
                return {"password": self.resolve_password(handle)}
            fields = item.get("fields", [])
        else:
            selector, category = self._locate(handle, ("LOGIN", "CREDIT_CARD"))
            if category != "CREDIT_CARD":
                return {"password": self._read_password(selector)}
            item = json.loads(self._run("item", "get", *selector, "--format", "json", "--reveal") or "{}")
            fields = item.get("fields", []) if isinstance(item, dict) else []
        secret = _card_secret(fields)
        if not all(secret.get(k) for k in ("card_number", "exp_month", "exp_year", "cvc")):
            raise RuntimeError("1Password card is missing its number, expiry, or verification number")
        return secret


def _normalize_protected_field_entry(entry):
    from urllib.parse import urlsplit

    if not isinstance(entry, dict):
        return None
    reference = str(entry.get("reference") or "").strip()
    semantic = str(entry.get("semantic") or "").strip()
    value_type = str(entry.get("value_type") or "").strip()
    label = str(entry.get("label") or "").strip()
    raw_origins = entry.get("origins") or []
    try:
        parsed_reference = urlsplit(reference)
    except ValueError:
        return None
    reference_parts = [part for part in parsed_reference.path.split("/") if part]
    if (parsed_reference.scheme != "op" or not parsed_reference.netloc or len(reference_parts) < 2
            or parsed_reference.username or parsed_reference.password or parsed_reference.query
            or parsed_reference.fragment or semantic != "bday" or value_type != "date"
            or not label or not isinstance(raw_origins, list)):
        return None
    origins: List[str] = []
    for raw in raw_origins:
        try:
            origin = normalize_origin(str(raw))
        except Exception:
            continue
        if not origin.startswith("https://") or origin in origins:
            continue
        origins.append(origin)
    return (reference, semantic, value_type, label, origins) if origins else None


def _protected_field_handle(prefix: str, reference: str, semantic: str, value_type: str,
                            origins: List[str]) -> str:
    """Account-scoped handle (``op:field:`` / ``op@<alias>:field:``) so routing by
    prefix reaches the backend, and account binding, that advertised it."""
    digest = hashlib.sha256(
        json.dumps([reference, semantic, value_type, origins], separators=(",", ":")).encode()
    ).hexdigest()[:20]
    return f"{prefix}field:{digest}"


def _normalize_protected_date(value: str) -> str:
    """Normalize 1Password DATE output to ``YYYY-MM-DD`` without disclosure."""
    from datetime import datetime, timezone
    value = (value or "").strip()
    if len(value) == 10 and value[4] == "-" and value[7] == "-":
        try:
            datetime.strptime(value, "%Y-%m-%d")
            return value
        except ValueError:
            pass
    # Eight digits can be either YYYYMMDD or an early Unix timestamp. Neither
    # interpretation is safe without source-type metadata, so fail closed.
    digits = value.lstrip("-")
    if digits.isdigit() and digits and len(digits) != 8 and value.count("-") <= 1:
        try:
            result = datetime.fromtimestamp(int(value), tz=timezone.utc).date().isoformat()
            if 1900 <= int(result[:4]) <= datetime.now(timezone.utc).year:
                return result
        except (OverflowError, OSError, ValueError):
            pass
    raise RuntimeError("Configured protected date has an unsupported format")


def _card_last4(masked: str) -> Optional[str]:
    digits = "".join(ch for ch in masked if ch.isdigit())
    return digits[-4:] if len(digits) >= 4 else None


def _card_meta(handle: str, item, created_at: str, last4: Optional[str]) -> VaultItemMeta:
    # No origin: the fill binds the card to the page it is on, and the user confirms that origin.
    return VaultItemMeta(id=handle, kind="payment", label=str(item.get("title") or "Card"), origin=None,
                         created_at=created_at, identifier_type="card_last4" if last4 else None, identifier=last4)


def _web_origins(origins: List[str]) -> tuple:
    """Fill targets are browser pages, so app URIs (``androidapp://`` etc.) never
    widen the fill set; an item whose only URI is an app URI keeps its single
    (unfillable-from-a-page) origin exactly as before."""
    web = tuple(o for o in origins if o.startswith(("http://", "https://")))
    return web or (origins[0],)


def _all_origins(urls: List[str]) -> List[str]:
    """Every normalized origin saved on the item, deduped, order preserved.

    A 1Password Login item can carry several websites; each of them is a place the
    user told 1Password the credential belongs, so all of them are valid fill targets.
    """
    out: List[str] = []
    for u in urls:
        u = u.strip()
        if _BARE_HOST_RE.fullmatch(u):
            u = "https://" + u  # 1Password saves a typed "example.com" verbatim and opens it as https
        try:
            origin = normalize_origin(u)
        except Exception:
            continue
        if origin not in out:
            out.append(origin)
    return out
