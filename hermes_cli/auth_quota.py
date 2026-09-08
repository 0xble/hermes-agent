"""Explicit, named-credential quota inspection. Never select or refresh during a probe."""

from __future__ import annotations

from datetime import datetime, timezone
import json
import math

import httpx

from agent.credential_pool import (
    AUTH_TYPE_OAUTH,
    REFRESHABLE_OAUTH_PROVIDERS,
    load_pool,
    load_pool_read_only,
)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _percent(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value) if math.isfinite(value) and 0 <= value <= 100 else None


def probe_quota(provider, entry) -> dict:
    """Codex's official usage endpoint, with explicit selected-token provenance.

    Other providers are explicitly unsupported, not routed through a resolver
    that may seed, refresh, or choose another pool account. No POSTs or writes.
    """
    result = {
        "outcome": "unknown",
        "observed_at": _now(),
        "source": "usage_api",
        "windows": [],
    }
    if provider != "openai-codex":
        return {
            **result,
            "outcome": "unsupported",
            "error": "provider_quota_unsupported",
        }
    if not entry.runtime_api_key:
        return {
            **result,
            "error": "access_token_missing",
            "action": "refresh_credential",
        }
    from agent.account_usage import _codex_backend_urls, _codex_headers, _get_json
    from hermes_cli.auth import _decode_jwt_claims

    claims = _decode_jwt_claims(entry.runtime_api_key) or {}
    auth_claim = claims.get("https://api.openai.com/auth") or {}
    account_id = (
        auth_claim.get("chatgpt_account_id") if isinstance(auth_claim, dict) else None
    )
    try:
        payload = _get_json(
            _codex_backend_urls(entry.runtime_base_url)[0],
            _codex_headers(entry.runtime_api_key, account_id),
            timeout=15.0,
        )
    except httpx.HTTPStatusError as exc:
        code = exc.response.status_code
        return {
            **result,
            "http_status": code,
            "error": "access_rejected" if code in (401, 403) else "quota_http_error",
            "action": "refresh_credential" if code in (401, 403) else "retry_probe",
        }
    except Exception:
        # Exception text and provider bodies can contain credential-bearing URLs.
        return {**result, "error": "quota_probe_failed", "action": "retry_probe"}
    if not isinstance(payload, dict) or not isinstance(payload.get("rate_limit"), dict):
        return {**result, "error": "invalid_quota_payload"}
    rate = payload["rate_limit"]
    blocked = rate.get("allowed") is False or rate.get("limit_reached") is True
    windows = []
    malformed = False
    for key in ("primary_window", "secondary_window"):
        raw = rate.get(key)
        if raw is None:
            continue
        if not isinstance(raw, dict) or _percent(raw.get("used_percent")) is None:
            malformed = True
            continue
        used = _percent(raw["used_percent"])
        assert used is not None
        seconds = raw.get("limit_window_seconds")
        if isinstance(seconds, bool) or not isinstance(seconds, int) or seconds <= 0:
            seconds = None
        kind = (
            "weekly"
            if seconds == 604800
            else "session"
            if seconds == 18000
            else "unknown"
        )
        reset_at = raw.get("reset_at")
        try:
            reset = (
                datetime.fromtimestamp(reset_at, timezone.utc).isoformat()
                if isinstance(reset_at, (int, float)) and not isinstance(reset_at, bool)
                else None
            )
        except (ValueError, OverflowError, OSError):
            reset = None
        windows.append({
            "kind": kind,
            "source_key": key,
            "window_seconds": seconds,
            "used_percent": used,
            "remaining_percent": 100.0 - used,
            "reset_at": reset,
        })
    exhausted = blocked or any(w["used_percent"] >= 100 for w in windows)
    # Incomplete/invalid evidence never proves availability.
    outcome = (
        "exhausted"
        if exhausted
        else "unknown"
        if malformed or not windows
        else "available"
    )
    return {
        **result,
        "outcome": outcome,
        "windows": windows,
        "plan": payload.get("plan_type")
        if isinstance(payload.get("plan_type"), str)
        else None,
        "limit_reached": rate.get("limit_reached")
        if isinstance(rate.get("limit_reached"), bool)
        else None,
        **({"error": "incomplete_quota_payload"} if outcome == "unknown" else {}),
    }


def _resolve(pool, target):
    entries = pool.entries()
    if target is None or not str(target).strip():
        return (
            (1, entries[0], None)
            if len(entries) == 1
            else (
                None,
                None,
                "Pass an exact label, entry ID, or index when the pool does not contain exactly one entry.",
            )
        )
    return pool.resolve_target(target)


def _view(index, entry):
    return {
        "id": entry.id,
        "label": entry.label,
        "index": index,
        "auth_type": entry.auth_type,
    }


def _cached(entry):
    # These are historical observations, never the verdict of the live probe.
    return {
        "status": entry.last_status,
        "observed_at": entry.last_status_at,
        "http_status": entry.last_error_code,
        "reset_at": entry.last_error_reset_at,
    }


