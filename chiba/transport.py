import asyncio
import json
import logging
import re
import time

import paho.mqtt.client as mqtt

from .events import Event, EventQueue, EventType

log = logging.getLogger(__name__)


RETRY_DELAY = 15  # seconds between reconnect attempts


class MQTTTransport:
    def __init__(self, config, node_id: str, event_queue: EventQueue):
        self._cfg = config
        self._node_id = node_id
        self._eq = event_queue
        self._client: mqtt.Client | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._connected = False
        self._cooldowns: dict[str, float] = {}
        self._status_cb = None  # callable(str) — fed into stream panel

    def set_status_cb(self, cb):
        self._status_cb = cb

    def _emit(self, msg: str):
        log.info(f"MQTT: {msg}")
        if self._status_cb and self._loop and not self._loop.is_closed():
            self._loop.call_soon_threadsafe(self._status_cb, msg)

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
            self._emit(f"connected ✓  broker={self._cfg.mqtt.broker}:{self._cfg.mqtt.port}  topic={self._cfg.mqtt.topic_rx}")
        else:
            self._emit(f"broker refused connection (rc={rc_value})")

    def _on_disconnect(self, client, userdata, *args):
        self._connected = False
        self._emit(f"disconnected from {self._cfg.mqtt.broker} — will retry in {RETRY_DELAY}s")

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

    def _make_client(self):
        try:
            client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
        except AttributeError:
            client = mqtt.Client()
        client.on_connect = self._on_connect
        client.on_disconnect = self._on_disconnect
        client.on_message = self._on_message
        return client

    async def connect_loop(self):
        """Background task: connect and auto-reconnect forever."""
        self._loop = asyncio.get_event_loop()
        broker = self._cfg.mqtt.broker
        port = self._cfg.mqtt.port

        while True:
            self._emit(f"connecting to {broker}:{port} ...")
            try:
                if self._client is None:
                    self._client = self._make_client()

                self._client.connect(broker, port, keepalive=60)
                self._client.loop_start()

                # Wait up to 8 seconds for on_connect to fire
                for _ in range(32):
                    if self._connected:
                        break
                    await asyncio.sleep(0.25)

                if not self._connected:
                    self._emit(
                        f"timeout — no response from {broker}:{port} after 8s  "
                        f"(is mosquitto running? is the Pi reachable?)"
                    )
                    self._client.loop_stop()
                    self._client = None
                    await asyncio.sleep(RETRY_DELAY)
                    continue

                # Stay connected — poll until drop
                while self._connected:
                    await asyncio.sleep(2)

                # on_disconnect already emitted the message
                self._client.loop_stop()
                self._client = None
                await asyncio.sleep(RETRY_DELAY)

            except asyncio.CancelledError:
                break
            except OSError as e:
                self._emit(f"network error: {e}  (retrying in {RETRY_DELAY}s)")
                if self._client:
                    try:
                        self._client.loop_stop()
                    except Exception:
                        pass
                    self._client = None
                await asyncio.sleep(RETRY_DELAY)
            except Exception as e:
                self._emit(f"unexpected error: {e}  (retrying in {RETRY_DELAY}s)")
                self._client = None
                await asyncio.sleep(RETRY_DELAY)

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

    async def scan_topics(self, timeout: float = 5.0) -> list[str]:
        """Subscribe to msh/# for `timeout` seconds and return unique topics seen."""
        if not self._connected or not self._client:
            return []

        found: set[str] = set()
        orig = self._client.on_message

        def scanner(client, userdata, msg):
            found.add(msg.topic)
            orig(client, userdata, msg)

        self._client.subscribe("msh/#")
        self._client.on_message = scanner
        await asyncio.sleep(timeout)
        self._client.on_message = orig
        self._client.unsubscribe("msh/#")
        # Re-ensure we're on our configured topic
        self._client.subscribe(self._cfg.mqtt.topic_rx)
        return sorted(found)

    def update_topics(self, topic_rx: str, topic_tx: str):
        """Hot-swap subscribed topics without reconnecting."""
        old_rx = self._cfg.mqtt.topic_rx
        self._cfg.mqtt.topic_rx = topic_rx
        self._cfg.mqtt.topic_tx = topic_tx
        if self._connected and self._client:
            if old_rx != topic_rx:
                self._client.unsubscribe(old_rx)
                self._client.subscribe(topic_rx)
            self._emit(f"topics updated → rx={topic_rx}  tx={topic_tx}")

    def trigger_reconnect(self):
        """Force a reconnect cycle (e.g. after broker change)."""
        self._connected = False
        if self._client:
            try:
                self._client.loop_stop()
                self._client.disconnect()
            except Exception:
                pass
            self._client = None

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
