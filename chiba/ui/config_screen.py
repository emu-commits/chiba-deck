import logging

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Container, Horizontal
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
        width: 72;
        height: auto;
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
        color: #2a4a2a;
        margin-top: 1;
        margin-bottom: 0;
    }

    .row {
        height: 3;
    }

    .lbl {
        width: 12;
        height: 3;
        content-align: left middle;
        color: #888888;
    }

    .inp {
        width: 1fr;
        background: #020902;
        color: #00ff41;
        border: solid #1a3a1a;
    }

    .inp:focus {
        border: solid #00ff41;
    }

    #scan-row {
        height: 3;
        margin-top: 1;
    }

    #scan-btn {
        background: #0a1a0a;
        color: #00ff41;
        border: solid #1a3a1a;
        min-width: 22;
    }

    #scan-btn:hover { background: #1a3a1a; }
    #scan-btn:disabled { color: #2a4a2a; border: solid #1a3a1a; }

    #scan-status {
        color: #555555;
        height: 3;
        content-align: left middle;
        padding-left: 2;
        width: 1fr;
    }

    #topic-list {
        height: 7;
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

    #btn-row {
        height: 3;
        align: center middle;
        margin-top: 1;
    }

    #save-btn {
        background: #1a3a1a;
        color: #00ff41;
        border: solid #00ff41;
        margin-right: 3;
        min-width: 18;
    }

    #cancel-btn {
        background: #0a0f0a;
        color: #555555;
        border: solid #333333;
        min-width: 12;
    }

    #save-btn:hover   { background: #2a5a2a; }
    #cancel-btn:hover { color: #aaaaaa; border: solid #777777; }
    """

    BINDINGS = [Binding("escape", "cancel", show=False)]

    def __init__(self, config: Config, transport=None):
        super().__init__()
        self._cfg = config
        self._transport = transport
        self._scanned_topics: list[str] = []

    def compose(self) -> ComposeResult:
        with Container(id="dialog"):
            yield Label("■  CHIBA CONFIG  ■", id="title")

            yield Label("── Node ─────────────────────────────────────────", classes="section-label")
            with Horizontal(classes="row"):
                yield Label("Node ID", classes="lbl")
                yield Input(self._cfg.node_id, id="node-id", classes="inp")
            with Horizontal(classes="row"):
                yield Label("Handle", classes="lbl")
                yield Input(self._cfg.node_handle, id="node-handle", classes="inp")

            yield Label("── MQTT ─────────────────────────────────────────", classes="section-label")
            with Horizontal(classes="row"):
                yield Label("Broker", classes="lbl")
                yield Input(self._cfg.mqtt.broker, id="broker", classes="inp")
            with Horizontal(classes="row"):
                yield Label("Port", classes="lbl")
                yield Input(str(self._cfg.mqtt.port), id="port", classes="inp")
            with Horizontal(classes="row"):
                yield Label("Topic RX", classes="lbl")
                yield Input(self._cfg.mqtt.topic_rx, id="topic-rx", classes="inp")
            with Horizontal(classes="row"):
                yield Label("Topic TX", classes="lbl")
                yield Input(self._cfg.mqtt.topic_tx, id="topic-tx", classes="inp")

            yield Label("── Topic Discovery ──────────────────────────────", classes="section-label")
            with Horizontal(id="scan-row"):
                yield Button("Scan msh/# (5s)", id="scan-btn")
                yield Static("← scan live broker, click result to use", id="scan-status")
            yield ListView(id="topic-list")

            with Horizontal(id="btn-row"):
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
            status.update("not connected to broker — connect first, then scan")
            btn.disabled = False
            return

        topics = await self._transport.scan_topics(timeout=5.0)
        self._scanned_topics = topics

        lv = self.query_one("#topic-list", ListView)
        lv.clear()
        if topics:
            for t in topics:
                await lv.append(ListItem(Label(t)))
            status.update(f"{len(topics)} topic(s) found — click to select")
        else:
            status.update("no msh/ traffic seen during 5s scan")

        btn.disabled = False

    def on_list_view_selected(self, event: ListView.Selected):
        idx = event.index
        if 0 <= idx < len(self._scanned_topics):
            topic = self._scanned_topics[idx]
            self.query_one("#topic-rx", Input).value = topic
            self.query_one("#topic-tx", Input).value = topic

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

            if self._transport:
                if topics_changed:
                    self._transport.update_topics(new_rx, new_tx)
                else:
                    self._cfg.mqtt.topic_rx = new_rx
                    self._cfg.mqtt.topic_tx = new_tx
                if broker_changed:
                    self._transport.trigger_reconnect()
            else:
                self._cfg.mqtt.topic_rx = new_rx
                self._cfg.mqtt.topic_tx = new_tx

            save_config(self._cfg)
            self.dismiss(True)

        except ValueError:
            self.query_one("#scan-status", Static).update("invalid port — must be a number")
        except Exception as e:
            log.error(f"config save: {e}")
            self.query_one("#scan-status", Static).update(f"error: {e}")

    def action_cancel(self):
        self.dismiss(False)
