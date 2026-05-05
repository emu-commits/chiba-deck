import asyncio
import time
from dataclasses import dataclass, field
from enum import Enum


class EventType(str, Enum):
    HEARTBEAT = "heartbeat"
    MESSAGE = "message"
    PAYMENT_IN = "payment_in"
    PAYMENT_OUT = "payment_out"
    NODE_SEEN = "node_seen"
    SYSTEM = "system"


@dataclass
class Event:
    type: EventType
    ts: float = field(default_factory=time.time)
    from_node: str = ""
    to_node: str = ""
    payload: str = ""
    meta: dict = field(default_factory=dict)


class EventQueue:
    def __init__(self):
        self._q: asyncio.Queue[Event] = asyncio.Queue()

    async def put(self, event: Event):
        await self._q.put(event)

    def put_nowait(self, event: Event):
        self._q.put_nowait(event)

    async def get(self) -> Event:
        return await self._q.get()

    def qsize(self) -> int:
        return self._q.qsize()
