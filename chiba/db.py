import json
import sqlite3
import threading
import time
from contextlib import contextmanager


SCHEMA = """
CREATE TABLE IF NOT EXISTS nodes (
    node_id   TEXT PRIMARY KEY,
    handle    TEXT,
    caps_json TEXT,
    hops      INTEGER DEFAULT 0,
    snr       REAL DEFAULT 0.0,
    last_seen REAL,
    pubkey    TEXT
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
        # Plugin handlers run in worker threads (asyncio.to_thread) while the
        # event loop also writes — serialize write transactions explicitly.
        self._lock = threading.Lock()

    def connect(self):
        self._conn = sqlite3.connect(self.path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.executescript(SCHEMA)
        self._migrate()
        self._conn.commit()
        self.prune_old_data()

    def _migrate(self):
        cols = {r[1] for r in self._conn.execute("PRAGMA table_info(nodes)")}
        if "pubkey" not in cols:
            self._conn.execute("ALTER TABLE nodes ADD COLUMN pubkey TEXT")

    @contextmanager
    def tx(self):
        with self._lock:
            try:
                yield self._conn
                self._conn.commit()
            except Exception:
                self._conn.rollback()
                raise

    def upsert_node(self, node_id: str, handle: str = "", caps: list | None = None,
                    hops: int = 0, snr: float = 0.0):
        # caps=None means "no capability info in this packet" — preserve what we
        # already know rather than wiping it (plain chat messages carry no caps).
        caps_json = json.dumps(caps) if caps is not None else None
        with self.tx():
            self._conn.execute("""
                INSERT INTO nodes (node_id, handle, caps_json, hops, snr, last_seen)
                VALUES (?, ?, COALESCE(?, '[]'), ?, ?, ?)
                ON CONFLICT(node_id) DO UPDATE SET
                    handle=CASE WHEN excluded.handle != '' THEN excluded.handle ELSE handle END,
                    caps_json=COALESCE(?, caps_json),
                    hops=excluded.hops,
                    snr=excluded.snr,
                    last_seen=excluded.last_seen
            """, (node_id, handle, caps_json, hops, snr, time.time(), caps_json))

    def get_node_handle(self, node_id: str) -> str:
        row = self._conn.execute(
            "SELECT handle FROM nodes WHERE node_id = ?", (node_id,)
        ).fetchone()
        return row["handle"] if row and row["handle"] else ""

    def get_node_pubkey(self, node_id: str) -> str | None:
        row = self._conn.execute(
            "SELECT pubkey FROM nodes WHERE node_id = ?", (node_id,)
        ).fetchone()
        return row["pubkey"] if row and row["pubkey"] else None

    def set_node_pubkey(self, node_id: str, pubkey_hex: str) -> None:
        with self.tx():
            self._conn.execute(
                "UPDATE nodes SET pubkey = ? WHERE node_id = ?",
                (pubkey_hex, node_id),
            )

    def find_node_by_handle(self, handle: str) -> str:
        """Return node_id for an exact (then prefix) case-insensitive handle match."""
        row = self._conn.execute(
            "SELECT node_id FROM nodes WHERE lower(handle) = lower(?)", (handle,)
        ).fetchone()
        if row:
            return row["node_id"]
        row = self._conn.execute(
            "SELECT node_id FROM nodes WHERE lower(handle) LIKE lower(?) ORDER BY last_seen DESC",
            (f"{handle}%",)
        ).fetchone()
        return row["node_id"] if row else ""

    def find_node_by_handle_exact(self, handle: str) -> str:
        """Return node_id for an exact case-insensitive handle match only (no prefix)."""
        row = self._conn.execute(
            "SELECT node_id FROM nodes WHERE lower(handle) = lower(?)", (handle,)
        ).fetchone()
        return row["node_id"] if row else ""

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
        # Only update last_seen/description when the same node re-announces.
        # A different node cannot overwrite an existing binding (first-claim-wins).
        with self.tx():
            self._conn.execute("""
                INSERT INTO service_registry (cmd, node_id, description, last_seen)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(cmd) DO UPDATE SET
                    description=CASE WHEN service_registry.node_id=excluded.node_id
                                     THEN excluded.description ELSE service_registry.description END,
                    last_seen=CASE WHEN service_registry.node_id=excluded.node_id
                                   THEN excluded.last_seen ELSE service_registry.last_seen END
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

    def get_recent_messages(self, direction: str | None = None,
                            max_age_s: float = 86400, limit: int = 200) -> list[dict]:
        cutoff = time.time() - max_age_s
        if direction:
            rows = self._conn.execute(
                "SELECT * FROM messages WHERE ts > ? AND direction = ? ORDER BY ts ASC LIMIT ?",
                (cutoff, direction, limit)
            ).fetchall()
        else:
            rows = self._conn.execute(
                "SELECT * FROM messages WHERE ts > ? ORDER BY ts ASC LIMIT ?",
                (cutoff, limit)
            ).fetchall()
        return [dict(r) for r in rows]

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

    def insert_payment_in(self, from_node: str, amount: float, token_hex: str) -> int:
        with self.tx():
            cur = self._conn.execute(
                "INSERT INTO payments_in (from_node, amount, token, ts, status) VALUES (?, ?, ?, ?, ?)",
                (from_node, amount, token_hex, time.time(), "received")
            )
        return cur.lastrowid

    def insert_payment_out(self, to_node: str, amount: float, token_hex: str) -> int:
        with self.tx():
            cur = self._conn.execute(
                "INSERT INTO payments_out (to_node, amount, token, ts, status) VALUES (?, ?, ?, ?, ?)",
                (to_node, amount, token_hex, time.time(), "sent")
            )
        return cur.lastrowid

    def get_payments_in(self, limit: int = 50) -> list[dict]:
        rows = self._conn.execute(
            "SELECT from_node, amount, ts, status FROM payments_in ORDER BY ts DESC LIMIT ?",
            (limit,)
        ).fetchall()
        return [dict(r) for r in rows]

    def insert_event(self, type_: str, from_id: str, payload: str):
        with self.tx():
            self._conn.execute(
                "INSERT INTO events (type, from_id, payload, ts) VALUES (?, ?, ?, ?)",
                (type_, from_id, payload, time.time())
            )

    def prune_old_data(self, max_age_s: float = 7776000) -> None:
        # Payments and wallet records are never pruned — they are financial history.
        # Everything else is pruned after max_age_s (default 90 days).
        cutoff = time.time() - max_age_s
        with self.tx():
            self._conn.execute("DELETE FROM messages WHERE ts < ?", (cutoff,))
            self._conn.execute("DELETE FROM events WHERE ts < ?", (cutoff,))
            self._conn.execute("DELETE FROM market_listings WHERE ts < ?", (cutoff,))
