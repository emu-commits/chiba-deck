import asyncio
import logging

from .db import Database
from .events import Event, EventQueue, EventType
from .nlp.classifier import IntentClassifier
from .nlp.extractor import extract
from .registry import ServiceRegistry
from .transport import MQTTTransport

log = logging.getLogger(__name__)

_INTENT_TO_CMD: dict[str, str] = {
    "check_balance": "balance",
    "send_payment": "pay",
    "query_wiki": "wiki",
    "list_market": "market",
    "buy_item": "market",
    "post_listing": "market",
    "list_nodes": "nodes",
    "ping_node": "nodes",
    "check_history": "history",
    "get_help": "help",
    "show_status": "status",
    "open_config": "__config__",
    "send_chat": "say",
}


class Router:
    def __init__(
        self,
        config,
        registry: ServiceRegistry,
        transport: MQTTTransport,
        db: Database,
        classifier: IntentClassifier,
        event_queue: EventQueue,
        node_id: str,
    ):
        self._cfg = config
        self._registry = registry
        self._transport = transport
        self._db = db
        self._classifier = classifier
        self._eq = event_queue
        self._node_id = node_id
        self._pending: dict[str, asyncio.Future] = {}
        self._display_cb = None

    def set_display_queue(self, display_queue: EventQueue):
        self._display_cb = display_queue.put_nowait

    async def process_event(self, event: Event):
        self._db.insert_event(event.type.value, event.from_node, event.payload)

        if event.type == EventType.HEARTBEAT:
            await self._handle_heartbeat(event)
        elif event.type == EventType.MESSAGE:
            await self._handle_message(event)

        if event.meta.get("silent"):
            return

        # Attach stored handle for display; prefer what's already in meta (Pi bridge name field)
        if event.from_node and "from_handle" not in event.meta:
            handle = self._db.get_node_handle(event.from_node)
            if handle:
                event.meta["from_handle"] = handle

        if self._display_cb:
            self._display_cb(event)

    async def _handle_heartbeat(self, event: Event):
        caps = event.meta.get("caps", [])
        handle = event.meta.get("handle", "")
        self._registry.upsert_remote(event.from_node, caps)
        self._db.upsert_node(event.from_node, handle=handle, caps=caps)

    async def _handle_message(self, event: Event):
        text = event.payload.strip()
        from_node = event.from_node
        to_node = event.to_node

        self._db.insert_message(from_node, to_node, text, "in")
        if from_node:
            self._db.upsert_node(from_node, handle=event.meta.get("from_handle", ""))

        is_for_us = (to_node == self._node_id) or not to_node

        if text.startswith("?") and is_for_us:
            await self._handle_inbound_cmd(from_node, text)
            return

        # Route reply to any pending outbound proxy
        if from_node in self._pending and not self._pending[from_node].done():
            self._pending[from_node].set_result(text)

    async def _handle_inbound_cmd(self, from_node: str, text: str):
        parts = text.lstrip("?").split(None, 1)
        cmd = parts[0].lower()
        query = parts[1] if len(parts) > 1 else ""

        plugin = self._registry.get_local(cmd)
        if not plugin:
            return

        if not self._transport.can_reply_to(from_node, self._cfg.mesh.cooldown_seconds):
            log.debug(f"rate-limited reply to {from_node}")
            return

        try:
            reply = plugin.handle(query, from_node=from_node)
            self._transport.send_dm(from_node, reply[:200])
        except Exception as e:
            log.error(f"plugin {cmd} error: {e}")

    async def handle_user_input(self, text: str, force: bool = False) -> str:
        text = text.strip()
        if not text:
            return ""

        if text.startswith("?"):
            parts = text.lstrip("?").split(None, 1)
            cmd = parts[0].lower()
            query = parts[1] if len(parts) > 1 else ""
            return await self._dispatch_cmd(cmd, query, text)

        intent, confidence = self._classifier.classify(text)
        entities = extract(intent, text)

        if intent == "open_config":
            return "__config__"

        threshold = getattr(self._classifier, "threshold", self._cfg.nlp.confidence_threshold)
        if not force and confidence < threshold:
            cmd = _INTENT_TO_CMD.get(intent, intent)
            return (
                f"[?] Understood as: {intent} → ?{cmd} "
                f"(confidence {confidence:.2f}) — correct? [y/n]"
            )

        return await self._dispatch_intent(intent, entities, text)

    async def _dispatch_cmd(self, cmd: str, query: str, raw: str) -> str:
        if cmd in ("say", "chat", "broadcast", "s"):
            if not query:
                return "usage: ?say <message>"
            if not self._transport or not self._transport.connected:
                return "not connected — can't send"
            self._transport.send_broadcast(query)
            self._db.insert_message(self._node_id, "", query, "out")
            return f"→ mesh: {query}"

        plugin = self._registry.get_local(cmd)
        if plugin:
            try:
                return plugin.handle(query)
            except Exception as e:
                return f"error: {e}"

        node_id = self._registry.get_remote(cmd)
        if node_id:
            return await self._proxy(node_id, raw)

        return f"?{cmd} not found — try ?help"

    async def _dispatch_intent(self, intent: str, entities, raw: str) -> str:
        cmd = _INTENT_TO_CMD.get(intent, intent)
        query = entities.query or entities.item or raw
        return await self._dispatch_cmd(cmd, query, f"?{cmd} {query}".strip())

    async def _proxy(self, node_id: str, text: str) -> str:
        timeout = self._cfg.mesh.reply_timeout_seconds
        loop = asyncio.get_event_loop()
        fut: asyncio.Future = loop.create_future()
        self._pending[node_id] = fut
        self._transport.send_dm(node_id, text)
        try:
            return await asyncio.wait_for(asyncio.shield(fut), timeout=timeout)
        except asyncio.TimeoutError:
            return f"no reply from {node_id} ({timeout}s timeout)"
        finally:
            self._pending.pop(node_id, None)
