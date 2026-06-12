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
        self._status_cb       = None   # callable(str)
        self._token_cb        = None   # callable(bytes, str) — inbound 225-byte token
        self._new_secret_cb   = None   # callable(bytes, str) — 32-byte new_secret sidecar
        self._spend_sidecar_cb = None  # callable(bytes, str) — 225-byte original spend token

    def set_status_cb(self, cb):
        self._status_cb = cb

    def set_token_cb(self, cb):
        """Register callback for inbound 225-byte token messages: cb(token_bytes, from_node)."""
        self._token_cb = cb

    def set_new_secret_cb(self, cb):
        """Register callback for inbound 32-byte new_secret sidecars: cb(secret_bytes, from_node)."""
        self._new_secret_cb = cb

    def set_spend_sidecar_cb(self, cb):
        """Register callback for inbound original spend token (re-spend sidecar): cb(token_bytes, from_node)."""
        self._spend_sidecar_cb = cb

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

        # 32-byte new_secret sidecar (Port 255): hex-encoded
        if msg_type == "new_secret":
            if self._new_secret_cb is not None:
                hex_val = data.get("payload", "")
                if isinstance(hex_val, str) and len(hex_val) == 64:
                    try:
                        secret_bytes = bytes.fromhex(hex_val)
                        if self._loop and not self._loop.is_closed():
                            self._loop.call_soon_threadsafe(self._new_secret_cb, secret_bytes, from_node)
                    except Exception as e:
                        log.debug(f"new_secret decode error from {from_node}: {e}")
            return None

        # 225-byte original spend token sidecar (Port 257): base64-encoded
        if msg_type == "spend_sidecar":
            if self._spend_sidecar_cb is not None:
                raw_b64 = data.get("payload", "")
                if isinstance(raw_b64, str):
                    try:
                        sidecar_bytes = base64.b64decode(raw_b64)
                        if self._loop and not self._loop.is_closed():
                            self._loop.call_soon_threadsafe(self._spend_sidecar_cb, sidecar_bytes, from_node)
                    except Exception as e:
                        log.debug(f"spend_sidecar decode error from {from_node}: {e}")
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

    def send_new_secret(self, to_node: str, new_secret_bytes: bytes) -> bool:
        """Send a 32-byte new_secret sidecar (Port 255) to the token recipient."""
        if not self._connected or not self._client:
            return False
        msg = json.dumps({
            "from": self._node_id, "to": to_node,
            "type": "new_secret", "payload": new_secret_bytes.hex(),
        })
        self._client.publish(self._cfg.mqtt.topic_tx, msg)
        return True

    def send_spend_sidecar(self, to_node: str, spend_token_bytes: bytes) -> bool:
        """Send the original 225-byte spend token as a re-spend sidecar (Port 257)."""
        if not self._connected or not self._client:
            return False
        payload = base64.b64encode(spend_token_bytes).decode()
        msg = json.dumps({
            "from": self._node_id, "to": to_node,
            "type": "spend_sidecar", "payload": payload,
        })
        self._client.publish(self._cfg.mqtt.topic_tx, msg)
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
    """
    Direct Meshtastic BLE transport using the meshtastic Python library.

    Requires: pip install "chiba-deck[ble]"
    (adds meshtastic>=2.3 and bleak>=0.21)

    Port assignments (Meshtastic private range 256–511):
      255 : new_secret sidecar (32 bytes) — always precedes a token
      256 : MeshCash token (225 bytes binary)
      257 : spend_sidecar (225 bytes) — original spend token, precedes a re-spend token
      259 : Payment pubkey announce (32 bytes binary)
    Port 1 (TEXT_MESSAGE_APP) carries all text, including heartbeats.

    config.ble.device_name — BLE device name or MAC; empty = auto-scan
    config.ble.adapter     — HCI adapter (e.g. "hci0"); empty = OS default
    """

    PORT_NEW_SECRET    = 255
    PORT_TOKEN         = 256
    PORT_SPEND_SIDECAR = 257
    PORT_PUBKEY        = 259

    def __init__(self, config, node_id: str, event_queue: EventQueue):
        self._cfg              = config
        self._node_id          = node_id
        self._eq               = event_queue
        self._iface            = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._connected        = False
        self._subscribed       = False
        self._status_cb        = None
        self._token_cb         = None
        self._new_secret_cb    = None
        self._spend_sidecar_cb = None
        self._cooldowns: dict[str, float] = {}

    def set_status_cb(self, cb):
        self._status_cb = cb

    def set_token_cb(self, cb):
        """Register callback for inbound 225-byte token messages: cb(token_bytes, from_node)."""
        self._token_cb = cb

    def set_new_secret_cb(self, cb):
        """Register callback for inbound 32-byte new_secret sidecars: cb(secret_bytes, from_node)."""
        self._new_secret_cb = cb

    def set_spend_sidecar_cb(self, cb):
        """Register callback for inbound original spend token sidecars: cb(token_bytes, from_node)."""
        self._spend_sidecar_cb = cb

    def _emit(self, msg: str):
        log.info(f"BLE: {msg}")
        if self._status_cb and self._loop and not self._loop.is_closed():
            self._loop.call_soon_threadsafe(self._status_cb, msg)

    @staticmethod
    def _node_str(num) -> str:
        if num is None or num == 0xFFFFFFFF:
            return ""
        return f"!{int(num):08x}"

    @staticmethod
    def _node_int(node_id: str) -> int:
        if not node_id or node_id.startswith("^"):
            return 0xFFFFFFFF
        return int(node_id.lstrip("!"), 16)

    # ── PyPubSub callbacks (run in meshtastic/bleak thread) ───────────────────

    def _on_receive(self, packet, interface):
        try:
            from_id = self._node_str(packet.get("from"))
            decoded = packet.get("decoded", {})
            portnum = decoded.get("portnum", "")

            # Normalise string portnum → int
            _MAP = {"TEXT_MESSAGE_APP": 1, "NODEINFO_APP": 4, "PRIVATE_APP": 256}
            portnum_int = _MAP.get(portnum, portnum) if isinstance(portnum, str) else int(portnum)

            event = None

            if portnum_int == 1:  # TEXT_MESSAGE_APP
                text = decoded.get("text", "")
                if not text or not from_id:
                    return
                if text.startswith(">:"):
                    caps = re.findall(r'\?(\w+)', text.split("|")[0])
                    event = Event(type=EventType.HEARTBEAT, from_node=from_id,
                                  payload=text, meta={"caps": caps})
                else:
                    event = Event(type=EventType.MESSAGE, ts=time.time(),
                                  from_node=from_id,
                                  to_node=self._node_str(packet.get("to")),
                                  payload=text)

            elif portnum_int == 4:  # NODEINFO_APP
                user   = decoded.get("user", {})
                handle = user.get("longName") or user.get("shortName") or ""
                if from_id:
                    event = Event(type=EventType.HEARTBEAT, from_node=from_id,
                                  payload=handle or from_id,
                                  meta={"handle": handle, "caps": []})

            elif portnum_int == self.PORT_NEW_SECRET:
                payload = decoded.get("payload", b"") or b""
                if isinstance(payload, bytes) and len(payload) == 32 and self._new_secret_cb:
                    if self._loop and not self._loop.is_closed():
                        self._loop.call_soon_threadsafe(self._new_secret_cb, payload, from_id)
                return

            elif portnum_int == self.PORT_TOKEN:
                payload = decoded.get("payload", b"") or b""
                if isinstance(payload, bytes) and len(payload) == 225 and self._token_cb:
                    if self._loop and not self._loop.is_closed():
                        self._loop.call_soon_threadsafe(self._token_cb, payload, from_id)
                return

            elif portnum_int == self.PORT_SPEND_SIDECAR:
                payload = decoded.get("payload", b"") or b""
                if isinstance(payload, bytes) and len(payload) == 225 and self._spend_sidecar_cb:
                    if self._loop and not self._loop.is_closed():
                        self._loop.call_soon_threadsafe(self._spend_sidecar_cb, payload, from_id)
                return

            elif portnum_int == self.PORT_PUBKEY:
                payload = decoded.get("payload", b"") or b""
                if isinstance(payload, bytes) and len(payload) == 32 and from_id:
                    event = Event(type=EventType.HEARTBEAT, from_node=from_id,
                                  meta={"pubkey": payload.hex(), "caps": [], "silent": True})

            if event and self._loop and not self._loop.is_closed():
                self._loop.call_soon_threadsafe(self._eq.put_nowait, event)
        except Exception as e:
            log.debug(f"BLE receive error: {e}")

    def _on_connection(self, interface, topic=None):
        try:
            my_info = getattr(interface, "myInfo", None)
            if my_info and hasattr(my_info, "myNodeNum"):
                self._node_id = self._node_str(my_info.myNodeNum)
                self._cfg.node_id = self._node_id
        except Exception:
            pass
        self._connected = True
        device = self._cfg.ble.device_name or "auto"
        self._emit(f"connected ✓  device={device}  node={self._node_id}")

    def _on_lost(self, interface, topic=None):
        self._connected = False
        self._emit(f"connection lost — will retry in {RETRY_DELAY}s")

    # ── Subscription management ───────────────────────────────────────────────

    def _subscribe(self):
        if self._subscribed:
            return
        try:
            from pubsub import pub
            pub.subscribe(self._on_receive,    "meshtastic.receive")
            pub.subscribe(self._on_connection, "meshtastic.connection.established")
            pub.subscribe(self._on_lost,       "meshtastic.connection.lost")
            self._subscribed = True
        except Exception as e:
            log.warning(f"BLE pubsub subscribe error: {e}")

    def _unsubscribe(self):
        if not self._subscribed:
            return
        try:
            from pubsub import pub
            for fn, topic in [
                (self._on_receive,    "meshtastic.receive"),
                (self._on_connection, "meshtastic.connection.established"),
                (self._on_lost,       "meshtastic.connection.lost"),
            ]:
                try:
                    pub.unsubscribe(fn, topic)
                except Exception:
                    pass
        except Exception:
            pass
        self._subscribed = False

    # ── Main connect loop ─────────────────────────────────────────────────────

    async def connect_loop(self):
        """Background task: connect and auto-reconnect forever."""
        self._loop = asyncio.get_event_loop()

        try:
            from pubsub import pub as _pub  # noqa: F401
        except ImportError:
            self._emit("pypubsub not found — run: pip install 'chiba-deck[ble]'")
            return

        while True:
            try:
                import meshtastic.ble_interface

                device  = self._cfg.ble.device_name or None
                adapter = self._cfg.ble.adapter or None

                self._emit(
                    f"scanning for {'device: ' + device if device else 'any Meshtastic BLE device'}..."
                )

                self._subscribe()

                # BLEInterface constructor is synchronous-ish; run in thread to
                # avoid blocking the asyncio event loop during BLE scan/connect.
                kwargs = {"address": device}
                if adapter:
                    kwargs["adapter"] = adapter

                self._iface = await self._loop.run_in_executor(
                    None,
                    lambda: meshtastic.ble_interface.BLEInterface(**kwargs),
                )

                # Poll until the connection drops
                while self._connected:
                    await asyncio.sleep(2)

            except asyncio.CancelledError:
                break
            except ImportError:
                self._emit("meshtastic not installed — run: pip install 'chiba-deck[ble]'")
                await asyncio.sleep(60)
                continue
            except Exception as e:
                self._emit(f"error: {e}  (retrying in {RETRY_DELAY}s)")

            # Clean up before retry
            self._unsubscribe()
            self._connected = False
            if self._iface:
                try:
                    self._iface.close()
                except Exception:
                    pass
                self._iface = None
            await asyncio.sleep(RETRY_DELAY)

    # ── Send methods (same public API as MQTTTransport) ───────────────────────

    def send_dm(self, to_node: str, text: str) -> bool:
        if not self._connected or not self._iface:
            log.debug(f"send_dm offline: {to_node}: {text[:60]}")
            return False
        try:
            self._iface.sendText(text, destinationId=self._node_int(to_node),
                                 wantAck=False, channelIndex=0)
            return True
        except Exception as e:
            log.debug(f"BLE send_dm error: {e}")
            return False

    def send_broadcast(self, text: str):
        if not self._connected or not self._iface:
            return
        try:
            self._iface.sendText(text, channelIndex=0)
        except Exception as e:
            log.debug(f"BLE broadcast error: {e}")

    def send_chiba_broadcast(self, payload: str):
        if not self._connected or not self._iface:
            return
        try:
            self._iface.sendText(payload, channelIndex=0)
        except Exception as e:
            log.debug(f"BLE chiba_broadcast error: {e}")

    def send_pubkey_announce(self, pubkey_hex: str):
        if not self._connected or not self._iface:
            return
        try:
            self._iface.sendData(bytes.fromhex(pubkey_hex), destinationId="^all",
                                 portNum=self.PORT_PUBKEY, wantAck=False)
        except Exception as e:
            log.debug(f"BLE pubkey_announce error: {e}")

    def send_token(self, to_node: str, token_bytes: bytes) -> bool:
        if not self._connected or not self._iface:
            log.debug(f"send_token offline: {to_node}")
            return False
        try:
            self._iface.sendData(token_bytes, destinationId=self._node_int(to_node),
                                 portNum=self.PORT_TOKEN, wantAck=True)
            return True
        except Exception as e:
            log.debug(f"BLE send_token error: {e}")
            return False

    def send_new_secret(self, to_node: str, new_secret_bytes: bytes) -> bool:
        """Send a 32-byte new_secret sidecar (Port 255) to the token recipient."""
        if not self._connected or not self._iface:
            return False
        try:
            self._iface.sendData(new_secret_bytes, destinationId=self._node_int(to_node),
                                 portNum=self.PORT_NEW_SECRET, wantAck=False)
            return True
        except Exception as e:
            log.debug(f"BLE send_new_secret error: {e}")
            return False

    def send_spend_sidecar(self, to_node: str, spend_token_bytes: bytes) -> bool:
        """Send the original 225-byte spend token as a re-spend sidecar (Port 257)."""
        if not self._connected or not self._iface:
            return False
        try:
            self._iface.sendData(spend_token_bytes, destinationId=self._node_int(to_node),
                                 portNum=self.PORT_SPEND_SIDECAR, wantAck=False)
            return True
        except Exception as e:
            log.debug(f"BLE send_spend_sidecar error: {e}")
            return False

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
        self._unsubscribe()
        if self._iface:
            try:
                self._iface.close()
            except Exception:
                pass
