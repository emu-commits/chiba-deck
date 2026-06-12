import datetime

from .base import Plugin


class HistoryPlugin(Plugin):
    """
    `?history [handle] [count]` — show recent messages, optionally from one node.

    Examples:
      ?history            last 20 messages
      ?history maria      last 20 messages involving maria
      ?history maria 50   last 50 messages involving maria
    """

    cmd = "history"
    description = "recent messages  e.g. ?history maria"
    local = True

    DEFAULT_COUNT = 20
    MAX_COUNT = 100

    def __init__(self):
        self._db = None

    def set_db(self, db):
        self._db = db

    def handle(self, query: str, from_node: str | None = None) -> str:
        if self._db is None:
            return "history: db not available"

        handle_filter = ""
        count = self.DEFAULT_COUNT
        for part in query.split():
            if part.isdigit():
                count = min(int(part), self.MAX_COUNT)
            else:
                handle_filter = part

        node_filter = ""
        if handle_filter:
            node_filter = self._db.find_node_by_handle(handle_filter)
            if not node_filter:
                return f"node '{handle_filter}' not found — try ?nodes"

        rows = self._db.get_recent_messages(max_age_s=7 * 86400, limit=500)
        if node_filter:
            rows = [r for r in rows
                    if r["from_id"] == node_filter or r["to_id"] == node_filter]
        rows = rows[-count:]

        if not rows:
            return "no messages" + (f" with {handle_filter}" if handle_filter else "") + " in the last 7 days"

        lines = []
        for r in rows:
            ts = datetime.datetime.fromtimestamp(r["ts"]).strftime("%a %H:%M")
            who = self._db.get_node_handle(r["from_id"]) or r["from_id"] or "me"
            lines.append(f"{ts}  {who}: {r['text']}")
        return "\n".join(lines)
