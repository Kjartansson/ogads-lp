"""Tiny in-process TTL cache.

OGAds personalises the offer list per visitor IP, so the cache key is the
full request parameter tuple. The point is not to serve one visitor from
another's list -- it is to survive a page refresh, a bot hammering /, or a
TikTok traffic spike without one upstream call per pageview.
"""
from __future__ import annotations

import asyncio
import time
from typing import Any, Awaitable, Callable, Hashable


class TTLCache:
    def __init__(self, ttl: int = 300, max_entries: int = 2000):
        self.ttl = ttl
        self.max_entries = max_entries
        self._data: dict[Hashable, tuple[float, Any]] = {}
        self._locks: dict[Hashable, asyncio.Lock] = {}
        self._guard = asyncio.Lock()
        self.hits = 0
        self.misses = 0

    def _evict(self) -> None:
        now = time.monotonic()
        for k in [k for k, (exp, _) in self._data.items() if exp <= now]:
            self._data.pop(k, None)
        # Hard cap as a backstop: without it a per-IP key space grows with
        # unique visitors and the process leaks for as long as it runs.
        while len(self._data) > self.max_entries:
            self._data.pop(next(iter(self._data)), None)

    def get(self, key: Hashable) -> Any | None:
        entry = self._data.get(key)
        if entry is None:
            self.misses += 1
            return None
        expires, value = entry
        if expires <= time.monotonic():
            self._data.pop(key, None)
            self.misses += 1
            return None
        self.hits += 1
        return value

    def set(self, key: Hashable, value: Any) -> None:
        self._data[key] = (time.monotonic() + self.ttl, value)
        self._evict()

    async def get_or_set(self, key: Hashable, producer: Callable[[], Awaitable[Any]]) -> Any:
        """Single-flight: concurrent misses on the same key make one call."""
        cached = self.get(key)
        if cached is not None:
            return cached
        async with self._guard:
            lock = self._locks.setdefault(key, asyncio.Lock())
        async with lock:
            cached = self.get(key)          # another waiter may have filled it
            if cached is not None:
                return cached
            value = await producer()
            self.set(key, value)
            return value

    def stats(self) -> dict:
        return {
            "entries": len(self._data),
            "hits": self.hits,
            "misses": self.misses,
            "ttl_seconds": self.ttl,
        }

    def clear(self) -> None:
        self._data.clear()
