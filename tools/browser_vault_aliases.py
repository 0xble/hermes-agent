"""Alias-only login authorization, independent of confirmation preferences."""

import json
from collections.abc import Callable

from agent.vault_backends.base import FILL_METADATA_TTL_SECONDS
from agent.vault_origin_aliases import alias_domain_warning, alias_fill_confirmation_enabled, apply_origin_aliases
from agent.vault_store import VaultItemMeta


def authorize_login_alias(
    handle: str,
    raw_meta: VaultItemMeta,
    meta: VaultItemMeta,
    page_origin: str,
    task_id: str,
    refresh_metadata: Callable[[], object],
) -> tuple[VaultItemMeta, VaultItemMeta] | str:
    from tools import browser_vault_tool as vault

    saved_origins = tuple(raw_meta.allowed_origins) or ((str(raw_meta.origin),) if raw_meta.origin else ())
    alias_key = vault._alias_fill_key(task_id, handle, page_origin)
    decision = vault._alias_fill_decision(alias_key)
    if decision == "refused":
        return json.dumps({"success": False, "error_type": "origin_alias_retry_refused",
                           "error": "Alias-origin login confirmation was declined or unanswered. Do not retry; ask the user to fill the login or explicitly start a new approval session."})
    if not alias_fill_confirmation_enabled():
        if warning := alias_domain_warning(page_origin, saved_origins):
            vault.logger.info("%s", warning)
        return raw_meta, meta
    if decision is None:
        prompt_started = vault.time.monotonic()
        decision = vault._confirm_alias_fill(meta.label, page_origin, saved_origins)
        if decision != "accept":
            vault._record_alias_fill_decision(alias_key, "refused")
        else:
            if vault.time.monotonic() - prompt_started > FILL_METADATA_TTL_SECONDS:
                refreshed = refresh_metadata()
                if refreshed is None:
                    return json.dumps({"success": False, "error_type": "metadata_changed",
                                       "error": "The vault item changed while confirmation was pending. Nothing was written; retry the fill."})
                if isinstance(refreshed, VaultItemMeta):
                    raw_meta = refreshed
                    meta = apply_origin_aliases([raw_meta])[0]
            if refusal := vault._alias_fill_revalidation_refusal(raw_meta, page_origin):
                return refusal
            vault._record_alias_fill_decision(alias_key, "accept")
    if decision != "accept":
        return json.dumps({"success": False,
                           "error_type": "origin_alias_declined" if decision == "decline" else "origin_alias_prompt_unanswered",
                           "error": ("The user declined this alias-origin login fill." if decision == "decline" else
                                     "The alias-origin login fill prompt went unanswered.") +
                                    " Do not retry; ask the user to fill the login."})
    return raw_meta, meta
