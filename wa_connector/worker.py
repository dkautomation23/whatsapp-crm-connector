"""The queue between "Meta is waiting for a 200" and "the CRM call finished".

Meta expects an ACK within seconds and retries the whole webhook when it does
not get one. CRM APIs are not that fast and occasionally fail. So the endpoint
does three cheap things - verify, parse, enqueue - and this worker does the
slow part, with retries, out of the request path.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Any

from .crm import CrmClient, handle_inbound
from .models import InboundMessage, StatusUpdate
from .store import Store

log = logging.getLogger(__name__)


@dataclass(slots=True)
class WorkerStats:
    received: int = 0
    stored: int = 0
    duplicates: int = 0
    failed: int = 0
    statuses: int = 0
    results: list[dict[str, Any]] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "received": self.received,
            "stored": self.stored,
            "duplicates": self.duplicates,
            "failed": self.failed,
            "statuses": self.statuses,
        }


class InboundWorker:
    """Single consumer over an asyncio queue.

    One consumer on purpose: messages from the same customer stay in order, and
    two workers cannot race to create the same contact twice.
    """

    def __init__(self, crm: CrmClient, store: Store, max_attempts: int = 3) -> None:
        self.crm = crm
        self.store = store
        self.max_attempts = max_attempts
        self.queue: asyncio.Queue[InboundMessage] = asyncio.Queue()
        self.stats = WorkerStats()
        self._task: asyncio.Task | None = None

    # -- lifecycle ---------------------------------------------------------

    async def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._run(), name="inbound-worker")

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

    async def drain(self) -> None:
        """Wait until the queue is empty - used by the demo and the tests."""
        await self.queue.join()

    # -- producing / consuming --------------------------------------------

    async def enqueue(self, message: InboundMessage) -> None:
        self.stats.received += 1
        await self.queue.put(message)

    def record_status(self, status: StatusUpdate) -> None:
        """Delivery receipts need no CRM write; failures are worth keeping."""
        self.stats.statuses += 1
        if status.status == "failed":
            self.store.record_failure("delivery", status.message_id, status.error or "failed")

    async def _run(self) -> None:
        while True:
            message = await self.queue.get()
            try:
                await self._process(message)
            finally:
                self.queue.task_done()

    async def _process(self, message: InboundMessage) -> None:
        """Retry the CRM write a few times, then park the error and move on."""
        for attempt in range(1, self.max_attempts + 1):
            try:
                result = await asyncio.to_thread(handle_inbound, message, self.crm, self.store)
                self.stats.results.append(result)
                if result["status"] == "duplicate":
                    self.stats.duplicates += 1
                else:
                    self.stats.stored += 1
                return
            except Exception as exc:  # noqa: BLE001 - the queue must survive anything
                log.warning("attempt %s/%s failed for %s: %s", attempt, self.max_attempts, message.message_id, exc)
                if attempt == self.max_attempts:
                    self.stats.failed += 1
                    # Never lost: the message id and the reason stay in the DB
                    # so it can be replayed after the CRM is back.
                    self.store.record_failure("inbound", message.message_id, str(exc))
                    return
                await asyncio.sleep(0.2 * attempt)
