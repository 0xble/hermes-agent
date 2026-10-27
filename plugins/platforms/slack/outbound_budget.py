"""One outbound admission path for Slack SDK methods and raw stream calls."""

from asyncio import sleep
from collections.abc import Mapping
from time import monotonic
from weakref import WeakValueDictionary

from slack_sdk.errors import SlackApiError
from slack_sdk.web.async_client import AsyncWebClient
from slack_sdk.web.async_slack_response import AsyncSlackResponse

from agent.retry_utils import parse_retry_after_seconds


class _OutboundBudget:
    def __init__(self):
        self.channels: dict[str, float] = {}
        self.methods: dict[str, float] = {}
        self.cooldowns: dict[str, float] = {}
        self.widen_until: dict[str, float] = {}

    def delay(self, method: str, channels: set[str]) -> float:
        now = monotonic()
        return max(0.0, self.methods.get(method, 0) - now,
                   self.cooldowns.get(method, 0) - now,
                   *(self.channels.get(channel, 0) - now for channel in channels))

    def charge(self, method: str, channels: set[str]) -> None:
        now = monotonic()
        # Slack: startStream Tier 2 (20+/min), appendStream Tier 4 (100+/min).
        # All other current writes use the conservative Tier 2 allowance.
        gap = 0.8 if method == "chat.appendStream" else 4.0
        if now < self.widen_until.get(method, 0):
            gap *= 2
        self.methods[method] = now + gap
        for channel in channels:
            self.channels[channel] = now + 2.0

    def rate_limited(self, method: str, response: AsyncSlackResponse) -> None:
        retry_after = parse_retry_after_seconds(response.headers)
        now = monotonic()
        until = now + (retry_after if retry_after is not None else 4.0)
        self.cooldowns[method] = max(self.cooldowns.get(method, 0), until)
        # A temporary lower rate after Slack rejects us, never a permanent latch.
        self.widen_until[method] = self.cooldowns[method] + 60.0


# The Bolt primary client and auth.test client use the same workspace bot token.
# Weak values release credentials and pacing state when their clients disappear.
_BUDGETS: WeakValueDictionary[str | None, _OutboundBudget] = WeakValueDictionary()


def _is_write(method: str) -> bool:
    return (method.startswith(("chat.", "reactions.", "assistant.threads.", "agents.sessions."))
            and method not in {"chat.getPermalink", "chat.scheduledMessages.list", "reactions.get",
                               "reactions.list", "agents.sessions.info", "agents.sessions.list"}
            or method in {"files.completeUploadExternal", "files.share", "files.upload"})


def _channels(kwargs: dict) -> set[str]:
    channels: set[str] = set()
    for key in ("json", "data", "params"):
        payload = kwargs.get(key)
        if not isinstance(payload, Mapping):
            continue
        for field in ("channel", "channel_id", "channels"):
            value = payload.get(field)
            if isinstance(value, str):
                channels.update(part for part in value.split(",") if part)
            elif isinstance(value, (list, tuple)):
                channels.update(value)
    return channels


def _cosmetic(method: str, kwargs: dict) -> bool:
    payload = kwargs.get("json") or kwargs.get("params") or kwargs.get("data") or {}
    if method == "assistant.threads.setStatus":
        return bool(payload.get("status"))  # Clearing a status must still run.
    chunks = payload.get("chunks")
    return (method == "chat.appendStream" and bool(chunks)
            and not payload.get("markdown_text") and isinstance(chunks, list)
            and all(chunk.get("type") in {"task_update", "plan_update"} for chunk in chunks))


class BudgetedAsyncWebClient(AsyncWebClient):
    """Keep all transport attempts visible to the shared workspace-token budget."""

    def __init__(self, *args, **kwargs):
        # SDK retries happen below api_call and cannot be charged by this gate.
        # The adapter already owns explicit, disposition-aware retries.
        kwargs["retry_handlers"] = []
        super().__init__(*args, **kwargs)
        budget = _BUDGETS.get(self.token)
        if budget is None:
            budget = _OutboundBudget()
            _BUDGETS[self.token] = budget
        self._outbound_budget = budget

    async def api_call(self, api_method: str, **kwargs) -> AsyncSlackResponse:
        # Slack's published limits do not describe alternate API endpoints, such
        # as a stand-in server. Do not delay, shed, or share their cooldowns.
        if self.base_url != "https://slack.com/api/" or not _is_write(api_method):
            return await super().api_call(api_method, **kwargs)
        channels = _channels(kwargs)
        while delay := self._outbound_budget.delay(api_method, channels):
            server_wait = self._outbound_budget.cooldowns.get(api_method, 0) - monotonic()
            if _cosmetic(api_method, kwargs) or server_wait > 10.0:
                # Shed stale indicators and return long server penalties to the
                # caller instead of parking real output. Routine pacing, including
                # adaptive spacing, is still waited out regardless of its length.
                response = AsyncSlackResponse(
                    client=self, http_verb="POST", api_url=api_method, req_args={},
                    data={"ok": False, "error": "ratelimited"},
                    headers={"Retry-After": str(delay)}, status_code=429,
                )
                raise SlackApiError("Slack outbound budget busy", response)
            await sleep(delay)
        # No await between checking and charging: concurrent producers cannot
        # reserve the same slot, including copy.copy clients used for starts.
        self._outbound_budget.charge(api_method, channels)
        try:
            return await super().api_call(api_method, **kwargs)
        except SlackApiError as exc:
            if exc.response.status_code == 429 or exc.response.get("error") in {"ratelimited", "rate_limited"}:
                self._outbound_budget.rate_limited(api_method, exc.response)
            raise
