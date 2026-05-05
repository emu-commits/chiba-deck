from abc import ABC, abstractmethod


class Plugin(ABC):
    cmd: str = ""
    description: str = ""
    local: bool = True

    @abstractmethod
    def handle(self, query: str, from_node: str | None = None) -> str:
        ...

    def __repr__(self) -> str:
        return f"Plugin(cmd={self.cmd!r}, local={self.local})"
