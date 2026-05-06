import json
import sqlite3
import time
from contextlib import contextmanager


SCHEMA = """
CREATE TABLE IF NOT EXISTS nodes (
    node_id   TEXT PRIMARY KEY,
    handle    TEXT,
    caps_json TEXT,
    hops      INTEGER DEFAULT 0,
    snr       REAL DEFAULT 0.0,
    last_seen REAL
);

CREATE TABLE IF NOT EXISTS messages (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    from_id   TEXT,
    to_id     TEXT,
    text      TEXT,
    ts        REAL,
    direction TEXT
);

CREATE TABLE IF NOT EXISTS service_registry (
    cmd         TEXT PRIMARY KEY,
    node_id     TEXT,
    description TEXT,
    last_seen   REAL
);

CREATE TABLE IF NOT EXISTS market_listings (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    seller_node TEXT,
    item        TEXT,
    price_token TEXT,
    ts          REAL
);

CREATE TABLE IF NOT EXISTS payments_in (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    from_node TEXT,
    amount    REAL,
    token     TEXT,
    ts        REAL,
    status    TEXT
);

CREATE TABLE IF NOT EXISTS payments_out (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    to_node TEXT,
    amount  REAL,
    token   TEXT,
    ts      REAL,
    status  TEXT
);

CREATE TABLE IF NOT EXISTS events (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    type    TEXT,
    from_id TEXT,
    payload TEXT,
    ts      REAL
);
"""


class Database:
    def __init__(self, path: str):
        self.path = path
        self._conn: sqlite3.Connection | None = None

    def connect(self):
        self._conn = sqlite3.connect(self.path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.executescript(SCHEMA)
        self._conn.commit()

    @contextmanager
    def tx(self):
        try:
            yield self._conn
            self._conn.commit()
        except Exception:
            self._conn.rollback()
            raise

    def upsert_node(self, node_id: str, handle: str = "", caps: list | None = None,
                    hops: int = 0, snr: float = 0.0):
        caps_json = json.dumps(caps or [])
        with self.tx():
            self._conn.execute("""
                INSERT INTO nodes (node_id, handle, caps_json, hops, snr, last_seen)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(node_id) DO UPDATE SET
                    handle=CASE WHEN excluded.handle != '' THEN excluded.handle ELSE handle END,
                    caps_json=excluded.caps_json,
                    hops=excluded.hops,
                    snr=excluded.snr,
                    last_seen=excluded.last_seen
            """, (node_id, handle, caps_json, hops, snr, time.time()))

    def get_node_handle(self, node_id: str) -> str:
        row = self._conn.execute(
            "SELECT handle FROM nodes WHERE node_id = ?", (node_id,)
        ).fetchone()
        return row["handle"] if row and row["handle"] else ""

    def get_nodes_online(self, max_age_s: float = 172800) -> list[dict]:
        cutoff = time.time() - max_age_s
        rows = self._conn.execute(
            "SELECT * FROM nodes WHERE last_seen > ? ORDER BY last_seen DESC",
            (cutoff,)
        ).fetchall()
        return [dict(r) for r in rows]

    def get_node_count(self) -> int:
        return self._conn.execute(
            "SELECT COUNT(*) FROM nodes WHERE last_seen > ?",
            (time.time() - 172800,)
        ).fetchone()[0]

    def register_remote_service(self, cmd: str, node_id: str, description: str = ""):
        with self.tx():
            self._conn.execute("""
                INSERT INTO service_registry (cmd, node_id, description, last_seen)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(cmd) DO UPDATE SET
                    node_id=excluded.node_id,
                    description=excluded.description,
                    last_seen=excluded.last_seen
            """, (cmd, node_id, description, time.time()))

    def get_remote_services(self) -> list[dict]:
        rows = self._conn.execute("SELECT * FROM service_registry").fetchall()
        return [dict(r) for r in rows]

    def expire_offline_services(self, threshold_s: float = 172800):
        cutoff = time.time() - threshold_s
        with self.tx():
            self._conn.execute(
                "DELETE FROM service_registry WHERE last_seen < ?", (cutoff,)
            )

    def insert_message(self, from_id: str, to_id: str, text: str, direction: str = "in"):
        with self.tx():
            self._conn.execute(
                "INSERT INTO messages (from_id, to_id, text, ts, direction) VALUES (?, ?, ?, ?, ?)",
                (from_id, to_id, text, time.time(), direction)
            )

    def get_unread_count(self) -> int:
        cutoff = time.time() - 3600
        return self._conn.execute(
            "SELECT COUNT(*) FROM messages WHERE direction='in' AND ts > ?",
            (cutoff,)
        ).fetchone()[0]

    def insert_event(self, type_: str, from_id: str, payload: str):
        with self.tx():
            self._conn.execute(
                "INSERT INTO events (type, from_id, payload, ts) VALUES (?, ?, ?, ?)",
                (type_, from_id, payload, time.time())
            )
