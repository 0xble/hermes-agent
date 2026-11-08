"""Secret-safe public replies for vault fills."""

import json


def fill_result_reply(meta, backend_name, parsed, fills, page_origin):
    """Expose only counts and targeted controls, never filled values."""
    filled = parsed.get("filled", 0) if isinstance(parsed, dict) else 0
    if meta.kind == "protected_field" and filled and int(filled) != len(fills):
        # A partial date is a wrong date: report failure rather than a half-filled success.
        return json.dumps({"success": False, "error_type": "partial_fill", "filled_fields": int(filled),
                           "error": "Could not fill every birth-date part unambiguously; check the form "
                                    "or use browser_handoff for the user to finish it."})

    out = {"success": bool(filled), "filled_fields": int(filled), "backend": backend_name,
           "kind": meta.kind, "origin": page_origin}
    if meta.kind == "login":
        out["remembered"] = bool(parsed.get("remembered")) if isinstance(parsed, dict) else False
        out["next"] = ("Submit. If the site then asks for a verification code, "
                       + ("call browser_vault_enter_code with this handle (a code will be generated automatically)."
                          if meta.has_otp else
                          "fetch the newest matching code from the intended 1Password item, mailbox, or phone and "
                          "type it with the browser input tool; call browser_vault_enter_code only when no such source "
                          "exists."))
    if meta.kind != "login":
        out["fields"] = sorted(f["token"] for f in fills)  # which controls were targeted, never the values
    return json.dumps(out)
