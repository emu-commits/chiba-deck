import asyncio
import logging
import time

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
    "send_dm": "dm",
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
        self._msg_seen: set[tuple] = set()

    def set_display_queue(self, display_queue: EventQueue):
        self._display_cb = display_queue.put_nowait

    async def process_event(self, event: Event):
        # Sentinel from transport: all history batches have been queued — pass straight to display
        if event.meta.get("history_end"):
            if self._display_cb:
                self._display_cb(event)
            return

        is_history = event.meta.get("history")

        # Dedup all message events — live and history share the same seen-set.
        # The bridge republishes the retained history topic on every new message,
        # so without this a live message is immediately followed by a history copy.
        if event.type == EventType.MESSAGE and event.from_node and event.payload:
            key = (event.from_node, round(event.ts or 0), event.payload[:80])
            if key in self._msg_seen:
                # Dedup suppresses display, but still process inbound ?commands
                # addressed to us — the bridge may echo our own DMs sub-second,
                # causing a ts collision that would otherwise silence the command.
                if (not is_history
                        and event.to_node == self._node_id
                        and event.payload.strip().startswith("?")):
                    await self._handle_message(event, store=False)
                return
            self._msg_seen.add(key)

        if not is_history:
            self._db.insert_event(event.type.value, event.from_node, event.payload)

        if event.type == EventType.HEARTBEAT:
            await self._handle_heartbeat(event)
        elif event.type == EventType.MESSAGE:
            await self._handle_message(event, store=not is_history)

        if event.meta.get("silent"):
            return

        # Attach stored handles for display; prefer what's already in meta (Pi bridge name field)
        if event.from_node and "from_handle" not in event.meta:
            handle = self._db.get_node_handle(event.from_node)
            if handle:
                event.meta["from_handle"] = handle
        if event.to_node and "to_handle" not in event.meta:
            handle = self._db.get_node_handle(event.to_node)
            if handle:
                event.meta["to_handle"] = handle

        if self._display_cb:
            self._display_cb(event)

    async def _handle_heartbeat(self, event: Event):
        caps = event.meta.get("caps", [])
        handle = event.meta.get("handle", "")
        self._registry.upsert_remote(event.from_node, caps)
        self._db.upsert_node(event.from_node, handle=handle, caps=caps)

    async def _handle_message(self, event: Event, store: bool = True):
        text = event.payload.strip()
        from_node = event.from_node
        to_node = event.to_node

        if store:
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
        if cmd == "dm":
            words = query.split()
            if not words:
                return "usage: ?dm <handle> <message>"

            # Strip trailing punctuation from each word for handle matching only;
            # original words are preserved for the message portion.
            norm = [w.rstrip(":.!?,;") for w in words]

            # Greedy exact match: try longest handle candidate first, shorten until found
            node_id = ""
            recipient = ""
            message = ""
            for n in range(len(norm), 0, -1):
                candidate = " ".join(norm[:n])
                nid = self._db.find_node_by_handle_exact(candidate)
                if nid:
                    node_id = nid
                    recipient = candidate
                    message = " ".join(words[n:]).strip()
                    break

            # Prefix fallback on first word only (e.g. "cliff" → "Cliff in JC")
            if not node_id:
                recipient = norm[0]
                node_id = self._db.find_node_by_handle(recipient)
                message = " ".join(words[1:]).strip()

            if not message:
                return "usage: ?dm <handle> <message>"

            if not node_id:
                if recipient.startswith("!"):
                    node_id = recipient
                else:
                    return f"node '{recipient}' not found — try ?nodes"

            if not self._transport or not self._transport.connected:
                return "not connected — can't send"
            ok = self._transport.send_dm(node_id, message)
            if not ok:
                return "not connected — can't send"
            self._db.insert_message(self._node_id, node_id, message, "out")
            handle = self._db.get_node_handle(self._node_id) or self._cfg.node_handle
            to_handle = self._db.get_node_handle(node_id) or recipient
            sent_event = Event(
                type=EventType.MESSAGE,
                from_node=self._node_id,
                to_node=node_id,
                payload=message,
                meta={"from_handle": handle, "to_handle": to_handle},
            )
            key = (self._node_id, round(sent_event.ts or 0), message[:80])
            self._msg_seen.add(key)
            if self._display_cb:
                self._display_cb(sent_event)
            return ""

        if cmd in ("say", "chat", "broadcast", "s"):
            if not query:
                return "usage: ?say <message>"
            if not self._transport or not self._transport.connected:
                return "not connected — can't send"
            self._transport.send_broadcast(query)
            self._db.insert_message(self._node_id, "", query, "out")
            handle = self._db.get_node_handle(self._node_id) or self._cfg.node_handle
            sent_event = Event(
                type=EventType.MESSAGE,
                from_node=self._node_id,
                payload=query,
                meta={"from_handle": handle} if handle else {},
            )
            # Register in dedup so bridge echoes don't re-display the message
            key = (self._node_id, round(sent_event.ts or 0), query[:80])
            self._msg_seen.add(key)
            if self._display_cb:
                self._display_cb(sent_event)
            return ""

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
        if intent == "send_dm" and entities.target_node:
            query = f"{entities.target_node} {entities.query}".strip()
        else:
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
