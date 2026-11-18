"""Delivery-result field access and Bot Chat outcome accounting."""

def _result_field(send_result, key: str, default=None):
    """Read ``key`` from a SendResult-like object or the plain dict the silence filter returns."""
    if isinstance(send_result, dict):
        return send_result.get(key, default)
    return getattr(send_result, key, default)


def _deliver_bot_chat_result(
    job, target, delivery_content, copy_blocks, suppressed_targets, *, for_failure, delivery_errors, unverified_targets,
):
    """Deliver one Bot Chat result and account for its durable receipt before continuing."""
    from cron.scheduler_delivery import _deliver_to_bot_chat

    bot_chat_content = delivery_content
    if copy_blocks:
        bot_chat_content = "\n\n".join(part for part in (delivery_content, *copy_blocks) if part)
    bot_chat_error = _deliver_to_bot_chat(job, bot_chat_content, target["chat_id"], for_failure=for_failure)
    suppressed_targets += job.pop("_notification_all_targets_suppressed", False)
    if bot_chat_error:
        receipt_target = f"bot-chat:{target['chat_id'] or '(own)'}"
        receipt = job.get("_bot_chat_delivery_receipts", {}).get(receipt_target)
        if not receipt or receipt["status"] not in ("queued", "claimed"):
            delivery_errors.append(bot_chat_error)
        if receipt and receipt["status"] == "ambiguous":
            unverified_targets.append(bot_chat_error)
    return suppressed_targets
