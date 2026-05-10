import asyncio
import base64
import json
import logging
import re
import time

import paho.mqtt.client as mqtt

from .events import Event, EventQueue, EventType

log = logging.getLogger(__name__)


RETRY_DELAY = 15  # seconds between reconnect attempts

_BROADCAST_IDS = {"^all", "4294967295", str(0xFFFFFFFF)}


def _node_id_str(v) -> str:
    """Convert a Meshtastic node ID (int or str) to !hexid; return '' for broadcast."""
    if v is None or v == "":
        return ""
    if isinstance(v, int):
        if v == 0xFFFFFFFF:
            return ""
        return f"!{v:08x}"
    s = str(v)
    return "" if s in _BROADCAST_IDS else s


class MQTTTransport:
    def __init__(self, config, node_id: str, event_queue: EventQueue):
        self._cfg = config
        self._node_id = node_id
        self._eq = event_queue
        self._client: mqtt.Client | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._connected = False
        self._cooldowns: dict[str, float] = {}
        self._status_cb = None   # callable(str) — fed into stream panel
        self._token_cb = None    # callable(bytes, str) — inbound token handler

    def set_status_cb(self, cb):
        self._status_cb = cb

    def set_token_cb(self, cb):
        """Register callback for inbound 225-byte token messages: cb(token_bytes, from_node)."""
        self._token_cb = cb

    def _emit(self, msg: str):
        log.info(f"MQTT: {msg}")
        if self._status_cb and self._loop and not self._loop.is_closed():
            self._loop.call_soon_threadsafe(self._status_cb, msg)

    def _derived_topic(self, suffix: str) -> str | None:
        rx = self._cfg.mqtt.topic_rx
        candidate = rx.replace("/rx", f"/{suffix}")
        return candidate if candidate != rx else None

    def _nodes_topic(self) -> str | None:
        return self._derived_topic("nodes")

    def _history_topic(self) -> str | None:
        return self._derived_topic("history")

    def _gateway_topic(self) -> str | None:
        return self._derived_topic("gateway")

    def _subscribe_topics(self, client):
        client.subscribe(self._cfg.mqtt.topic_rx)
        for t in (self._nodes_topic(), self._history_topic(), self._gateway_topic()):
            if t:
                client.subscribe(t)

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
            self._subscribe_topics(client)
            self._emit(f"connected ✓  broker={self._cfg.mqtt.broker}:{self._cfg.mqtt.port}  topic={self._cfg.mqtt.topic_rx}")
        else:
            self._emit(f"broker refused connection (rc={rc_value})")

    def _on_disconnect(self, client, userdata, *args):
        self._connected = False
        self._emit(f"disconnected from {self._cfg.mqtt.broker} — will retry in {RETRY_DELAY}s")

    def _on_message(self, client, userdata, msg):
        try:
            # Retained messages on topic_rx are redelivered on reconnect — skip them;
            # the history topic is the authoritative replay source.
            if msg.retain and msg.topic == self._cfg.mqtt.topic_rx:
                return

            text = msg.payload.decode()
            data = json.loads(text)

            # Nodes-list topic: bulk import all known nodes silently
            nt = self._nodes_topic()
            if nt and msg.topic == nt and isinstance(data, list):
                for node in data:
                    node_id = _node_id_str(node.get("id", ""))
                    if not node_id:
                        continue
                    event = Event(
                        type=EventType.HEARTBEAT,
                        from_node=node_id,
                        payload=node.get("longName", node_id),
                        meta={"handle": node.get("longName", ""), "caps": [], "silent": True},
                    )
                    if self._loop and not self._loop.is_closed():
                        self._loop.call_soon_threadsafe(self._eq.put_nowait, event)
                return

            # Gateway topic: auto-configure our own node ID
            gt = self._gateway_topic()
            if gt and msg.topic == gt and isinstance(data, dict):
                node_id = _node_id_str(data.get("id", ""))
                if node_id and node_id != self._cfg.node_id:
                    self._cfg.node_id = node_id
                    self._node_id = node_id
                    log.info(f"node ID auto-set from gateway: {node_id}")
                return

            # History topic: replay last N messages into the stream
            ht = self._history_topic()
            if ht and msg.topic == ht and isinstance(data, list):
                for item in sorted(data, key=lambda x: x.get("ts", 0)):
                    from_node = _node_id_str(item.get("from", ""))
                    to_node = _node_id_str(item.get("to", ""))
                    text = item.get("text", "")
                    name = item.get("name", "")
                    if not text or not from_node:
                        continue
                    meta = {"history": True}
                    if name:
                        meta["from_handle"] = name
                    event = Event(
                        type=EventType.MESSAGE,
                        ts=item.get("ts", time.time()),
                        from_node=from_node,
                        to_node=to_node,
                        payload=text,
                        meta=meta,
                    )
                    if self._loop and not self._loop.is_closed():
                        self._loop.call_soon_threadsafe(self._eq.put_nowait, event)
                # Sentinel: signals the drain that all history has arrived
                sentinel = Event(type=EventType.SYSTEM, meta={"history_end": True})
                if self._loop and not self._loop.is_closed():
                    self._loop.call_soon_threadsafe(self._eq.put_nowait, sentinel)
                return

            event = self._parse(data)
            if event and self._loop and not self._loop.is_closed():
                self._loop.call_soon_threadsafe(self._eq.put_nowait, event)
        except Exception as e:
            log.debug(f"transport parse error: {e}")

    def _parse(self, data: dict) -> Event | None:
        raw_from = data.get("from") if data.get("from") else data.get("sender", "")
        from_node = _node_id_str(raw_from)
        to_node = _node_id_str(data.get("to", ""))
        msg_type = data.get("type", "")

        # Pi bridge longName field (added by our bridge patch)
        name = data.get("name", "")

        # Standard Meshtastic JSON nodeinfo packet
        if msg_type == "nodeinfo":
            pl = data.get("payload", {})
            if isinstance(pl, dict):
                handle = (pl.get("longname") or pl.get("longName") or
                          pl.get("shortname") or pl.get("shortName") or "")
            else:
                handle = name
            if not from_node:
                return None
            return Event(
                type=EventType.HEARTBEAT,
                from_node=from_node,
                payload=handle or from_node,
                meta={"handle": handle, "caps": []},
            )

        # Ignore other standard Meshtastic packet types
        if msg_type in ("position", "telemetry", "waypoint", "routing", "admin"):
            return None

        # Chiba heartbeat received off port 258 (not from chat channel)
        if msg_type == "heartbeat":
            hb_payload = data.get("payload", "")
            caps = re.findall(r'\?(\w+)', hb_payload.split("|")[0])
            return Event(
                type=EventType.HEARTBEAT,
                from_node=from_node,
                payload=hb_payload,
                meta={"caps": caps},
            )

        # Pubkey announcement — silent heartbeat, only parsed by chiba-deck nodes
        if msg_type == "pubkey_announce":
            pubkey_hex = data.get("payload", "")
            if from_node and isinstance(pubkey_hex, str) and len(pubkey_hex) == 64:
                return Event(
                    type=EventType.HEARTBEAT,
                    from_node=from_node,
                    meta={"pubkey": pubkey_hex, "caps": [], "silent": True},
                )
            return None

        # 225-byte bound token (Port 256): base64-encoded binary payload
        if msg_type == "token":
            if self._token_cb is not None:
                raw_b64 = data.get("payload", "")
                if isinstance(raw_b64, str):
                    try:
                        token_bytes = base64.b64decode(raw_b64)
                        if self._loop and not self._loop.is_closed():
                            self._loop.call_soon_threadsafe(self._token_cb, token_bytes, from_node)
                    except Exception as e:
                        log.debug(f"token decode error from {from_node}: {e}")
            return None

        # Resolve text: Pi bridge uses top-level "text"; standard JSON uses payload.text
        payload = data.get("payload", data.get("text", ""))
        if isinstance(payload, dict):
            payload = payload.get("text", "")

        if not payload:
            return None

        if isinstance(payload, str) and payload.startswith(">:"):
            caps = re.findall(r'\?(\w+)', payload.split("|")[0])
            return Event(
                type=EventType.HEARTBEAT,
                from_node=from_node,
                payload=payload,
                meta={"caps": caps},
            )

        return Event(
            type=EventType.MESSAGE,
            ts=data.get("ts", time.time()),
            from_node=from_node,
            to_node=to_node,
            payload=str(payload),
            meta={"from_handle": name} if name else {},
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

    def send_chiba_broadcast(self, payload: str):
        """Send a heartbeat on port 258 — NOT the chat channel."""
        if not self._connected or not self._client:
            log.debug(f"send_chiba_broadcast offline: {payload[:60]}")
            return
        msg = json.dumps({"from": self._node_id, "type": "heartbeat", "payload": payload})
        self._client.publish(self._cfg.mqtt.topic_tx, msg)
        log.debug(f"chiba broadcast: {payload[:60]}")

    def send_pubkey_announce(self, pubkey_hex: str):
        if not self._connected or not self._client:
            return
        msg = json.dumps({"from": self._node_id, "type": "pubkey_announce", "payload": pubkey_hex})
        self._client.publish(self._cfg.mqtt.topic_tx, msg)

    def send_token(self, to_node: str, token_bytes: bytes) -> bool:
        if not self._connected or not self._client:
            log.debug(f"send_token offline: {to_node}")
            return False
        payload = base64.b64encode(token_bytes).decode()
        msg = json.dumps({"from": self._node_id, "to": to_node, "type": "token", "payload": payload})
        self._client.publish(self._cfg.mqtt.topic_tx, msg)
        log.debug(f"token → {to_node}: {len(token_bytes)} bytes")
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

        # Set callback before subscribing so retained messages aren't missed
        self._client.on_message = scanner
        self._client.subscribe("msh/#")
        await asyncio.sleep(timeout)
        self._client.on_message = orig
        self._client.unsubscribe("msh/#")
        self._subscribe_topics(self._client)
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
