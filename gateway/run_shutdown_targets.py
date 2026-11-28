"""Reserve shutdown-note destinations before the ordinary broadcast dedup pass."""

from logging import Logger

async def reserve_shutdown_note_target(self, session_key, notified, private_topic_parents, logger: Logger):
    from gateway.run_shutdown import Platform, _delivery_target_key
    # Reserve the S2 note's destination in the same dedup set used by the
    # home-channel pass. The note is sent after a timed-out drain; without this
    # reservation an active session that is also the home channel receives the
    # ordinary broadcast before the S2 note. A failed S2 send clears this fence by
    # replacing _s2_note_session_keys with the delivered set before the fallback pass.
    target = await self._shutdown_notification_target(session_key)
    if target is None:
        return
    if len(target) == 4:
        source, platform_str, chat_id, thread_id = target
        profile = None
    else:
        source, platform_str, chat_id, thread_id, profile = target
    try:
        platform = Platform(platform_str)
        adapter = self._delivery_adapter_for(source) if source is not None else None
        if adapter is None:
            adapter = self._authorization_adapter(platform, profile)
        if adapter is None:
            return
        _, delivery_profile = self._owning_profile(adapter, platform)
        notified.add(_delivery_target_key(
            platform_str, chat_id, thread_id, profile=delivery_profile,
        ))
        if (platform == Platform.TELEGRAM and thread_id is not None
                and getattr(source, "chat_type", None) in {"dm", "private"}):
            private_topic_parents.add((id(adapter), str(chat_id)))
    except Exception as e:
        logger.error("Failed to reserve S2 shutdown target for %s: %s", session_key, e, exc_info=True)
