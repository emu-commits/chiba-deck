from rich.text import Text
from textual.reactive import reactive
from textual.widget import Widget


class StatusBar(Widget):
    DEFAULT_CSS = """
    StatusBar {
        height: 1;
        background: #0a0a0a;
        dock: top;
    }
    """

    node_count: reactive[int] = reactive(0)
    balance: reactive[str] = reactive("n/a")
    unread: reactive[int] = reactive(0)
    connected: reactive[bool] = reactive(False)

    def render(self) -> Text:
        dot = "●" if self.connected else "○"
        dot_style = "bold green" if self.connected else "bold red"
        t = Text(" CHIBA MESH ", style="bold green on black")
        t.append(dot + " ", style=f"{dot_style} on black")
        t.append(f"{self.node_count} nodes ", style="green on black")
        t.append("● ", style="dim green on black")
        t.append(f"wallet: {self.balance} ", style="green on black")
        t.append("● ", style="dim green on black")
        t.append(f"{self.unread} in last hr", style="green on black")
        t.append(" " * 200, style="on black")
        return t
