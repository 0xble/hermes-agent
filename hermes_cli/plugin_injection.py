"""Opt-in plugin admission observations (upstream proposal #96179's handle contract).

A timeout observes uncertainty, not cancellation or refusal. The caller must retain
its identity until a durable receipt or transcript readback resolves that uncertainty.
"""
from __future__ import annotations

import asyncio
import concurrent.futures
from contextvars import ContextVar
from dataclasses import dataclass
import uuid


@dataclass(frozen=True)
class GatewayInjectionResult:
    accepted: bool
    reason: str
    session_id: str | None = None
    session_key: str | None = None
    correlation_id: str | None = None
    settled: bool = True
    admission_id: str | None = None

    @property
    def indeterminate(self) -> bool:
        return not self.settled

    @property
    def refused(self) -> bool:
        return not self.accepted and not self.indeterminate

    @property
    def safe_to_retry(self) -> bool:
        return self.refused

    def __bool__(self) -> bool:
        return self.accepted


class InjectionDelivery:
    """Per-dispatch boundary proof; never inherited as a process-global flag."""
    def __init__(self, session_key=None, correlation_id=None):
        self.session_key = session_key
        self.correlation_id = correlation_id or uuid.uuid4().hex
        self.session_id: str | None = None
        self.started = False
        self.owner_task = None
        self.observing = False
        self.receipt: GatewayInjectionResult | None = None

    def result(self, accepted, reason, *, indeterminate=False, admission_id=None):
        return GatewayInjectionResult(accepted, reason, session_id=self.session_id,
                                      session_key=self.session_key, correlation_id=self.correlation_id,
                                      settled=not indeterminate, admission_id=admission_id)

    def finish(self, accepted, reason, *, admission_id=None):
        self.receipt = self.result(accepted, reason, admission_id=admission_id)

    async def observe(self, coro):
        self.observing = True
        self.owner_task = asyncio.current_task()
        token = current_delivery.set(self)
        try:
            return await coro
        finally:
            current_delivery.reset(token)


current_delivery: ContextVar[InjectionDelivery | None] = ContextVar("plugin_injection_delivery", default=None)


def active_delivery():
    delivery = current_delivery.get()
    # Admission may spawn a FIFO drain with an inherited context. That child
    # must not mutate its producer's observation (nor any later plugin call).
    return delivery if delivery is not None and delivery.owner_task is asyncio.current_task() else None


class GatewayInjectionHandle:
    """Thread-safe admission waiter, also awaitable on the gateway loop.

    ``result`` must never block the gateway loop. Timeout does NOT cancel the
    dispatch or mutate the eventual result; later observations can see its commit.
    """
    def __init__(self, future: concurrent.futures.Future, delivery: InjectionDelivery, *, loop=None,
                 dispatch=None):
        self._future, self._delivery = future, delivery
        self._loop, self._dispatch = loop, dispatch

    @classmethod
    def resolved(cls, result: GatewayInjectionResult):
        future = concurrent.futures.Future()
        future.set_result(result)
        return cls(future, InjectionDelivery(result.session_key, result.correlation_id))

    @classmethod
    def track(cls, dispatch, delivery, loop, dispatch_coro=None):
        future = concurrent.futures.Future()
        handle = cls(future, delivery, loop=loop, dispatch=dispatch)

        def complete(done):
            # A commit proof outranks cancellation in the callback/dispatch race.
            try:
                accepted = bool(done.result())
                result = delivery.result(
                    accepted, "adopted" if accepted else "not_routed",
                    indeterminate=delivery.started and not accepted)
            except (asyncio.CancelledError, concurrent.futures.CancelledError):
                result = delivery.result(
                    False, "cancelled", indeterminate=True)
            except Exception:
                result = delivery.result(
                    False, "dispatch_error", indeterminate=delivery.started)
            if delivery.receipt is not None:
                result = delivery.receipt
            if not delivery.observing and dispatch_coro is not None:
                dispatch_coro.close()  # cancelled before the wrapper ever awaited it
            future.set_result(result)

        dispatch.add_done_callback(complete)
        return handle

    @property
    def correlation_id(self):
        return self._delivery.correlation_id

    @property
    def settled(self) -> bool:
        return self._future.done()

    def done(self):
        return self.settled

    def result(self, timeout=None) -> GatewayInjectionResult:
        try:
            running = asyncio.get_running_loop()
        except RuntimeError:
            running = None
        if running is not None and running is self._loop and not self.done():
            raise RuntimeError("await the injection handle on the gateway loop; result() would block it")
        try:
            return self._future.result(timeout)
        except concurrent.futures.TimeoutError:
            return self._delivery.result(False, "timeout", indeterminate=True)

    async def wait(self, timeout=None) -> GatewayInjectionResult:
        try:
            return await asyncio.wait_for(asyncio.shield(asyncio.wrap_future(self._future)), timeout)
        except asyncio.TimeoutError:
            return self._delivery.result(False, "timeout", indeterminate=True)
        except asyncio.CancelledError:
            # Cancelling an observer never cancels the possibly committed dispatch.
            return self._delivery.result(False, "cancelled", indeterminate=True)

    def __await__(self):
        return self.wait().__await__()

    def cancel(self):
        """Request cancellation; only dispatch's boundary proof can establish refusal."""
        if self._dispatch is None or self.done():
            return False
        if isinstance(self._dispatch, asyncio.Future):
            assert self._loop is not None
            self._loop.call_soon_threadsafe(self._dispatch.cancel)
        else:
            self._dispatch.cancel()
        return True

    def __bool__(self):
        raise TypeError("await GatewayInjectionHandle or call result() off the gateway loop")


def immediate_result(accepted: bool, reason: str, *, session_key=None, correlation_id=None):
    delivery = InjectionDelivery(session_key, correlation_id)
    return GatewayInjectionHandle.resolved(delivery.result(accepted, reason))
