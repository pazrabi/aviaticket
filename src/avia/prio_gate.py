"""Приоритетный шлюз поисков."""
from __future__ import annotations

import asyncio
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from typing import Any


@dataclass
class _Waiter:
    priority: int
    seq: int
    fut: asyncio.Future[None]


class _SlotContext(AbstractAsyncContextManager):
    """Асинхронный контекстный менеджер слота."""

    def __init__(self, gate: PriorityGate, priority: int) -> None:
        self._gate = gate
        self._priority = int(priority)
        self._acquired = False

    async def __aenter__(self) -> _SlotContext:
        await self._gate._acquire(self._priority)
        self._acquired = True
        return self

    async def __aexit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        if self._acquired:
            self._gate._release()
            self._acquired = False


class PriorityGate:
    """Приоритетный шлюз для ограничения одновременных поисков."""

    def __init__(self, capacity: int) -> None:
        if capacity < 1:
            raise ValueError("capacity must be at least 1")
        self._capacity = int(capacity)
        self._active: int = 0
        self._waiters: list[_Waiter] = []
        self._seq: int = 0

    @property
    def capacity(self) -> int:
        return self._capacity

    def active(self) -> int:
        return self._active

    def waiting_by_priority(self) -> dict[int, int]:
        counts: dict[int, int] = {}
        for w in self._waiters:
            if not w.fut.done():
                counts[w.priority] = counts.get(w.priority, 0) + 1
        return counts

    def slot(self, priority: int) -> _SlotContext:
        return _SlotContext(self, priority)

    async def _acquire(self, priority: int) -> None:
        if self._active < self._capacity and not self._waiters:
            self._active += 1
            return

        loop = asyncio.get_running_loop()
        fut: asyncio.Future[None] = loop.create_future()
        waiter = _Waiter(priority=priority, seq=self._seq, fut=fut)
        self._seq += 1
        self._waiters.append(waiter)
        self._waiters.sort(key=lambda w: (w.priority, w.seq))
        self._maybe_start_next()

        try:
            await fut
        except asyncio.CancelledError:
            if fut.done() and not fut.cancelled():
                self._release()
            else:
                if waiter in self._waiters:
                    self._waiters.remove(waiter)
            raise

    def _maybe_start_next(self) -> None:
        while self._active < self._capacity and self._waiters:
            waiter = self._waiters.pop(0)
            if not waiter.fut.done():
                self._active += 1
                waiter.fut.set_result(None)

    def _release(self) -> None:
        self._active = max(0, self._active - 1)
        self._maybe_start_next()


__all__ = ["PriorityGate"]
