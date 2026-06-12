import asyncio
import logging
import subprocess

from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.widgets import Input

from ..events import EventQueue
from ..payments.wallet import get_wallet
from .config_screen import ConfigScreen
from .status_bar import StatusBar
from .stream_panel import StreamPanel

log = logging.getLogger(__name__)


class ChibaApp(App):
    CSS = """
    Screen {
        background: #020902;
        layout: vertical;
    }

    StatusBar {
        height: 1;
        dock: top;
    }

    StreamPanel {
        height: 1fr;
        border: solid #1a3a1a;
        margin: 0 0 0 0;
    }

    Input {
        dock: bottom;
        height: 3;
        border: solid #1a3a1a;
        background: #020902;
        color: #00ff41;
        padding: 0 1;
    }

    Input:focus {
        border: solid #00ff41;
    }

    Input > .input--placeholder {
        color: #2a5a2a;
    }
    """

    BINDINGS = [
        Binding("ctrl+c", "quit", "quit", show=False),
        Binding("ctrl+q", "quit", "quit", show=True),
        Binding("ctrl+o", "config", "config", show=True),
        Binding("ctrl+y", "copy_log", "copy log", show=True),
        Binding("escape", "clear_input", "clear", show=False),
    ]

    TITLE = "CHIBA DECK"

    def __init__(self, router, display_queue: EventQueue, db, config, transport=None, **kwargs):
        super().__init__(**kwargs)
        self._router = router
        self._display_queue = display_queue
        self._db = db
        self._cfg = config
        self._transport = transport
        self._pending_confirmation: str | None = None

    def compose(self) -> ComposeResult:
        yield StatusBar(id="statusbar")
        yield StreamPanel(id="stream", highlight=True, markup=False, wrap=True)
        yield Input(placeholder="> type a command or plain english", id="input")

    def on_mount(self):
        self.query_one("#input", Input).focus()
        self.set_interval(3.0, self._refresh_status)
        self.run_worker(self._event_drain(), exclusive=False, name="event-drain")

    async def _event_drain(self):
        stream = self.query_one("#stream", StreamPanel)
        history_buf: list = []
        history_done = False

        async def flush_history():
            nonlocal history_done
            history_done = True
            history_buf.sort(key=lambda e: e.ts or 0)
            for e in history_buf:
                stream.render_event(e)
            history_buf.clear()

        while True:
            try:
                if not history_done:
                    try:
                        event = await asyncio.wait_for(
                            self._display_queue.get(), timeout=4.0
                        )
                    except asyncio.TimeoutError:
                        await flush_history()
                        continue

                    if event.meta.get("history_end"):
                        await flush_history()
                    elif event.meta.get("history"):
                        history_buf.append(event)
                    else:
                        stream.render_event(event)
                        await asyncio.sleep(0.3)
                else:
                    event = await self._display_queue.get()
                    if event.meta.get("history_end"):
                        continue
                    stream.render_event(event)
                    if not event.meta.get("history"):
                        await asyncio.sleep(0.3)
            except asyncio.CancelledError:
                break
            except Exception as e:
                log.debug(f"drain error: {e}")

    def _refresh_status(self):
        try:
            bar = self.query_one("#statusbar", StatusBar)
            bar.node_count = self._db.get_node_count()
            bar.unread = self._db.get_unread_count()
            bar.connected = self._transport.connected if self._transport else False
            w = get_wallet()
            if w is not None:
                total = sum(d * c for d, c in w.balance().items())
                bar.balance = f"${total:.2f}"
            else:
                bar.balance = "off"
        except Exception:
            pass

    _QUIT_CMDS = {"quit", "exit", "q", "?quit", "/quit", ":q"}

    async def on_input_submitted(self, event: Input.Submitted):
        text = event.value.strip()
        if not text:
            return

        input_w = self.query_one("#input", Input)
        stream = self.query_one("#stream", StreamPanel)
        input_w.value = ""

        if text.lower() in self._QUIT_CMDS:
            self.exit()
            return

        stream.write_input_echo(text)

        if self._pending_confirmation is not None:
            orig = self._pending_confirmation
            self._pending_confirmation = None
            if text.lower() in ("y", "yes"):
                self.run_worker(
                    self._process(orig, stream, force=True),
                    exclusive=False,
                )
            else:
                stream.write_system("cancelled")
            return

        self.run_worker(self._process(text, stream), exclusive=False)

    async def _process(self, text: str, stream: StreamPanel, force: bool = False):
        try:
            reply = await self._router.handle_user_input(text, force=force)
            if not reply:
                return
            if reply == "__config__":
                await self._open_config()
                return
            if reply.startswith("[?]"):
                self._pending_confirmation = text
            stream.write_reply(reply)
        except Exception as e:
            log.error(f"process error: {e}", exc_info=True)
            stream.write_system(f"error: {e}")

    def action_clear_input(self):
        self.query_one("#input", Input).value = ""
        self._pending_confirmation = None

    def action_config(self):
        self.run_worker(self._open_config(), exclusive=False)

    async def _open_config(self):
        node_name = self._db.get_node_handle(self._cfg.node_id) if self._db else ""
        changed = await self.push_screen_wait(
            ConfigScreen(self._cfg, self._transport, node_name=node_name)
        )
        if changed:
            stream = self.query_one("#stream", StreamPanel)
            stream.write_system("config saved to config.yaml")

    def action_copy_log(self):
        stream = self.query_one("#stream", StreamPanel)
        text = stream.get_text(100)
        if not text:
            stream.write_system("nothing to copy")
            return
        copied = _copy_to_clipboard(text)
        if copied:
            stream.write_system("copied last 100 lines to clipboard")
        else:
            stream.write_system(
                "clipboard failed — install xclip, xsel, or wl-copy  "
                "(or check chiba.log for full history)"
            )

    def action_quit(self):
        self.exit()


def _copy_to_clipboard(text: str) -> bool:
    for cmd in (
        ["xclip", "-selection", "clipboard"],
        ["xsel", "--clipboard", "--input"],
        ["wl-copy"],
    ):
        try:
            subprocess.run(cmd, input=text.encode(), timeout=3,
                           check=True, capture_output=True)
            return True
        except (FileNotFoundError, subprocess.CalledProcessError,
                subprocess.TimeoutExpired):
            continue
    return False
