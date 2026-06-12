import logging
import time

from .db import Database
from .plugins.base import Plugin

log = logging.getLogger(__name__)


class ServiceRegistry:
    def __init__(self, db: Database):
        self._db = db
        self._local: dict[str, Plugin] = {}
        self._remote: dict[str, dict] = {}  # cmd -> {node_id, last_seen}

    def register_local(self, plugin: Plugin):
        self._local[plugin.cmd] = plugin
        log.debug(f"registry: local plugin registered: ?{plugin.cmd}")

    def get_local(self, cmd: str) -> Plugin | None:
        return self._local.get(cmd)

    def upsert_remote(self, node_id: str, caps: list[str], ts: float | None = None):
        if ts is None:
            ts = time.time()
        for cap in caps:
            if cap in self._local:
                continue
            existing = self._remote.get(cap)
            if existing and existing["node_id"] != node_id:
                # First registration wins: a competing node cannot hijack a bound cap.
                # The user must explicitly delete the binding (future: ?forget <cap>)
                # before a new node can claim it.
                log.debug(f"registry: ?{cap} already bound to {existing['node_id']}, ignoring claim from {node_id}")
                continue
            self._remote[cap] = {"node_id": node_id, "last_seen": ts}
            self._db.register_remote_service(cap, node_id)

    def get_remote(self, cmd: str) -> str | None:
        entry = self._remote.get(cmd)
        if entry:
            return entry["node_id"]
        for row in self._db.get_remote_services():
            if row["cmd"] == cmd:
                return row["node_id"]
        return None

    def local_caps(self) -> list[str]:
        """Commands advertised to the mesh — mesh-visible plugins only."""
        return [cmd for cmd, p in self._local.items() if p.mesh_visible]

    def describe_all(self) -> list[dict]:
        result = []
        for cmd, plugin in self._local.items():
            result.append({
                "cmd": cmd,
                "description": plugin.description,
                "local": True,
            })
        for cmd, info in self._remote.items():
            result.append({
                "cmd": cmd,
                "node_id": info["node_id"],
                "local": False,
            })
        return result

    def expire_offline(self, threshold_s: float = 172800):
        cutoff = time.time() - threshold_s
        self._remote = {
            cmd: info for cmd, info in self._remote.items()
            if info["last_seen"] > cutoff
        }
        self._db.expire_offline_services(threshold_s)

    def restore_from_db(self):
        for row in self._db.get_remote_services():
            cmd = row["cmd"]
            if cmd not in self._local:
                self._remote[cmd] = {
                    "node_id": row["node_id"],
                    "last_seen": row["last_seen"],
                }
