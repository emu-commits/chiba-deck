import asyncio
import json
import logging
import re
import time

import paho.mqtt.client as mqtt

from .events import Event, EventQueue, EventType

log = logging.getLogger(__name__)


class MQTTTransport:
    def __init__(self, config, node_id: str, event_queue: EventQueue):
        self._cfg = config
        self._node_id = node_id
        self._eq = event_queue
        self._client: mqtt.Client | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._connected = False
        self._cooldowns: dict[str, float] = {}

    def _on_connect(self, client, userdata, *args):
        # paho v1: args = (flags_dict, rc_int)
        # paho v2: args = (connect_flags, reason_code, properties)
        rc = args[1] if len(args) >= 2 else (args[0] if args else 0)
        try:
            rc_value = rc.value  # paho v2 ReasonCode
        except AttributeError:
            rc_value = int(rc)   # paho v1 integer

        if rc_value == 0:
            self._connected = True
            client.subscribe(self._cfg.mqtt.topic_rx)
            log.info(f"MQTT connected → {self._cfg.mqtt.topic_rx}")
        else:
            log.error(f"MQTT connect failed rc={rc_value}")

    def _on_message(self, client, userdata, msg):
        try:
            text = msg.payload.decode()
            data = json.loads(text)
            event = self._parse(data)
            if event and self._loop and not self._loop.is_closed():
                self._loop.call_soon_threadsafe(self._eq.put_nowait, event)
        except Exception as e:
            log.debug(f"transport parse error: {e}")

    def _parse(self, data: dict) -> Event | None:
        from_node = data.get("from", "")
        to_node = data.get("to", "")
        payload = data.get("payload", data.get("text", ""))

        if not payload:
            return None

        if payload.startswith(">:"):
            caps = re.findall(r'\?(\w+)', payload.split("|")[0])
            return Event(
                type=EventType.HEARTBEAT,
                from_node=from_node,
                payload=payload,
                meta={"caps": caps},
            )

        return Event(
            type=EventType.MESSAGE,
            from_node=from_node,
            to_node=to_node,
            payload=payload,
        )

    async def connect(self):
        self._loop = asyncio.get_event_loop()
        try:
            self._client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
        except AttributeError:
            # paho < 2.0 fallback
            self._client = mqtt.Client()

        self._client.on_connect = self._on_connect
        self._client.on_message = self._on_message

        try:
            self._client.connect(self._cfg.mqtt.broker, self._cfg.mqtt.port, keepalive=60)
            self._client.loop_start()
            for _ in range(20):
                if self._connected:
                    break
                await asyncio.sleep(0.25)
            if not self._connected:
                log.warning("MQTT not connected — running in offline mode")
        except Exception as e:
            log.warning(f"MQTT unavailable: {e} — offline mode")

    def send_dm(self, to_node: str, text: str) -> bool:
        if not self._connected or not self._client:
            log.debug(f"send_dm offline: {to_node}: {text[:60]}")
            return False
        msg = json.dumps({"from": self._node_id, "to": to_node, "type": "text", "payload": text})
        self._client.publish(self._cfg.mqtt.topic_tx, msg)
        log.debug(f"DM → {to_node}: {text[:60]}")
        return True

    def send_broadcast(self, text: str):
        if not self._connected or not self._client:
            log.debug(f"broadcast offline: {text[:60]}")
            return
        msg = json.dumps({"from": self._node_id, "type": "text", "payload": text})
        self._client.publish(self._cfg.mqtt.topic_tx, msg)
        log.info(f"broadcast: {text[:80]}")

    def can_reply_to(self, node_id: str, cooldown_s: int = 15) -> bool:
        now = time.time()
        if now - self._cooldowns.get(node_id, 0) >= cooldown_s:
            self._cooldowns[node_id] = now
            return True
        return False

    @property
    def connected(self) -> bool:
        return self._connected

    def disconnect(self):
        if self._client:
            try:
                self._client.loop_stop()
                self._client.disconnect()
            except Exception:
                pass


class BLETransport:
    """Stub — implement when bleak hardware is available."""

    async def connect(self):
        raise NotImplementedError("BLE transport not yet implemented")
