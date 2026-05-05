from .base import Plugin


class StatusPlugin(Plugin):
    cmd = "status"
    description = "wallet and node stats"
    local = True

    def __init__(self):
        self._db = None
        self._wallet = None

    def set_db(self, db):
        self._db = db

    def set_wallet(self, wallet):
        self._wallet = wallet

    def handle(self, query: str, from_node: str | None = None) -> str:
        node_count = self._db.get_node_count() if self._db else 0
        balance = self._wallet.balance_str() if self._wallet else "n/a"
        return f"wallet: {balance} | nodes: {node_count} online"
