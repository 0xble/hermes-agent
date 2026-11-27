"""Telegram egress methods carry the durable-outbox wrapper for their own kind.

``@durable_egress(kind)`` replays a row by calling the adapter method named ``kind``, so the decorator
must sit on that method. A merge once left it on the private ``_after_legacy_edit`` helper, which
left ``edit_message`` unprotected and queued helper calls as ``edit_message`` rows.
"""

from __future__ import annotations

import inspect

from plugins.platforms.telegram.adapter import TelegramAdapter


def test_edit_message_is_durable_egress():
    assert getattr(TelegramAdapter.edit_message, "_durable_outbox", False) is True


def test_durable_wrappers_only_on_public_egress_methods():
    durable = {
        name for name, member in inspect.getmembers(TelegramAdapter, inspect.isfunction)
        if getattr(member, "_durable_outbox", False)
    }
    assert "_after_legacy_edit" not in durable
    assert not {name for name in durable if name.startswith("_")}
