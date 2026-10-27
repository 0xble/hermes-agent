"""Producer-owned FYI styling, carried only to adapters that consume it.

Kept outside gateway/ so storage and agent modules can mark notices without
importing the gateway package.
"""


class ContextNotice(str):
    """A receipt/status producer explicitly says no user action is required."""
    notice_style = "context"


def notice_metadata(metadata=None, *, platform=None, style=None):
    """Leave other platforms byte-for-byte unchanged, including metadata shape."""
    if getattr(platform, "value", platform) != "slack" or style != "context":
        return metadata
    return {**(metadata or {}), "notice_style": "context", "_interim_send": True}
