"""Request-local fallback policy shared by synchronous auxiliary consumers."""
from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any, Callable
from weakref import finalize


@dataclass(frozen=True)
class CallFallbackPolicy:
    task: str | None
    chain: list[dict[str, Any]] | None
    prepare: Callable | None = None


_policy: ContextVar[CallFallbackPolicy | None] = ContextVar("aux_call_fallback_policy", default=None)


@contextmanager
def scoped_fallback_policy(policy: CallFallbackPolicy):
    token = _policy.set(policy)
    try:
        yield
    finally:
        _policy.reset(token)


def has_call_fallback_chain(task: str | None) -> bool:
    policy = _policy.get()
    return policy is not None and policy.task == task and policy.chain is not None


def call_fallback_chain(task: str | None, inherited: Any) -> Any:
    policy = _policy.get()
    return policy.chain if policy is not None and policy.task == task and policy.chain is not None else inherited


def prepare_fallback_request(
    task: str | None, destination: Any, messages: list, tools: Any, reasoning_config: Any, entry: Any,
) -> tuple[list, Any, Any]:
    policy = _policy.get()
    if policy is not None and policy.task == task and policy.prepare is not None:
        return policy.prepare(destination, messages, tools, reasoning_config, entry)
    return messages, tools, reasoning_config


def is_stream_response(response: Any) -> bool:
    """Completed SDK models may be iterable, but their choices still belong to the consumer."""
    return not hasattr(response, "choices") and isinstance(response, Iterator)


class _OwnedStream(Iterator):
    """Own cleanup at construction, including close before the first next()."""

    def __init__(self, iterator: Iterator, on_close: Callable):
        self._iterator = iterator
        self._finalizer = finalize(self, self._close_resource, iterator, on_close)

    def __iter__(self):
        return self

    def __next__(self):
        if not self._finalizer.alive:
            raise StopIteration
        try:
            return next(self._iterator)
        except BaseException:
            self.close()
            raise

    def close(self):
        self._finalizer()

    @staticmethod
    def _close_resource(iterator, on_close):
        try:
            _close_stream(iterator)
        finally:
            on_close()


def scoped_fallback_stream(stream: Any, policy: CallFallbackPolicy, progress_hook: Any, main_runtime: Any):
    """Bind only while advancing/closing, never while control belongs to its consumer."""
    from agent.auxiliary_client import _RELAY_AUX_CALL_CONTEXT, aux_progress_hook, scoped_runtime_main
    relay_context = _RELAY_AUX_CALL_CONTEXT.get()

    @contextmanager
    def scope():
        token = _RELAY_AUX_CALL_CONTEXT.set(relay_context)
        try:
            with scoped_fallback_policy(policy), aux_progress_hook(progress_hook), scoped_runtime_main(main_runtime):
                yield
        finally:
            _RELAY_AUX_CALL_CONTEXT.reset(token)

    def advance():
        while True:
            with scope():
                try:
                    chunk = next(stream)
                except StopIteration:
                    return
            yield chunk

    def close():
        with scope():
            _close_stream(stream)

    return _OwnedStream(advance(), close)


def release_sync_semaphore_after_stream(stream: Any, semaphore: Any):
    """Release a permit exactly once after consumption, error, or explicit close."""
    return _OwnedStream(iter(stream), semaphore.release)


def stream_with_recovery(send: Callable, recover: Callable):
    """Dispatch eagerly, using the completion ladder only before any chunk escapes."""
    try:
        stream = send()
    except Exception as exc:
        return recover(exc)
    if not is_stream_response(stream):
        return stream

    def advance():
        emitted = False
        try:
            for chunk in stream:
                # Even role/usage-only frames forbid replay of the request.
                emitted = True
                yield chunk
        except Exception as exc:
            if emitted:
                raise
            yield completed_response_as_stream_chunk(recover(exc))

    return _OwnedStream(advance(), lambda: _close_stream(stream))


def _close_stream(stream: Any):
    close = getattr(stream, "close", None)
    if callable(close):
        close()


def completed_response_as_stream_chunk(response: Any) -> Any:
    """Adapt a completed response into one delta stream chunk."""
    from types import SimpleNamespace
    choices = getattr(response, "choices", None)
    first_choice = choices[0] if isinstance(choices, (list, tuple)) and choices else None
    message = getattr(first_choice, "message", None)
    raw_tool_calls = getattr(message, "tool_calls", None)
    tool_call_deltas = None
    if isinstance(raw_tool_calls, (list, tuple)) and raw_tool_calls:
        tool_call_deltas = [
            SimpleNamespace(
                index=getattr(tc, "index", index), id=getattr(tc, "id", None), type=getattr(tc, "type", None) or "function",
                function=SimpleNamespace(
                    name=getattr(getattr(tc, "function", None), "name", None),
                    arguments=getattr(getattr(tc, "function", None), "arguments", None),
                ),
            )
            for index, tc in enumerate(raw_tool_calls)
        ]
    delta = SimpleNamespace(
        content=getattr(message, "content", None), tool_calls=tool_call_deltas,
        **{k: getattr(message, k, None) for k in ("reasoning_content", "reasoning", "reasoning_details")},
    )
    choice = SimpleNamespace(
        index=getattr(first_choice, "index", 0), delta=delta, finish_reason=getattr(first_choice, "finish_reason", None) or "stop",
    )
    return SimpleNamespace(
        id=getattr(response, "id", None), model=getattr(response, "model", None), choices=[choice], usage=getattr(response, "usage", None),
    )
