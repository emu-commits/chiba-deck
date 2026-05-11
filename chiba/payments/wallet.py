"""
Payment wallet for chiba-deck.

Uses mesh_cash_crypto (Rust/PyO3) for all cryptographic operations.
Token state is stored in a plain SQLite DB (wallet.db) — not SQLCipher.
The production SQLCipher wallet lives in mesh-cash/daemon/wallet.py;
this is the UI-layer wallet for chiba-deck.

Wallet DB schema mirrors the mesh-cash daemon schema minus encryption.
"""

import logging
import re
import sqlite3
import time
from pathlib import Path
from threading import Lock

from ..plugins.base import Plugin

log = logging.getLogger(__name__)

_WALLET_SCHEMA = """
CREATE TABLE IF NOT EXISTS keypair (
    id      INTEGER PRIMARY KEY CHECK (id = 1),
    privkey TEXT NOT NULL,
    pubkey  TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS unbound_tokens (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    denomination INTEGER NOT NULL,
    secret       TEXT NOT NULL,
    commitment   TEXT NOT NULL,
    status       TEXT NOT NULL DEFAULT 'ready'
);
CREATE TABLE IF NOT EXISTS received_tokens (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    token_hex    TEXT NOT NULL,
    from_node    TEXT,
    denomination INTEGER NOT NULL,
    commitment   TEXT NOT NULL,
    ts           REAL NOT NULL,
    redeemed     INTEGER NOT NULL DEFAULT 0
);
"""

_config_ref = None  # set by init_payments() — used by WalletInfoPlugin

def _mc():
    """Lazy import — fails gracefully if not installed."""
    try:
        import mesh_cash_crypto
        return mesh_cash_crypto
    except ImportError:
        return None


class CryptoWallet:
    """
    Thread-safe token wallet backed by a plain SQLite file.
    Holds keypair + unbound/received token tables.
    """

    def __init__(self, db_path: str):
        self._lock = Lock()
        self._conn = sqlite3.connect(str(db_path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(_WALLET_SCHEMA)
        self._conn.commit()

    def close(self):
        with self._lock:
            self._conn.close()

    # ── Keypair ───────────────────────────────────────────────────────────────

    def get_keypair(self) -> tuple[str, str] | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT privkey, pubkey FROM keypair WHERE id = 1"
            ).fetchone()
        return (row["privkey"], row["pubkey"]) if row else None

    def set_keypair(self, privkey_hex: str, pubkey_hex: str) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO keypair (id, privkey, pubkey) VALUES (1, ?, ?)",
                (privkey_hex, pubkey_hex),
            )
            self._conn.commit()

    def get_pubkey(self) -> str | None:
        kp = self.get_keypair()
        return kp[1] if kp else None

    def ensure_keypair(self) -> str:
        """Return our pubkey, generating a new keypair if none exists."""
        mc = _mc()
        if mc is None:
            return ""
        kp = self.get_keypair()
        if kp:
            return kp[1]
        sk, pk = mc.generate_keypair()
        self.set_keypair(sk, pk)
        log.info("Generated new BN254 keypair")
        return pk

    # ── Unbound tokens ────────────────────────────────────────────────────────

    def add_unbound_token(self, denomination: int, secret_hex: str, commitment_hex: str) -> int:
        with self._lock:
            cur = self._conn.execute(
                "INSERT INTO unbound_tokens (denomination, secret, commitment) VALUES (?, ?, ?)",
                (denomination, secret_hex, commitment_hex),
            )
            self._conn.commit()
        return cur.lastrowid

    def claim_unbound_token(self, denomination: int) -> dict | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT id, secret, commitment FROM unbound_tokens "
                "WHERE denomination = ? AND status = 'ready' LIMIT 1",
                (denomination,),
            ).fetchone()
            if row is None:
                return None
            self._conn.execute(
                "UPDATE unbound_tokens SET status = 'in_use' WHERE id = ?", (row["id"],)
            )
            self._conn.commit()
        return {"id": row["id"], "secret": row["secret"], "commitment": row["commitment"],
                "denomination": denomination}

    def consume_unbound_token(self, token_id: int) -> None:
        with self._lock:
            self._conn.execute("DELETE FROM unbound_tokens WHERE id = ?", (token_id,))
            self._conn.commit()

    def release_unbound_token(self, token_id: int) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE unbound_tokens SET status = 'ready' WHERE id = ?", (token_id,)
            )
            self._conn.commit()

    def unbound_count(self) -> dict[int, int]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT denomination, COUNT(*) FROM unbound_tokens WHERE status='ready' GROUP BY denomination"
            ).fetchall()
        return {r[0]: r[1] for r in rows}

    # ── Received tokens ───────────────────────────────────────────────────────

    def add_received_token(self, token_hex: str, denomination: int,
                           commitment_hex: str, from_node: str | None = None) -> int:
        with self._lock:
            cur = self._conn.execute(
                "INSERT INTO received_tokens (token_hex, from_node, denomination, commitment, ts) "
                "VALUES (?, ?, ?, ?, ?)",
                (token_hex, from_node, denomination, commitment_hex, time.time()),
            )
            self._conn.commit()
        return cur.lastrowid

    def balance(self) -> dict[int, int]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT denomination, COUNT(*) FROM received_tokens WHERE redeemed=0 GROUP BY denomination"
            ).fetchall()
        return {r[0]: r[1] for r in rows}

    def balance_str(self) -> str:
        bal = self.balance()
        if not bal:
            return "$0.00"
        total = sum(d * c for d, c in bal.items())
        parts = "  ".join(f"{c}×${d}" for d, c in sorted(bal.items(), reverse=True))
        return f"${total:.2f}  ({parts})"


