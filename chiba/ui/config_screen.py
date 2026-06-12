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
        padding: 0 2;
    }

    #title {
        text-align: center;
        color: #00ff41;
        text-style: bold;
        margin-top: 1;
        margin-bottom: 0;
    }

    .section-label {
        color: #2a4a2a;
        margin-top: 1;
        margin-bottom: 0;
    }

    #node-info {
        color: #2a6a2a;
        height: 1;
        padding: 0 1;
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
        margin-top: 0;
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
        height: 4;
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
        margin-bottom: 1;
    }

    #save-btn {
        background: #1a3a1a;
        color: #00ff41;
        border: solid #00ff41;
        margin-right: 3;
        min-width: 22;
    }

    #cancel-btn {
        background: #0a0f0a;
        color: #555555;
        border: solid #333333;
        min-width: 16;
    }

    #save-btn:hover   { background: #2a5a2a; }
    #cancel-btn:hover { color: #aaaaaa; border: solid #777777; }
    """

    BINDINGS = [
        Binding("escape", "cancel", "cancel", show=True),
        Binding("ctrl+s", "save", "save", show=True),
        Binding("up", "nav_up", show=False, priority=True),
        Binding("down", "nav_down", show=False, priority=True),
    ]

    def __init__(self, config: Config, transport=None, node_name: str = ""):
        super().__init__()
        self._cfg = config
        self._transport = transport
        self._scanned_topics: list[str] = []
        self._node_name = node_name

    def compose(self) -> ComposeResult:
        node_id = self._cfg.node_id or "connecting…"
        name = self._node_name or ""
        node_line = f"{node_id}  {name}" if name else node_id

        with Container(id="dialog"):
            yield Label("■  CHIBA CONFIG  ■", id="title")

            yield Label("── Node (auto) ──────────────────────────────────", classes="section-label")
            yield Static(node_line, id="node-info")

            yield Label("── MQTT ─────────────────────────────────────────", classes="section-label")
            with Horizontal(classes="row"):
                yield Label("Broker", classes="lbl")
                yield Input(self._cfg.mqtt.broker, placeholder="192.168.1.x", id="broker", classes="inp")
            with Horizontal(classes="row"):
                yield Label("Port", classes="lbl")
                yield Input(str(self._cfg.mqtt.port), placeholder="1883", id="port", classes="inp")
            with Horizontal(classes="row"):
                yield Label("Topic RX", classes="lbl")
                yield Input(self._cfg.mqtt.topic_rx, placeholder="msh/region/rx", id="topic-rx", classes="inp")
            with Horizontal(classes="row"):
                yield Label("Topic TX", classes="lbl")
                yield Input(self._cfg.mqtt.topic_tx, placeholder="msh/region/tx", id="topic-tx", classes="inp")

            yield Label("── Topic Discovery ──────────────────────────────", classes="section-label")
            with Horizontal(id="scan-row"):
                yield Button("Scan msh/# (5s)", id="scan-btn")
                yield Static("← click result to load into RX + TX", id="scan-status")
            yield ListView(id="topic-list")

            with Horizontal(id="btn-row"):
                yield Button("Save & Apply  [^S]", id="save-btn")
                yield Button("Cancel  [esc]", id="cancel-btn")

    def action_nav_down(self):
        if isinstance(self.focused, ListView):
            lv = self.focused
            count = len(lv._nodes)
            if count == 0:
                self.focus_next()
            elif lv.index is not None and lv.index >= count - 1:
                self.focus_next()
            else:
                lv.action_cursor_down()
        else:
            self.focus_next()

    def action_nav_up(self):
        if isinstance(self.focused, ListView):
            lv = self.focused
            if not lv._nodes or lv.index == 0:
                self.focus_previous()
            else:
                lv.action_cursor_up()
        else:
            self.focus_previous()

    def on_button_pressed(self, event: Button.Pressed):
        if event.button.id == "scan-btn":
            self.run_worker(self._do_scan(), exclusive=False)
        elif event.button.id == "save-btn":
            self._save()
        elif event.button.id == "cancel-btn":
            self.dismiss(False)

    # Suffixes of bridge housekeeping topics — used to DERIVE the rx/tx prefix
    _META_SUFFIXES = ("history", "nodes", "channels", "gateway")

    async def _do_scan(self):
        status = self.query_one("#scan-status", Static)
        btn = self.query_one("#scan-btn", Button)
        btn.disabled = True
        status.update("scanning… (5s)")

        if not self._transport or not self._transport.connected:
            status.update("not connected — connect first, then scan")
            btn.disabled = False
            return

        if not hasattr(self._transport, "scan_topics"):
            status.update("topic scan only available over MQTT")
            btn.disabled = False
            return

        raw = await self._transport.scan_topics(timeout=5.0)
        topic_set = set(raw)
        seen_bases: set[str] = set()
        display: list[tuple[str, str]] = []  # (rx_topic, label)

        # Pass 1: direct live rx/tx traffic seen during scan window
        for t in raw:
            if t.endswith("/rx"):
                base = t[:-2]
                tx = base + "tx"
                seen_bases.add(base)
                label = f"{t}  →  {tx}" if tx in topic_set else t
                display.append((t, label))
            elif t.endswith("/tx"):
                base = t[:-2]
                if base not in seen_bases:
                    seen_bases.add(base)
                    rx = base + "rx"
                    display.append((rx, f"{rx}  →  {t}"))

        # Pass 2: derive prefix from retained meta topics (available immediately)
        for t in raw:
            last = t.rsplit("/", 1)[-1]
            if last in self._META_SUFFIXES:
                base = t[: -(len(last))]   # e.g. "msh/norns/"
                if base not in seen_bases:
                    seen_bases.add(base)
                    rx = base + "rx"
                    tx = base + "tx"
                    display.append((rx, f"{rx}  →  {tx}"))

        self._scanned_topics = [t for t, _ in display]

        lv = self.query_one("#topic-list", ListView)
        lv.clear()
        if display:
            for _, label in display:
                await lv.append(ListItem(Label(label)))
            status.update(f"{len(display)} found — click to load")
        else:
            status.update("no message topics seen — try again")

        btn.disabled = False

    def on_list_view_selected(self, event: ListView.Selected):
        idx = event.index
        if 0 <= idx < len(self._scanned_topics):
            topic = self._scanned_topics[idx]
            # Smart pair: /rx → derive /tx; /tx → derive /rx; otherwise set both
            if topic.endswith("/rx"):
                rx, tx = topic, topic[:-2] + "tx"
            elif topic.endswith("/tx"):
                rx, tx = topic[:-2] + "rx", topic
            else:
                rx, tx = topic, topic
            self.query_one("#topic-rx", Input).value = rx
            self.query_one("#topic-tx", Input).value = tx

    def action_save(self):
        self._save()

    def _save(self):
        try:
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
