"""The wake loop: turns each pulled wake into one inbound message and settles every wake.

Hermes-free on purpose: `HuddlesWakeLoop` knows nothing about the gateway, so the tests can run
the whole wake -> read -> deliver -> reply/observe flow against a fake Huddles. `adapter.py`
wires it to the Hermes platform adapter.
"""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from typing import Awaitable, Callable

try:
    from .huddles_client import HuddlesAuthError, HuddlesClient, Wake
except ImportError:  # flat load
    from huddles_client import HuddlesAuthError, HuddlesClient, Wake

logger = logging.getLogger(__name__)

DEFAULT_WAIT_SECONDS = 20
DEFAULT_BACKOFF_MAX_SECONDS = 30
DEFAULT_NO_OP_AFTER_SECONDS = 600

#: A long poll's own request timeout must exceed the server-side wait.
POLL_TIMEOUT_MARGIN_SECONDS = 15.0

Delay = Callable[[float], Awaitable[None]]
Now = Callable[[], float]


@dataclass
class PendingWake:
    """The wake a chat owes an answer for, and the finalizer watching it."""

    wake_id: str
    participation_id: str
    task: asyncio.Task[None] | None = None


class HuddlesWakeLoop:
    """Long-polls the wake feed and delivers one message per wake.

    `deliver(text, chat_id)` is the sink (the adapter's `handle_message`); `send(chat_id, text)`
    is the outbound path (the adapter's `send`), which answers the pending wake with
    `huddles_reply`. A wake nobody answered by the deadline is settled with `huddles_observe`
    `no_op` — the same courtesy a webhook bot that accepts and dies never gets.
    """

    def __init__(
        self,
        client: HuddlesClient,
        deliver: Callable[[str, str], Awaitable[None]],
        *,
        wait_seconds: int = DEFAULT_WAIT_SECONDS,
        backoff_max_seconds: float = DEFAULT_BACKOFF_MAX_SECONDS,
        no_op_after_seconds: float = DEFAULT_NO_OP_AFTER_SECONDS,
        on_disconnected: Callable[[str], None] | None = None,
        delay: Delay | None = None,
        now: Now | None = None,
    ) -> None:
        self._client = client
        self._deliver = deliver
        self._wait_seconds = max(0, min(int(wait_seconds), 25))
        self._backoff_max = float(backoff_max_seconds)
        self._no_op_after = float(no_op_after_seconds)
        self._on_disconnected = on_disconnected
        self._delay = delay or asyncio.sleep
        self._now = now or time.monotonic
        self._pending: dict[str, PendingWake] = {}
        self._seen: dict[str, float] = {}
        self._stopped = asyncio.Event()
        self._task: asyncio.Task[None] | None = None

    # -- lifecycle -----------------------------------------------------------

    def start(self) -> asyncio.Task[None]:
        if self._task is None:
            self._task = asyncio.create_task(self.run())
        return self._task

    def stop(self) -> None:
        self._stopped.set()

    async def run(self) -> None:
        """Poll until stopped. A dead grant ends the loop (a new invite is the only cure);
        network trouble backs off, capped at `backoff_max_seconds`."""
        backoff = 1.0
        while not self._stopped.is_set():
            try:
                wake = await self._client.next_wake(self._wait_seconds)
            except HuddlesAuthError as cause:
                logger.error("%s", cause)
                if self._on_disconnected is not None:
                    self._on_disconnected(str(cause))
                self.stop()
                return
            except Exception:  # noqa: BLE001 — the loop must survive any transport trouble
                logger.exception("huddles wake poll failed; backing off")
                await self._sleep(backoff)
                backoff = min(backoff * 2, self._backoff_max)
                continue
            backoff = 1.0
            if wake is None:
                await self._sleep(0)
                continue
            await self._handle_wake(wake)

    # -- wake handling -------------------------------------------------------

    async def poll_once(self) -> Wake | None:
        """One fetch, delivered if due. Used by tests and by `run`'s body."""
        wake = await self._client.next_wake(self._wait_seconds)
        if wake is not None:
            await self._handle_wake(wake)
        return wake

    async def _handle_wake(self, wake: Wake) -> None:
        if self._is_duplicate(wake.wake_id):
            return
        chat_id = wake.participation_id
        # One wake is open per chat: a newer wake replaces an unanswered one, which is settled
        # with no_op so it never lingers past its expiry unanswered.
        await self.observe(chat_id)
        pending = PendingWake(wake_id=wake.wake_id, participation_id=wake.participation_id)
        self._pending[chat_id] = pending
        read = await self._client.read_room(wake.participation_id, wake.wake_id)
        pending.task = asyncio.create_task(self._finalize_later(chat_id, pending))
        await self._deliver(read.transcript, chat_id)

    def _is_duplicate(self, wake_id: str) -> bool:
        """Huddles redelivers a wake whose delivery was not acknowledged; a wake this process
        already handled (delivered or observed) must not wake a second message."""
        now = self._now()
        for seen_id, at in list(self._seen.items()):
            if now - at > 600:
                del self._seen[seen_id]
        if wake_id in self._seen:
            return True
        self._seen[wake_id] = now
        return False

    async def _finalize_later(self, chat_id: str, pending: PendingWake) -> None:
        await self._delay(self._no_op_after)
        # Settle only the wake this finalizer was scheduled for: a newer wake already replaced it.
        if self._pending.get(chat_id) is pending:
            await self.observe(chat_id)

    async def send(self, chat_id: str, text: str) -> str | None:
        """Answer the chat's pending wake with `huddles_reply`; answers with the receipt's
        event id, or `None` when the chat had no pending wake (or Huddles refused)."""
        pending = self._pending.pop(chat_id, None)
        if pending is None:
            return None
        if pending.task is not None:
            pending.task.cancel()
        receipt = await self._client.reply(chat_id, pending.wake_id, text)
        events = receipt.get("event_ids") or []
        return events[0] if events else None

    async def observe(self, chat_id: str, outcome: str = "no_op") -> None:
        """Settle the chat's pending wake without a reply."""
        pending = self._pending.pop(chat_id, None)
        if pending is None:
            return
        if pending.task is not None:
            pending.task.cancel()
        await self._client.observe(chat_id, pending.wake_id, outcome)

    def wake_for(self, chat_id: str) -> str | None:
        """The wake `chat_id` still owes an answer for."""
        pending = self._pending.get(chat_id)
        return pending.wake_id if pending else None

    async def _sleep(self, seconds: float) -> None:
        if seconds <= 0:
            # Still yield, so a concurrent stop() is observed between empty polls.
            await asyncio.sleep(0)
            return
        try:
            await self._delay(seconds)
        except asyncio.CancelledError:
            raise
