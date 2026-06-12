from abc import ABC, abstractmethod


class Plugin(ABC):
    cmd: str = ""
    description: str = ""
    local: bool = True
    # Only mesh_visible plugins are executable via inbound mesh DMs and
    # advertised in heartbeats. Wallet/payment commands must stay False —
    # otherwise any node on the mesh can drive this wallet remotely.
    mesh_visible: bool = False
    # Plugins that opt in receive the user's y/n confirmation replay as
    # handle(query, from_node=None, force=True).
    accepts_force: bool = False

    @abstractmethod
    def handle(self, query: str, from_node: str | None = None) -> str:
        ...

    def __repr__(self) -> str:
        return f"Plugin(cmd={self.cmd!r}, local={self.local})"
