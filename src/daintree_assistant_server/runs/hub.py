"""In-memory per-run event broadcast for live SSE tails.

Durability lives in the store; the hub only fans out already-committed events
(publish-after-commit) plus ephemeral model deltas to connected subscribers. A
subscriber that falls behind its bounded buffer is marked lagged and its stream
ends with a ``stream.reset`` event — the client reconnects with its last seen
``seq`` and replays from the store, so nothing durable is ever lost to
backpressure.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field

from .models import EventRecord


@dataclass(eq=False)  # identity semantics: each subscriber is its own object
class Subscription:
    run_id: str
    queue: asyncio.Queue[EventRecord] = field(
        default_factory=lambda: asyncio.Queue(maxsize=1024)
    )
    lagged: bool = False


class EventHub:
    def __init__(self) -> None:
        self._subs: dict[str, set[Subscription]] = {}

    def subscribe(self, run_id: str) -> Subscription:
        sub = Subscription(run_id=run_id)
        self._subs.setdefault(run_id, set()).add(sub)
        return sub

    def unsubscribe(self, sub: Subscription) -> None:
        subs = self._subs.get(sub.run_id)
        if subs is not None:
            subs.discard(sub)
            if not subs:
                self._subs.pop(sub.run_id, None)

    def publish(self, event: EventRecord) -> None:
        for sub in self._subs.get(event.run_id, ()):  # copy not needed: no mutation here
            if sub.lagged:
                continue
            try:
                sub.queue.put_nowait(event)
            except asyncio.QueueFull:
                # Never block or drop silently: flag the subscriber so its SSE
                # loop emits stream.reset and the client replays from the store.
                sub.lagged = True

    def publish_all(self, events: list[EventRecord]) -> None:
        for event in events:
            self.publish(event)

    def subscriber_count(self, run_id: str) -> int:
        return len(self._subs.get(run_id, ()))
