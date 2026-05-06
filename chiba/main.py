import asyncio
import logging
import sys
from pathlib import Path

from .config import load_config
from .db import Database
from .events import Event, EventQueue, EventType
from .heartbeat import HeartbeatService
from .nlp.classifier import IntentClassifier
from .payments.wallet import BalancePlugin, WalletPlugin
from .plugins.help_plugin import HelpPlugin
from .plugins.loader import load_plugins
from .plugins.nodes_plugin import NodesPlugin
from .plugins.status_plugin import StatusPlugin
from .registry import ServiceRegistry
from .router import Router
from .transport import MQTTTransport
from .ui.app import ChibaApp

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    handlers=[logging.FileHandler("chiba.log")],
)
log = logging.getLogger(__name__)


async def _main():
    config = load_config()

    db = Database(config.db_path)
    db.connect()

    transport_eq = EventQueue()
    display_eq = EventQueue()

    transport = MQTTTransport(config, config.node_id, transport_eq)
    registry = ServiceRegistry(db)

    # NLP
    classifier = IntentClassifier(config.nlp.embeddings_path)
    if Path(config.nlp.embeddings_path).exists():
        classifier.load()
    else:
        log.warning(f"embeddings not found at {config.nlp.embeddings_path}")
        log.warning("run: python scripts/build_embeddings.py")

    # Built-in plugins
    wallet = WalletPlugin()
    balance = BalancePlugin()

    nodes_plugin = NodesPlugin()
    nodes_plugin.set_db(db)

    help_plugin = HelpPlugin()
    help_plugin.set_registry(registry)

    status_plugin = StatusPlugin()
    status_plugin.set_db(db)
    status_plugin.set_wallet(wallet)

    for plugin in [nodes_plugin, help_plugin, status_plugin, wallet, balance]:
        registry.register_local(plugin)

    # Auto-load extra plugins from plugins/
    for plugin in load_plugins():
        registry.register_local(plugin)

    registry.restore_from_db()

    router = Router(
        config=config,
        registry=registry,
        transport=transport,
        db=db,
        classifier=classifier,
        event_queue=transport_eq,
        node_id=config.node_id,
    )
    router.set_display_queue(display_eq)

    heartbeat = HeartbeatService(
        config=config,
        registry=registry,
        transport=transport,
        node_handle=config.node_handle,
    )

    # Route MQTT status messages into the stream panel as SYS events
    def _mqtt_status(msg: str):
        display_eq.put_nowait(Event(type=EventType.SYSTEM, payload=f"MQTT: {msg}"))

    transport.set_status_cb(_mqtt_status)

    # Startup banner
    display_eq.put_nowait(Event(
        type=EventType.SYSTEM,
        payload=f"chiba-deck v0.1 | node {config.node_id} | ?help for commands",
    ))

    async def event_processor():
        while True:
            try:
                event = await transport_eq.get()
                await router.process_event(event)
            except asyncio.CancelledError:
                break
            except Exception as e:
                log.error(f"event processor error: {e}", exc_info=True)

    app = ChibaApp(
        router=router,
        display_queue=display_eq,
        db=db,
        config=config,
        transport=transport,
    )

    tasks = [
        asyncio.create_task(event_processor(), name="event-processor"),
        asyncio.create_task(heartbeat.run(), name="heartbeat"),
        asyncio.create_task(transport.connect_loop(), name="mqtt-connect"),
    ]

    try:
        await app.run_async()
    finally:
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        transport.disconnect()
        log.info("chiba-deck stopped")


def run():
    try:
        asyncio.run(_main())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    run()
