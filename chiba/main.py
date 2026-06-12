import argparse
import asyncio
import logging
import sys
from pathlib import Path

from .config import load_config
from .db import Database
from .events import Event, EventQueue, EventType
from .heartbeat import HeartbeatService
from .nlp.classifier import IntentClassifier
from .payments import receiver as token_receiver
from .payments import wallet as wallet_module
from .payments.deposit_plugin import DepositPlugin
from .payments.redeem_plugin import RedeemPlugin
from .payments.wallet import BalancePlugin, MintPlugin, PubkeyPlugin, WalletInfoPlugin, WalletPlugin, init_payments
from .plugins.exec_plugin import ExecPlugin
from .plugins.help_plugin import HelpPlugin
from .plugins.history_plugin import HistoryPlugin
from .plugins.nodes_plugin import NodesPlugin
from .plugins.status_plugin import StatusPlugin
from .registry import ServiceRegistry
from .router import Router
from .transport import BLETransport, MQTTTransport
from .ui.app import ChibaApp

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    handlers=[logging.FileHandler("chiba.log")],
)
log = logging.getLogger(__name__)


async def _main(config_path: str = "config.yaml"):
    config = load_config(config_path)

    db = Database(config.db_path)
    db.connect()

    transport_eq = EventQueue()
    display_eq = EventQueue()

    if config.ble.enabled:
        transport = BLETransport(config, config.node_id, transport_eq)
        transport_label = "BLE"
    else:
        transport = MQTTTransport(config, config.node_id, transport_eq)
        transport_label = "MQTT"
    registry = ServiceRegistry(db)

    # NLP
    classifier = IntentClassifier(config.nlp.embeddings_path)
    if Path(config.nlp.embeddings_path).exists():
        classifier.load()
    else:
        log.warning(f"embeddings not found at {config.nlp.embeddings_path}")
        log.warning("run: python scripts/build_embeddings.py")

    # Payments
    payment_wallet = init_payments(config)

    # Built-in plugins
    wallet = WalletPlugin()
    balance = BalancePlugin()
    mint = MintPlugin()
    wallet_info = WalletInfoPlugin()
    pubkey = PubkeyPlugin()
    redeem = RedeemPlugin()
    deposit = DepositPlugin()

    wallet.set_db(db)
    wallet.set_transport(transport)

    nodes_plugin = NodesPlugin()
    nodes_plugin.set_db(db)

    help_plugin = HelpPlugin()
    help_plugin.set_registry(registry)

    status_plugin = StatusPlugin()
    status_plugin.set_db(db)
    status_plugin.set_wallet(wallet)

    history_plugin = HistoryPlugin()
    history_plugin.set_db(db)

    for plugin in [nodes_plugin, help_plugin, status_plugin, history_plugin,
                   wallet, balance, mint, wallet_info, pubkey, redeem, deposit]:
        registry.register_local(plugin)

    # Load exec plugins from config — each is a cmd → shell command mapping.
    # The plugin logic lives entirely outside this app; chiba just dispatches
    # the query as a subprocess arg and returns stdout over the mesh DM.
    for p_cfg in config.plugins:
        try:
            plugin = ExecPlugin.from_config(
                p_cfg.cmd, p_cfg.description, p_cfg.exec_cmd,
                p_cfg.timeout, p_cfg.max_chars,
            )
            registry.register_local(plugin)
            log.info(f"exec plugin registered: ?{p_cfg.cmd} → {p_cfg.exec_cmd!r}")
        except ValueError as e:
            log.warning(f"skipped exec plugin: {e}")

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
        pubkey=payment_wallet.get_pubkey() if payment_wallet else "",
    )

    def _wire_payments(pw):
        """Connect the token receiver, transport callbacks and heartbeat pubkey.
        Runs at startup if payments are enabled, and again via the post-init
        hook when payments are enabled live through ?wallet."""
        token_receiver.init(pw, db, display_eq, config)
        transport.set_token_cb(token_receiver.handle_token_message)
        transport.set_new_secret_cb(token_receiver.handle_new_secret_message)
        transport.set_spend_sidecar_cb(token_receiver.handle_spend_sidecar_message)
        pk = pw.get_pubkey() or ""
        heartbeat.set_pubkey(pk)
        if pk and transport.connected:
            transport.send_pubkey_announce(pk)

    if payment_wallet is not None:
        _wire_payments(payment_wallet)
    wallet_module.set_post_init_hook(_wire_payments)

    def _transport_status(msg: str):
        display_eq.put_nowait(Event(type=EventType.SYSTEM, payload=f"{transport_label}: {msg}"))

    transport.set_status_cb(_transport_status)

    # Startup banner
    display_eq.put_nowait(Event(
        type=EventType.SYSTEM,
        payload=f"chiba-deck v0.1 | node {config.node_id} | ?help for commands",
    ))

    # Replay all recent messages from SQLite (both sent and received).
    # MQTT retained history also provides inbound messages, but SQLite is authoritative
    # and available offline. Dedup in the router blocks any MQTT overlap.
    for row in db.get_recent_messages(max_age_s=86400):
        transport_eq.put_nowait(Event(
            type=EventType.MESSAGE,
            ts=row["ts"],
            from_node=row["from_id"] or "",
            to_node=row["to_id"] or "",
            payload=row["text"],
            meta={"history": True},
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
        asyncio.create_task(transport.connect_loop(), name="transport-connect"),
    ]

    try:
        await app.run_async()
    except Exception as e:
        log.error(f"app crash: {e}", exc_info=True)
        raise
    finally:
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        transport.disconnect()
        log.info("chiba-deck stopped")


def run():
    parser = argparse.ArgumentParser(description="chiba-deck")
    parser.add_argument("--config", default="config.yaml", help="path to config.yaml")
    args = parser.parse_args()
    try:
        asyncio.run(_main(args.config))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    run()
