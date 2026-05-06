import asyncio
import logging

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Container, Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, Input, Label, ListItem, ListView, Static

from ..config import Config, save_config

log = logging.getLogger(__name__)


class ConfigScreen(ModalScreen[bool]):
    DEFAULT_CSS = """
    ConfigScreen {
        align: center middle;
    }

    #dialog {
        width: 70;
        height: auto;
        max-height: 40;
        background: #0a0f0a;
        border: double #00ff41;
        padding: 1 2;
    }

    #title {
        text-align: center;
        color: #00ff41;
        text-style: bold;
        margin-bottom: 1;
    }

    .section-label {
        color: #444444;
        margin-top: 1;
    }

    .field-row {
        height: 3;
        margin-bottom: 0;
    }

    .field-label {
        width: 14;
        height: 3;
        content-align: left middle;
        color: #888888;
    }

    .field-input {
        width: 1fr;
        background: #020902;
        color: #00ff41;
        border: solid #1a3a1a;
    }

    .field-input:focus {
        border: solid #00ff41;
    }

    #scan-row {
        height: 3;
        margin-top: 1;
        align: left middle;
    }

    #scan-btn {
        background: #0a1a0a;
        color: #00ff41;
        border: solid #1a3a1a;
        min-width: 20;
    }

    #scan-btn:hover {
        background: #1a3a1a;
    }

    #scan-status {
        color: #666666;
        margin-left: 2;
        height: 3;
        content-align: left middle;
    }

    #topic-list {
        height: 6;
        border: solid #1a3a1a;
        background: #020902;
        margin-top: 0;
    }

    #topic-list > ListItem {
        color: #00cc33;
        padding: 0 1;
    }

    #topic-list > ListItem.--highlight {
        background: #1a3a1a;
        color: #00ff41;
    }

    #button-row {
        height: 3;
        align: center middle;
        margin-top: 1;
    }

    #save-btn {
        background: #1a3a1a;
        color: #00ff41;
        border: solid #00ff41;
        margin-right: 2;
        min-width: 16;
    }

    #cancel-btn {
        background: #0a0f0a;
        color: #666666;
        border: solid #444444;
        min-width: 12;
    }

    #save-btn:hover { background: #2a5a2a; }
    #cancel-btn:hover { color: #aaaaaa; border: solid #888888; }
    """

    BINDINGS = [Binding("escape", "cancel", show=False)]

    def __init__(self, config: Config, transport=None):
        super().__init__()
        self._cfg = config
        self._transport = transport

    def compose(self) -> ComposeResult:
        with Container(id="dialog"):
            yield Label("■ CHIBA CONFIG ■", id="title")

            yield Label("── Node ──────────────────────────", classes="section-label")
            with Horizontal(classes="field-row"):
                yield Label("Node ID", classes="field-label")
                yield Input(self._cfg.node_id, id="node-id", classes="field-input")
            with Horizontal(classes="field-row"):
                yield Label("Handle", classes="field-label")
                yield Input(self._cfg.node_handle, id="node-handle", classes="field-input")

            yield Label("── MQTT ──────────────────────────", classes="section-label")
            with Horizontal(classes="field-row"):
                yield Label("Broker", classes="field-label")
                yield Input(self._cfg.mqtt.broker, id="broker", classes="field-input")
            with Horizontal(classes="field-row"):
                yield Label("Port", classes="field-label")
                yield Input(str(self._cfg.mqtt.port), id="port", classes="field-input")
            with Horizontal(classes="field-row"):
                yield Label("Topic RX", classes="field-label")
                yield Input(self._cfg.mqtt.topic_rx, id="topic-rx", classes="field-input")
            with Horizontal(classes="field-row"):
                yield Label("Topic TX", classes="field-label")
                yield Input(self._cfg.mqtt.topic_tx, id="topic-tx", classes="field-input")

            yield Label("── Topic Discovery ───────────────", classes="section-label")
            with Horizontal(id="scan-row"):
                yield Button("Scan msh/# (5s)", id="scan-btn")
                yield Static("← click to scan live broker topics", id="scan-status")
            yield ListView(id="topic-list")

            with Horizontal(id="button-row"):
                yield Button("Save & Apply", id="save-btn")
                yield Button("Cancel", id="cancel-btn")

    def on_button_pressed(self, event: Button.Pressed):
        if event.button.id == "scan-btn":
            self.run_worker(self._do_scan(), exclusive=False)
        elif event.button.id == "save-btn":
            self._save()
        elif event.button.id == "cancel-btn":
            self.dismiss(False)

    async def _do_scan(self):
        status = self.query_one("#scan-status", Static)
        btn = self.query_one("#scan-btn", Button)
        btn.disabled = True
        status.update("scanning… (5s)")

        if not self._transport or not self._transport.connected:
            status.update("not connected — cannot scan")
            btn.disabled = False
            return

        topics = await self._transport.scan_topics(timeout=5.0)

        lv = self.query_one("#topic-list", ListView)
        await lv.clear()
        if topics:
            for t in topics:
                await lv.append(ListItem(Label(t)))
            status.update(f"{len(topics)} topics found — click one to use it")
        else:
            status.update("no msh/ topics seen (no traffic during scan)")

        btn.disabled = False

    def on_list_view_selected(self, event: ListView.Selected):
        topic = event.item.query_one(Label).renderable
        topic_str = str(topic).strip()
        self.query_one("#topic-rx", Input).value = topic_str
        # Derive TX topic: same path but without trailing node suffix
        # msh/2/e/LongFast/!abc → msh/2/e/LongFast/!abc (same is fine for unicast)
        self.query_one("#topic-tx", Input).value = topic_str

    def _save(self):
        try:
            self._cfg.node_id = self.query_one("#node-id", Input).value.strip()
            self._cfg.node_handle = self.query_one("#node-handle", Input).value.strip()

            new_broker = self.query_one("#broker", Input).value.strip()
            new_port = int(self.query_one("#port", Input).value.strip())
            new_rx = self.query_one("#topic-rx", Input).value.strip()
            new_tx = self.query_one("#topic-tx", Input).value.strip()

            broker_changed = (new_broker != self._cfg.mqtt.broker or
                              new_port != self._cfg.mqtt.port)
            topics_changed = (new_rx != self._cfg.mqtt.topic_rx or
                              new_tx != self._cfg.mqtt.topic_tx)

            self._cfg.mqtt.broker = new_broker
            self._cfg.mqtt.port = new_port

            if topics_changed and self._transport:
                self._transport.update_topics(new_rx, new_tx)
            else:
                self._cfg.mqtt.topic_rx = new_rx
                self._cfg.mqtt.topic_tx = new_tx

            if broker_changed and self._transport:
                self._transport.trigger_reconnect()

            save_config(self._cfg)
            self.dismiss(True)
        except Exception as e:
            log.error(f"config save error: {e}")
            self.query_one("#scan-status", Static).update(f"error: {e}")

    def action_cancel(self):
        self.dismiss(False)
