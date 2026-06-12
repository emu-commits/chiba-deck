from .base import Plugin


class NodesPlugin(Plugin):
    cmd = "nodes"
    description = "list online mesh nodes"
    local = True
    mesh_visible = True

    def __init__(self):
        self._db = None

    def set_db(self, db):
        self._db = db

    def handle(self, query: str, from_node: str | None = None) -> str:
        if not self._db:
            return "nodes: db not available"
        nodes = self._db.get_nodes_online()
        if not nodes:
            return "no nodes seen yet"
        lines = []
        for n in nodes[:6]:
            handle = n.get("handle") or n["node_id"]
            lines.append(f"{handle}")
        suffix = f" (+{len(nodes)-6} more)" if len(nodes) > 6 else ""
        return f"{len(nodes)} online: {', '.join(lines)}{suffix}"