def _emit(args, report, code=0):
    report = {"schema_version": 1, **report}
    if getattr(args, "json", False):
        print(json.dumps(report, sort_keys=True, allow_nan=False))
    else:
        identity = report.get("credential", {})
        print(f"{report['provider']} {identity.get('label', '')}: {report['outcome']}")
        quota = report.get("quota", {})
        if quota.get("plan"):
            print(f"  Plan: {quota['plan']} (live)")
        for window in quota.get("windows", []):
            print(
                f"  {window['kind']}: {window['used_percent']:g}% used, {window['remaining_percent']:g}% remaining; reset {window['reset_at'] or 'unknown'}"
            )
        error = report.get("error") or quota.get("error")
        if error:
            print(f"  {error}")
        if quota.get("action"):
            print(
                f"  Next: {quota['action']} (quota unreadable, not proof reauth is required)"
            )
    if code:
        raise SystemExit(code)


def _selected(args):
    provider = args.provider
    index = entry = error = None
    try:
        pool = load_pool_read_only(provider)
        index, entry, error = _resolve(pool, getattr(args, "target", None))
    except Exception:
        _emit(
            args,
            {
                "provider": provider,
                "outcome": "store_unavailable",
                "error": "Unable to read credential store.",
            },
            1,
        )
    if entry is None or index is None:
        _emit(
            args, {"provider": provider, "outcome": "invalid_target", "error": error}, 2
        )
    assert entry is not None and index is not None
    return index, entry


def run_status(args):
    index, entry = _selected(args)
    report = {
        "provider": args.provider,
        "credential": _view(index, entry),
        "cached": _cached(entry),
        "outcome": "not_probed",
    }
    if getattr(args, "live", False):
        quota = probe_quota(args.provider, entry)
        report.update(quota=quota, outcome=quota["outcome"])
    _emit(args, report, 1 if report["outcome"] in ("unknown", "unsupported") else 0)


def _refresh_selected(provider, credential_id):
    """Lock ownership and membership before native load/refresh can persist."""
    from hermes_cli import auth as auth_mod
    from agent.credential_pool import read_pool_snapshot

    # Native write-through takes local then global locks. Preserve that order.
    with auth_mod._auth_store_lock():
        owner, _ = read_pool_snapshot(provider)
        with auth_mod._auth_store_lock(target_path=owner):
            current_owner, rows = read_pool_snapshot(provider)
            if current_owner != owner or not any(
                r.get("id") == credential_id for r in rows
            ):
                raise ValueError("Credential ownership changed")
            pool = load_pool(provider)
            if not any(e.id == credential_id for e in pool.entries()):
                raise ValueError("Credential disappeared during recovery")
            return pool, pool.try_refresh_matching(credential_id=credential_id)


def run_refresh(args):
    index, chosen = _selected(args)
    report = {"provider": args.provider, "credential": _view(index, chosen)}
    if (
        args.provider not in REFRESHABLE_OAUTH_PROVIDERS
        or chosen.auth_type != AUTH_TYPE_OAUTH
        or not chosen.refresh_token
        # Nous's resolver is singleton-bound, not an independent-account refresher.
        or (args.provider == "nous" and chosen.source != "device_code")
    ):
        _emit(
            args,
            {
                **report,
                "outcome": "unsupported",
                "error": (
                    "Nous refresh supports only the device_code singleton."
                    if args.provider == "nous" and chosen.source != "device_code"
                    else "Credential has no supported refresh grant."
                ),
            },
            2,
        )
    pool = None
    refreshed = None
    try:
        pool, refreshed = _refresh_selected(args.provider, chosen.id)
    except Exception:
        _emit(
            args,
            {
                **report,
                "outcome": "refresh_failed",
                "error": "Refresh failed; no terminal reauth evidence.",
            },
            1,
        )
    assert pool is not None
    if refreshed is None:
        reason = pool.refresh_failure_reason(chosen.id)
        _emit(
            args,
            {
                **report,
                "outcome": "reauth_required"
                if reason == "terminal"
                else "refresh_failed",
                "error": "Native refresh recovery rejected the grant."
                if reason == "terminal"
                else "Refresh did not recover this credential; interactive login requirement is unproven.",
            },
            1,
        )
    assert refreshed is not None
    try:
        stored = next(
            (
                e
                for e in load_pool_read_only(args.provider).entries()
                if e.id == chosen.id
            ),
            None,
        )
    except Exception:
        stored = None
    if (
        stored is None
        or stored.runtime_api_key != refreshed.runtime_api_key
        or stored.refresh_token != refreshed.refresh_token
    ):
        _emit(
            args,
            {
                **report,
                "outcome": "readback_failed",
                "error": "Refreshed credential does not match persisted readback.",
            },
            1,
        )
    assert stored is not None
    report.update(
        refresh={"outcome": "completed", "persisted": True},
        cached=_cached(stored),
        outcome="refresh_completed",
    )
    if getattr(args, "verify", False):
        quota = probe_quota(args.provider, stored)
        report.update(quota=quota, outcome="refreshed_" + quota["outcome"])
    # Completed may include adopting a peer rotation. Do not claim a POST occurred.
    _emit(
        args,
        report,
        1 if report["outcome"] in ("refreshed_unknown", "refreshed_unsupported") else 0,
    )
