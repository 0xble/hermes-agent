"""Normalize ephemeral reply wrappers and result/event TTL hints."""

def ephemeral_reply_options(response, agent_result, event):
    from gateway.platforms.base import EphemeralReply
    is_ephemeral_response = isinstance(response, EphemeralReply) or bool(
        agent_result.get("is_ephemeral_response", getattr(event, "_is_ephemeral_response", False)))
    ephemeral_ttl = getattr(response, "ttl_seconds", None)
    if isinstance(response, EphemeralReply):
        response = response.text
    if ephemeral_ttl is None:
        try:
            ephemeral_ttl = int(agent_result.get("ephemeral_ttl", getattr(event, "_ephemeral_ttl", 0)) or 0)
        except (TypeError, ValueError):
            ephemeral_ttl = 0
    return response, is_ephemeral_response, ephemeral_ttl
