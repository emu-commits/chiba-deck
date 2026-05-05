from ..plugins.base import Plugin


class WalletPlugin(Plugin):
    cmd = "pay"
    description = "send mesh payment (ZK layer pending)"
    local = True

    def handle(self, query: str, from_node: str | None = None) -> str:
        return "payment layer not active — ZK circuit pending (separate repo)"

    def balance_str(self) -> str:
        return "n/a (ZK pending)"


class BalancePlugin(Plugin):
    cmd = "balance"
    description = "check wallet balance"
    local = True

    def handle(self, query: str, from_node: str | None = None) -> str:
        return "wallet: n/a — ZK payment layer not yet active"
