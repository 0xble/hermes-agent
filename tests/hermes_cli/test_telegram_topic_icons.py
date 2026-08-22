from __future__ import annotations

import json

from tests.hermes_cli.test_telegram_namespace import _parser


class _Service:
    def __init__(self):
        self.calls = []

    def catalog(self, **kwargs):
        self.calls.append(("catalog", kwargs))
        return {
            "schema_version": 1,
            "items": [
                {
                    "emoji": "🧹",
                    "custom_emoji_id": 20,
                    "pack": "AppleObjectsHomeTools",
                    "pack_title": "Apple Objects",
                    "source": "custom_pack",
                }
            ],
        }

    def resolve(self, emoji, pack=None):
        self.calls.append(("resolve", {"emoji": emoji, "pack": pack}))
        return self.catalog()["items"][0]

    def verify(self, topic_id):
        self.calls.append(("verify", {"topic_id": topic_id}))
        return {"schema_version": 1, "topic_id": topic_id, "state": "custom_manual"}

    def set(self, topic_id, emoji, pack=None):
        self.calls.append(
            ("set", {"topic_id": topic_id, "emoji": emoji, "pack": pack})
        )
        return {
            "schema_version": 1,
            "topic_id": topic_id,
            "requested_icon_emoji_id": 20,
            "observed_icon_emoji_id": 20,
            "result": "verified",
        }


def test_catalog_and_resolve_emit_custom_only_json(monkeypatch, capsys):
    from hermes_cli import telegram_topic_icons

    service = _Service()
    monkeypatch.setattr(telegram_topic_icons, "build_operator_service", lambda: service)

    args = _parser().parse_args(
        ("telegram", "topic-icons", "catalog", "--emoji", "🧹", "--json")
    )
    assert args.func(args) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["items"][0]["source"] == "custom_pack"
    assert service.calls[0][1]["emoji"] == "🧹"

    args = _parser().parse_args(
        ("telegram", "topic-icons", "resolve", "🧹️", "--json")
    )
    assert args.func(args) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["custom_emoji_id"] == 20


def test_set_requires_yes_before_building_a_live_service(monkeypatch, capsys):
    from hermes_cli import telegram_topic_icons

    monkeypatch.setattr(
        telegram_topic_icons,
        "build_operator_service",
        lambda: (_ for _ in ()).throw(AssertionError("must not construct service")),
    )
    args = _parser().parse_args(
        ("telegram", "topic-icons", "set", "--topic", "1", "--emoji", "🧹")
    )

    assert args.func(args) == 2
    assert "--yes" in capsys.readouterr().err


def test_set_reports_success_only_from_verified_service_receipt(monkeypatch, capsys):
    from hermes_cli import telegram_topic_icons

    service = _Service()
    monkeypatch.setattr(telegram_topic_icons, "build_operator_service", lambda: service)
    args = _parser().parse_args(
        (
            "telegram",
            "topic-icons",
            "set",
            "--topic",
            "1",
            "--emoji",
            "🧹",
            "--yes",
            "--json",
        )
    )

    assert args.func(args) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["result"] == "verified"
    assert payload["requested_icon_emoji_id"] == payload["observed_icon_emoji_id"]


def test_provider_errors_are_sanitized_without_traceback(monkeypatch, capsys):
    from hermes_cli import telegram_topic_icons
    from plugins.platforms.telegram.topic_icons import TopicIconCatalogError

    class Service:
        def resolve(self, *_args, **_kwargs):
            raise TopicIconCatalogError("configured custom pack is unavailable")

    monkeypatch.setattr(telegram_topic_icons, "build_operator_service", Service)
    args = _parser().parse_args(("telegram", "topic-icons", "resolve", "🧹"))
    assert args.func(args) == 1
    captured = capsys.readouterr()
    assert "configured custom pack is unavailable" in captured.err
    assert "Traceback" not in captured.err
