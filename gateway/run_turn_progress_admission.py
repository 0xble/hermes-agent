"""Choose an editable progress adapter or drain/delegate the progress lane."""

async def editable_progress_adapter(self):
    from gateway.run_turn_progress import BasePlatformAdapter
    ctx = self._ctx
    adapter = self._runner._delivery_adapter_for(ctx.source) if ctx.progress_queue else None
    if not adapter:
        return
    if ctx._native_slack_task_cards and hasattr(adapter, "send_native_task_card_progress"):
        await self._send_native_task_card_progress(adapter)
        return
    # Skip tool progress for platforms that can't edit messages (e.g. iMessage/BlueBubbles):
    # each update would be a separate bubble. getattr, not attribute access: duck-typed
    # adapters (test fakes, minimal plugins) may lack edit_message — treated as "can't edit".
    adapter_edit = getattr(type(adapter), "edit_message", None)
    if adapter_edit is None or adapter_edit is BasePlatformAdapter.edit_message:
        self._drain_progress_queue()
        return
    return adapter
