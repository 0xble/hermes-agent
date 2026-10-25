"""User-facing diagnostics for Slack attachment downloads that raised.

The agent repeats this text to the user, so it must name the real cause: a permission or auth
problem only when Slack actually answered with one, never for bytes Hermes could not decode.
"""

from __future__ import annotations

from typing import Optional

_SLACK_HTTP_STATUS_TEMPLATES = {
    401: "Slack attachment access failed for {file_label} with HTTP 401. The bot token is not "
         "authorized for this file.",
    403: "Slack attachment access failed for {file_label} with HTTP 403. The bot likely lacks "
         "permission or scope to read this file.",
    404: "Slack attachment {file_label} returned HTTP 404 and is no longer reachable."}


def describe_download_exception(exc: Exception, file_label: str) -> Optional[str]:
    """Diagnosis for a download exception that carried no Slack API error, or None."""
    try:
        import httpx
    except ImportError:  # pragma: no cover
        httpx = None
    if httpx is not None and isinstance(exc, httpx.HTTPStatusError):
        template = _SLACK_HTTP_STATUS_TEMPLATES.get(exc.response.status_code)
        if template:
            return template.format(file_label=file_label)
    message = str(exc)
    # The image cache quotes the first bytes ("starts with: '<!DOCTYPE…'"). An HTML document there
    # is a sign-in page; anything else (SVG, XML, binary) is a format Hermes cannot read.
    if "Slack returned HTML instead of media" in message or (
            "non-image data" in message and _looks_like_html(message.partition("starts with:")[2])):
        return (
            f"Slack attachment access failed for {file_label}: Slack returned an HTML/login or non-media response. "
            "This usually means a scope, auth, or file-permission problem.")
    if "non-image data" in message:
        return f"Hermes downloaded {file_label} but couldn't read it as an image (unsupported format)."
    return None


def _looks_like_html(snippet: str) -> bool:
    head = snippet.strip().lstrip("'\"").replace("\\r", "").replace("\\n", "").lstrip().lower()
    return head.startswith(("<!doctype html", "<html", "<head", "<body"))
