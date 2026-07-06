"""The run maintenance loop: durable wakeups, lease deadlines, claim expiry.

This is what makes supervision *server-owned*: a wakeup fires and submits a
wake turn whether or not any CLI is connected (its tool calls then wait
durably as leases), lapsed claims return to the pool, and leases past their
deadline resolve to synthesized expired results so a waiting round can always
complete. Single background task, panic-proof per tick.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from collections.abc import Callable

from ..config import Settings
from ..observability import get_logger
from .hub import EventHub
from .store import RunStore
from .supervisor import RunSupervisor

logger = get_logger("daintree.runs.scheduler")


class RunMaintenanceScheduler:
    def __init__(
        self,
        *,
        settings: Settings,
        store: RunStore,
        supervisor: RunSupervisor,
        hub: EventHub,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.settings = settings
        self.store = store
        self.supervisor = supervisor
        self.hub = hub
        self._clock = clock
        self._task: asyncio.Task[None] | None = None

    def start(self) -> None:
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None

    async def _loop(self) -> None:
        while True:
            try:
                await self.tick()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - the loop must survive any tick
                logger.warning("run_scheduler_tick_failed", error=str(exc))
            await asyncio.sleep(self.settings.runs_scheduler_tick_seconds)

    async def tick(self) -> None:
        now = self._clock()

        # 1. Lapsed claims back to the pool (executor died mid-execution).
        for outcome in await self.store.release_expired_claims(now=now):
            self.hub.publish_all(outcome.events)

        # 2. Leases past their deadline: synthesized expired results, then the
        #    waiting round may now be complete.
        expired = await self.store.expire_due_leases(now=now)
        touched_runs: set[str] = set()
        for outcome in expired:
            self.hub.publish_all(outcome.events)
            if outcome.lease is not None:
                touched_runs.add(outcome.lease.run_id)
        for run_id in touched_runs:
            await self.supervisor.on_lease_resolution(run_id)

        # 3. Wakeups whose claim committed but whose wake turn never landed —
        #    the crash window between the CAS and the submission. Retry the
        #    submission (idempotent on the wake-<id> turn_id).
        for wakeup in await self.store.fired_wakeups_without_turn():
            await self._submit_wake(wakeup.run_id, wakeup.wakeup_id, wakeup.note, now)

        # 4. Due wakeups: claim (CAS — an overlapping tick cannot double-fire),
        #    then submit a wake turn. A busy run gets the same wakeup pushed
        #    back (never a replacement — retries must not proliferate rows).
        for wakeup in await self.store.due_wakeups(now=now):
            claim = await self.store.claim_wakeup(wakeup.wakeup_id)
            if claim.kind != "fired":
                continue
            self.hub.publish_all(claim.events)
            await self._submit_wake(wakeup.run_id, wakeup.wakeup_id, wakeup.note, now)

    async def _submit_wake(
        self, run_id: str, wakeup_id: str, note: str | None, now: float
    ) -> None:
        outcome = await self.supervisor.submit_wake_turn(
            run_id=run_id, turn_id=f"wake-{wakeup_id}", note=note
        )
        if outcome.kind == "busy":
            retry_at = now + self.settings.runs_wake_busy_retry_seconds
            deferred = await self.store.defer_wakeup(wakeup_id, fire_at=retry_at)
            if deferred.kind == "deferred":
                self.hub.publish_all(deferred.events)
                logger.info("run_wakeup_deferred", run_id=run_id, retry_at=retry_at)
        elif outcome.kind not in ("accepted", "duplicate"):
            logger.warning(
                "run_wakeup_submit_failed", run_id=run_id, outcome=outcome.kind
            )
