import asyncio
import logging
import random

from .registry import ServiceRegistry
from .transport import MQTTTransport

log = logging.getLogger(__name__)


class HeartbeatService:
    def __init__(self, config, registry: ServiceRegistry, transport: MQTTTransport, node_handle: str):
        self._cfg = config
        self._registry = registry
        self._transport = transport
        self._handle = node_handle

    def _payload(self) -> str:
        caps = self._registry.local_caps()
        caps_str = " ".join(f"?{c}" for c in caps)
        return f">: {caps_str} | {self._handle} v0.1"

    async def run(self):
        interval = self._cfg.mesh.heartbeat_interval_seconds
        jitter = self._cfg.mesh.heartbeat_jitter_seconds

        await asyncio.sleep(5)
        self._broadcast()

        while True:
            delay = interval + random.randint(0, jitter)
            log.info(f"next heartbeat in {delay}s")
            await asyncio.sleep(delay)
            self._broadcast()

    def _broadcast(self):
        payload = self._payload()
        self._transport.send_broadcast(payload)
        log.info(f"heartbeat: {payload}")