# ── Module-level wallet instance (initialised once from main.py) ──────────────

_wallet: CryptoWallet | None = None
_vk_path: str = ""
_claim_vk_path: str = ""
_zkey_path: str = ""
_claim_zkey_path: str = ""
_wasm_path: str = ""
_claim_wasm_path: str = ""
_snarkjs_path: str = ""


def init_payments(config) -> CryptoWallet | None:
    """
    Initialise the payment wallet from config. Returns the wallet instance,
    or None if payments are disabled or mesh_cash_crypto is not installed.

    Call once at startup from main.py before creating WalletPlugin.
    """
    global _wallet, _vk_path, _claim_vk_path
    global _zkey_path, _claim_zkey_path, _wasm_path, _claim_wasm_path, _snarkjs_path
    global _config_ref
    _config_ref = config

    if not config.payments.enabled:
        log.info("Payments disabled (set payments.enabled=true in config.yaml)")
        return None

    mc = _mc()
    if mc is None:
        log.warning("mesh_cash_crypto not installed — payments disabled")
        return None

    mc_path = Path(config.payments.mesh_cash_path) if config.payments.mesh_cash_path else None

    if mc_path and mc_path.exists():
        _vk_path         = str(mc_path / "keys" / "verification_key.json")
        _claim_vk_path   = str(mc_path / "keys" / "claim_verification_key.json")
        _zkey_path       = str(mc_path / "keys" / "meshcash_spend_dev.zkey")
        _claim_zkey_path = str(mc_path / "keys" / "meshcash_claim_dev.zkey")
        _wasm_path       = str(mc_path / "circuit" / "build" / "meshcash_spend_js" / "meshcash_spend.wasm")
        _claim_wasm_path = str(mc_path / "circuit" / "build" / "meshcash_claim_js" / "meshcash_claim.wasm")
        _snarkjs_path    = str(mc_path / "node_modules" / ".bin" / "snarkjs")
    else:
        log.warning("payments.mesh_cash_path not set or missing — verify/prove paths unavailable")

    _wallet = CryptoWallet(config.payments.wallet_db)
    _wallet.ensure_keypair()
    log.info(f"Payment wallet opened: {config.payments.wallet_db}")
    return _wallet


def get_wallet() -> CryptoWallet | None:
    return _wallet


# ── Plugins ───────────────────────────────────────────────────────────────────

class WalletPlugin(Plugin):
    """
    `?pay <amount> to <handle>` — send a bound ZK token to a mesh node.

    Requires:
      - payments.enabled = true in config.yaml
      - An unbound token of the requested denomination in wallet.db
        (generated by batch_mint.py against an Ethereum deposit)
      - Recipient's pubkey resolvable from the node directory
    """

    cmd = "pay"
    description = "send mesh payment  e.g. ?pay 5 to bob"
    local = True

    def __init__(self):
        self._db = None
        self._transport = None

    def set_db(self, db):
        self._db = db

    def set_transport(self, transport):
        self._transport = transport

    def handle(self, query: str, from_node: str | None = None) -> str:
        mc = _mc()
        if mc is None or _wallet is None:
            return "payment layer not active (mesh_cash_crypto not installed)"

        # ── Parse: "5 to bob" / "pay bob $5" / "send ten dollars to alice" ──
        amount_match = re.search(r'\$?(\d+)', query)
        name_match   = re.search(r'\bto\s+(\w+)', query, re.IGNORECASE)

        if not amount_match or not name_match:
            return "usage: ?pay <amount> to <handle>  e.g. ?pay 5 to bob"

        try:
            denomination = int(amount_match.group(1))
        except ValueError:
            return "invalid amount"

        if denomination not in (1, 5, 10, 20):
            return f"invalid denomination ${denomination} — valid: $1 $5 $10 $20"

        target_handle = name_match.group(1).lower()

        if self._db is None:
            return "internal error: db not set"

        recipient_node = self._db.find_node_by_handle(target_handle)
        if not recipient_node:
            return f"unknown node '{target_handle}' — have they been seen on the mesh?"

        # Resolve pubkey: stored in nodes table as meta.pubkey (if they broadcast it)
        recipient_pubkey = self._db.get_node_pubkey(recipient_node)
        if not recipient_pubkey:
            return (
                f"{target_handle} has no registered payment pubkey. "
                "They need to run chiba-deck with payments enabled."
            )

        # Claim an unbound token and generate the bound proof
        unbound = _wallet.claim_unbound_token(denomination)
        if unbound is None:
            return (
                f"no unbound ${denomination} tokens available. "
                "Run batch_mint.py to generate tokens from your Ethereum deposits."
            )

        try:
            sk = _wallet.get_keypair()[0]
            new_secret = mc.nullifier(sk)  # deterministic fresh secret per spend
            token_hex = mc.generate_bound_token(
                _zkey_path, _wasm_path, _snarkjs_path,
                unbound["secret"], new_secret, unbound["commitment"],
                denomination, recipient_pubkey,
            )
        except Exception as e:
            _wallet.release_unbound_token(unbound["id"])
            log.error(f"proof generation failed: {e}")
            return f"proof generation failed: {e}"

        _wallet.consume_unbound_token(unbound["id"])

        token_bytes = bytes.fromhex(token_hex)
        sent = False
        if self._transport is not None:
            sent = self._transport.send_token(recipient_node, token_bytes)

        if self._db is not None:
            self._db.insert_payment_out(recipient_node, denomination, token_hex)

        if sent:
            return f"sent ${denomination} to {target_handle} ✓"
        else:
            return f"token generated but could not send (offline?) — stored in sent log"

    def balance_str(self) -> str:
        if _wallet is None:
            return "n/a (payments disabled)"
        return _wallet.balance_str()


