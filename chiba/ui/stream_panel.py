import datetime

from rich.text import Text
from textual.widgets import RichLog

from ..events import Event, EventType


class StreamPanel(RichLog):
    DEFAULT_CSS = """
    StreamPanel {
        border: solid #1a3a1a;
        background: #020902;
        color: #00ff41;
        padding: 0 1;
        scrollbar-color: #00ff41 #0a0a0a;
    }
    """

    MAX_BUFFER = 500

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._plain_lines: list[str] = []

    def _store(self, plain: str):
        self._plain_lines.append(plain)
        if len(self._plain_lines) > self.MAX_BUFFER:
            self._plain_lines = self._plain_lines[-self.MAX_BUFFER:]

    def get_text(self, n: int = 100) -> str:
        return "\n".join(self._plain_lines[-n:])

    def _ts(self, event: Event | None = None) -> str:
        ts = event.ts if event else None
        if ts:
            return datetime.datetime.fromtimestamp(ts).strftime("%H:%M")
        return datetime.datetime.now().strftime("%H:%M")

    def render_event(self, event: Event):
        ts = self._ts(event)

        if event.type == EventType.HEARTBEAT:
            caps = event.meta.get("caps", [])
            caps_str = " ".join(f"?{c}" for c in caps) if caps else "(no caps)"
            line = Text(f"[{ts}] ", style="#2a4a2a")
            line.append("NODE ", style="bold yellow")
            line.append(f"{event.from_node} ", style="bold #00ff41")
            line.append(f"[{caps_str}]", style="#666666")

        elif event.type == EventType.MESSAGE:
            line = Text(f"[{ts}] ", style="#2a4a2a")
            if event.from_node:
                line.append(f"{event.from_node}", style="bold #00cc33")
                if event.to_node:
                    line.append(" → ", style="#444444")
                    line.append(f"{event.to_node} ", style="#00cc33")
                else:
                    line.append(": ", style="#444444")
            line.append(event.payload, style="#ccffcc")

        elif event.type == EventType.PAYMENT_IN:
            line = Text(f"[{ts}] ", style="#2a4a2a")
            line.append("PAYMENT IN ", style="bold bright_green")
            line.append(event.payload, style="bright_green")

        elif event.type == EventType.PAYMENT_OUT:
            line = Text(f"[{ts}] ", style="#2a4a2a")
            line.append("PAYMENT OUT ", style="bold #ffaa00")
            line.append(event.payload, style="#ffaa00")

        elif event.type == EventType.SYSTEM:
            line = Text(f"[{ts}] ", style="#2a4a2a")
            line.append("SYS ", style="bold #ff8800")
            line.append(event.payload, style="#888888")

        else:
            line = Text(f"[{ts}] {event.payload}", style="#444444")

        self._store(line.plain)
        self.write(line)

    def write_system(self, text: str):
        ts = datetime.datetime.now().strftime("%H:%M")
        line = Text(f"[{ts}] ", style="#2a4a2a")
        line.append("SYS ", style="bold #ff8800")
        line.append(text, style="#888888")
        self._store(line.plain)
        self.write(line)

    def write_reply(self, text: str):
        ts = datetime.datetime.now().strftime("%H:%M")
        line = Text(f"[{ts}] ", style="#2a4a2a")
        line.append("→ ", style="bold #00ff41")
        line.append(text, style="#ffffff")
        self._store(line.plain)
        self.write(line)

    def write_input_echo(self, text: str):
        ts = datetime.datetime.now().strftime("%H:%M")
        line = Text(f"[{ts}] ", style="#2a4a2a")
        line.append("> ", style="bold #00aaff")
        line.append(text, style="#88ccff")
        self._store(line.plain)
        self.write(line)
