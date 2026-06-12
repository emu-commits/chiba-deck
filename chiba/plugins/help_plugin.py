from .base import Plugin

_MESH_HELP = "?nodes ?help | chiba-deck v0.1"

_LOCAL_HELP = """\
── mesh commands ────────────────────────────────────
  ?say <message>            broadcast to all nodes  (also: ?s, ?chat)
  ?dm <handle> <message>    send a direct message to a node
  ?nodes                    list nodes seen in last 48 h
  ?status                   wallet balance + node count
  ?history [handle] [n]     show recent message history
  ?market                   marketplace listings — forwarded if online
  ?help                     this help
── wallet ───────────────────────────────────────────
  ?wallet                   payment status / enable payments
  ?balance                  check wallet balance
  ?pay <amt> to <handle>    send ZK payment over the mesh
  ?mint [count] [denom]     generate unbound tokens
  ?deposit [denom]          register tokens on-chain (needs internet)
  ?redeem [denom] [addr]    redeem a token for USDC (needs internet)
  ?pubkey                   show payment pubkey for sharing
── app controls ─────────────────────────────────────
  Ctrl+O   open config
  Ctrl+Y   copy last 100 lines to clipboard
  Ctrl+Q   quit  (also: type quit / exit / q)
  Escape   clear the input line\
"""


class HelpPlugin(Plugin):
    cmd = "help"
    description = "list available commands"
    local = True
    mesh_visible = True

    def __init__(self):
        self._registry = None

    def set_registry(self, registry):
        self._registry = registry

    def handle(self, query: str, from_node: str | None = None) -> str:
        if from_node:
            # Short reply for mesh DM — 200-char limit
            return _MESH_HELP
        return _LOCAL_HELP