class BalancePlugin(Plugin):
    """
    `?balance` — show wallet balance.
    """

    cmd = "balance"
    description = "show wallet balance"
    local = True

    def handle(self, query: str, from_node: str | None = None) -> str:
        if _wallet is None:
            return "wallet: payments off — run ?wallet for setup steps"
        bal = _wallet.balance()
        if not bal:
            return "wallet: $0.00 (no tokens received yet)"
        return f"wallet: {_wallet.balance_str()}"


class WalletInfoPlugin(Plugin):
    """
    `?wallet` — show payment layer status and setup instructions.

    When payments are disabled or not yet configured, prints exact steps.
    When active, shows pubkey, balance, and unbound token inventory.
    """

    cmd = "wallet"
    description = "payment wallet status and setup"
    local = True

    def handle(self, query: str, from_node: str | None = None) -> str:
        cfg = _config_ref
        mc = _mc()

        lines = ["── PAYMENT LAYER ──────────────────────"]

        # ── 1. Enabled? ──────────────────────────────────────────────────────
        enabled = cfg is not None and cfg.payments.enabled
        lines.append(f"  enabled:     {'YES' if enabled else 'NO'}")

        if not enabled:
            lines.append("")
            lines.append("  To enable, edit config.yaml:")
            lines.append("    payments:")
            lines.append("      enabled: true")
            mc_path_hint = (
                cfg.payments.mesh_cash_path if (cfg and cfg.payments.mesh_cash_path)
                else "/path/to/mesh-cash"
            )
            lines.append(f"      mesh_cash_path: {mc_path_hint}")
            lines.append("")
            lines.append("  Then install the crypto library:")
            lines.append(f"    cd {mc_path_hint}/crypto")
            lines.append("    maturin develop --features pyo3-bindings")
            lines.append("")
            lines.append("  Restart chiba-deck — keypair is auto-generated.")
            return "\n".join(lines)

        # ── 2. Crypto lib installed? ──────────────────────────────────────────
        if mc is None:
            mc_path = cfg.payments.mesh_cash_path or "/path/to/mesh-cash"
            lines.append("  crypto lib:  NOT INSTALLED")
            lines.append("")
            lines.append("  Install mesh_cash_crypto:")
            lines.append(f"    cd {mc_path}/crypto")
            lines.append("    maturin develop --features pyo3-bindings")
            lines.append("  Then restart chiba-deck.")
            return "\n".join(lines)

        lines.append("  crypto lib:  OK")

        # ── 3. Wallet / keypair ───────────────────────────────────────────────
        if _wallet is None:
            lines.append("  wallet:      NOT OPEN (restart to retry)")
            return "\n".join(lines)

        pk = _wallet.get_pubkey()
        if pk:
            lines.append(f"  pubkey:      {pk[:16]}...{pk[-8:]}")
        else:
            lines.append("  pubkey:      (generating...)")

        # ── 4. Balance ────────────────────────────────────────────────────────
        bal = _wallet.balance()
        total = sum(d * c for d, c in bal.items())
        bal_str = _wallet.balance_str() if bal else "$0.00"
        lines.append(f"  balance:     {bal_str}")

        # ── 5. Unbound token inventory ────────────────────────────────────────
        unbound = _wallet.unbound_count()
        if unbound:
            parts = "  ".join(f"${d}×{c}" for d, c in sorted(unbound.items()))
            lines.append(f"  unbound:     {parts}  (ready to send)")
        else:
            lines.append("  unbound:     none")
            lines.append("")
            lines.append("  To top up, deposit USDC to the smart contract then run:")
            mc_path = cfg.payments.mesh_cash_path or "/path/to/mesh-cash"
            lines.append(f"    cd {mc_path}")
            lines.append("    python -m daemon.batch_mint --count=5 --denomination=5")

        lines.append("────────────────────────────────────────")
        return "\n".join(lines)
