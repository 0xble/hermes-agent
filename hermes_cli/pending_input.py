"""Native next-turn queue with original-arrival reinsertion for deferred input."""
from __future__ import annotations

from dataclasses import dataclass, replace
from queue import Queue
import time
from typing import Any


@dataclass(frozen=True)
class PendingInput:
    payload: Any
    arrival_order: int

    def __str__(self) -> str:
        return str(self.payload)


def capture_input(payload: Any) -> PendingInput:
    return PendingInput(payload, time.monotonic_ns())


def input_payload(item: Any) -> Any:
    return item.payload if isinstance(item, PendingInput) else item


def replace_input_payload(item: Any, payload: Any) -> Any:
    # /queue edit changes content in place without losing the entry's arrival or voice identity.
    return replace(item, payload=payload) if isinstance(item, PendingInput) else payload


class PendingInputQueue(Queue):
    """Keep Queue's mutex, notifications and task accounting, and return original raw payloads."""

    def _put(self, item: Any) -> None:
        # Queue calls this under its mutex. All ordinary admissions are stamped here, including
        # /queue, media, voice and background inputs. Only delayed admission needs reinsertion.
        if isinstance(item, PendingInput):
            for index, existing in enumerate(self.queue):
                if existing.arrival_order > item.arrival_order:
                    self.queue.insert(index, item)
                    return
        else:
            item = capture_input(item)
        self.queue.append(item)

    def _get(self) -> Any:
        return input_payload(self.queue.popleft())
