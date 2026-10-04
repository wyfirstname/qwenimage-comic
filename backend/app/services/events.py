# -*- coding: utf-8 -*-
"""进度事件总线：把推理进度推给 SSE 订阅者。"""

from __future__ import annotations

import asyncio
import json
from collections import defaultdict
from typing import Any, AsyncIterator


class EventBus:
    def __init__(self) -> None:
        self._subs: dict[str, list[asyncio.Queue]] = defaultdict(list)
        self._lock = asyncio.Lock()

    async def subscribe(self, job_id: str) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=100)
        async with self._lock:
            self._subs[job_id].append(q)
        return q

    async def unsubscribe(self, job_id: str, q: asyncio.Queue) -> None:
        async with self._lock:
            if q in self._subs.get(job_id, []):
                self._subs[job_id].remove(q)
            if not self._subs.get(job_id):
                self._subs.pop(job_id, None)

    async def publish(self, job_id: str, event: str, data: dict[str, Any]) -> None:
        payload = {"event": event, "data": data}
        async with self._lock:
            queues = list(self._subs.get(job_id, []))
        for q in queues:
            try:
                q.put_nowait(payload)
            except asyncio.QueueFull:
                pass

    async def stream(self, job_id: str) -> AsyncIterator[str]:
        """SSE 格式化输出。"""
        q = await self.subscribe(job_id)
        try:
            while True:
                try:
                    item = await asyncio.wait_for(q.get(), timeout=20)
                except asyncio.TimeoutError:
                    yield ": keep-alive\n\n"
                    continue
                event = item["event"]
                data = json.dumps(item["data"], ensure_ascii=False)
                yield f"event: {event}\ndata: {data}\n\n"
                if event in ("done", "error"):
                    break
        finally:
            await self.unsubscribe(job_id, q)


bus = EventBus()
