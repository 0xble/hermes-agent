import asyncio
from concurrent.futures import Future
from types import SimpleNamespace
from unittest.mock import MagicMock, patch
from cron.scheduler import _deliver_result
from gateway.config import Platform, PlatformConfig


def test_shared_bot_named_topic_is_persisted_in_the_bot_owners_config(tmp_path, monkeypatch):
    """A credentialless satellite's cron job delivers through the primary's bot (exact profile route).
    The topic that bot creates belongs to the bot's profile and durable owner registry.
    The satellite's config.yaml must not gain Telegram dm_topics."""
    import hermes_yaml as yaml
    from cron.scheduler_preflight import SharedRouteAdapters
    from gateway.profile_routing import ProfileRoute
    from hermes_constants import reset_hermes_home_override, set_hermes_home_override
    from hermes_state_registry import acquire, release_or_close
    from tests.gateway.conftest import _ensure_telegram_mock

    _ensure_telegram_mock()
    from plugins.platforms.telegram.adapter import TelegramAdapter

    owner, satellite = tmp_path / "owner", tmp_path / "satellite"
    for home in (owner, satellite):
        home.mkdir()
        (home / "config.yaml").write_text("platforms: {}\n")
    monkeypatch.setenv("HERMES_HOME", str(owner))  # the primary bot is built in its own profile
    adapter = TelegramAdapter(PlatformConfig(enabled=True, token="fake", extra={}))
    db = acquire(owner / "state.db")
    adapter._session_store = SimpleNamespace(_db=db)

    async def create_topic(chat_id, name, **_):
        db.create_session("brief", source="telegram", profile_name="default")
        db.bind_telegram_topic(chat_id=str(chat_id), thread_id="4242", user_id=str(chat_id),
                              session_key=f"agent:main:telegram:dm:{chat_id}:4242", session_id="brief",
                              managed_mode="automation")
        return 4242

    adapter._create_dm_topic = create_topic
    sent = []

    async def send(chat_id, content, reply_to=None, metadata=None):
        sent.append(metadata["thread_id"])
        return SimpleNamespace(success=True, message_id="1", raw_response=None, error=None)

    adapter.send = send
    shared = SharedRouteAdapters({Platform.TELEGRAM: adapter}, [
        ProfileRoute(name="sat", platform="telegram", chat_id="722341991", profile="satellite")])
    job = {"id": "j-shared", "name": "Brief", "deliver": "telegram:722341991:Debug"}
    loop = MagicMock()
    loop.is_running.return_value = True

    def run_now(coro, _loop):
        future = Future()
        try:
            future.set_result(asyncio.run(coro))
        except BaseException as exc:
            future.set_exception(exc)
        return future

    config = MagicMock()
    config.platforms = {}
    config.get_home_channel = lambda p: None
    token = set_hermes_home_override(satellite)  # the multiplexed cron tick runs the job in its profile
    try:
        with patch("gateway.config.load_gateway_config", return_value=config), \
             patch("cron.scheduler.load_config", return_value={"cron": {"wrap_response": False}}), \
             patch("asyncio.run_coroutine_threadsafe", side_effect=run_now):
            error = _deliver_result(job, "daily brief", adapters=shared, loop=loop)
    finally:
        reset_hermes_home_override(token)
        release_or_close(db)

    assert error is None and sent == ["4242"]
    owner_cfg = yaml.safe_load((owner / "config.yaml").read_text())
    topics = owner_cfg["platforms"]["telegram"]["extra"]["dm_topics"]
    assert topics == [{"chat_id": 722341991, "topics": [{"name": "Debug", "thread_id": 4242}]}]
    assert yaml.safe_load((satellite / "config.yaml").read_text()) == {"platforms": {}}
