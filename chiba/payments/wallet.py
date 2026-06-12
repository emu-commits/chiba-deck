"""
Payment wallet for chiba-deck.

Uses mesh_cash_crypto (Rust/PyO3) for all cryptographic operations.
Token state is stored in a plain SQLite DB (wallet.db) — not SQLCipher.
The production SQLCipher wallet lives in mesh-cash/daemon/wallet.py;
this is the UI-layer wallet for chiba-deck.

Wallet DB schema mirrors the mesh-cash daemon schema minus encryption.
"""

import importlib
import logging
import os
import re
import secrets
import sqlite3
import time
from pathlib import Path
from threading import Lock

from ..config import save_config
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
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    token_hex        TEXT NOT NULL,
    from_node        TEXT,
    denomination     INTEGER NOT NULL,
    commitment       TEXT NOT NULL,
    ts               REAL NOT NULL,
    redeemed         INTEGER NOT NULL DEFAULT 0,
    new_secret       TEXT,
    is_respend       INTEGER NOT NULL DEFAULT 0,
    spend_token_hex  TEXT
);
"""

_MIGRATIONS = [
    "ALTER TABLE received_tokens ADD COLUMN new_secret TEXT",
    "ALTER TABLE received_tokens ADD COLUMN is_respend INTEGER NOT NULL DEFAULT 0",
    "ALTER TABLE received_tokens ADD COLUMN spend_token_hex TEXT",
    # Reject replayed token messages (mesh duplicates, bridge echoes)
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_received_token_hex ON received_tokens(token_hex)",
]

_config_ref = None      # set by init_payments() — used by WalletInfoPlugin
_post_init_hook = None  # set by main.py — wires receiver/transport after live enable

def _mc():
    """Lazy import — fails gracefully if not installed."""
    try:
        import mesh_cash_crypto
        return mesh_cash_crypto
    except ImportError as e:
        log.warning(f"mesh_cash_crypto not importable: {e}")
        return None
    except Exception as e:
        log.error(f"mesh_cash_crypto import error: {e}", exc_info=True)
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
        self._migrate()
        try:
            os.chmod(db_path, 0o600)  # privkey + token secrets live here
        except OSError:
            pass

    def _migrate(self):
        for sql in _MIGRATIONS:
            try:
                self._conn.execute(sql)
                self._conn.commit()
            except Exception:
                pass  # column already exists

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

    def get_ready_unbound_tokens(self, denomination: int | None = None) -> list[dict]:
        """Return all ready unbound tokens, optionally filtered by denomination."""
        with self._lock:
            if denomination is not None:
                rows = self._conn.execute(
                    "SELECT id, denomination, secret, commitment FROM unbound_tokens "
                    "WHERE status = 'ready' AND denomination = ? ORDER BY id",
                    (denomination,),
                ).fetchall()
            else:
                rows = self._conn.execute(
                    "SELECT id, denomination, secret, commitment FROM unbound_tokens "
                    "WHERE status = 'ready' ORDER BY id"
                ).fetchall()
        return [dict(r) for r in rows]

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

    def in_use_count(self) -> int:
        """Tokens claimed by a send that never completed (crash mid-spend).
        Never auto-released: the token may already be on the mesh, and
        re-spending the same commitment would be caught as a double-spend
        at redemption — surface these for manual review instead."""
        with self._lock:
            return self._conn.execute(
                "SELECT COUNT(*) FROM unbound_tokens WHERE status='in_use'"
            ).fetchone()[0]

    # ── Received tokens ───────────────────────────────────────────────────────

    def add_received_token(self, token_hex: str, denomination: int,
                           commitment_hex: str, from_node: str | None = None,
                           new_secret: str | None = None) -> int | None:
        """Returns the new row id, or None if this exact token was already stored."""
        with self._lock:
            try:
                cur = self._conn.execute(
                    "INSERT INTO received_tokens "
                    "(token_hex, from_node, denomination, commitment, ts, new_secret) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (token_hex, from_node, denomination, commitment_hex, time.time(), new_secret),
                )
            except sqlite3.IntegrityError:
                return None
            self._conn.commit()
        return cur.lastrowid

    def add_received_respend_token(self, token_hex: str, denomination: int,
                                   commitment_hex: str, spend_token_hex: str,
                                   from_node: str | None = None,
                                   new_secret: str | None = None) -> int | None:
        """Store a received re-spend token along with the original spend token sidecar.
        Returns the new row id, or None if this exact token was already stored."""
        with self._lock:
            try:
                cur = self._conn.execute(
                    "INSERT INTO received_tokens "
                    "(token_hex, from_node, denomination, commitment, ts, "
                    " new_secret, is_respend, spend_token_hex) "
                    "VALUES (?, ?, ?, ?, ?, ?, 1, ?)",
                    (token_hex, from_node, denomination, commitment_hex, time.time(),
                     new_secret, spend_token_hex),
                )
            except sqlite3.IntegrityError:
                return None
            self._conn.commit()
        return cur.lastrowid

    def get_unredeemed_token(self, denomination: int) -> dict | None:
        """Return the oldest unredeemed received token of this denomination, or None."""
        with self._lock:
            row = self._conn.execute(
                "SELECT id, token_hex, commitment, from_node, "
                "       is_respend, spend_token_hex, new_secret "
                "FROM received_tokens "
                "WHERE denomination = ? AND redeemed = 0 ORDER BY ts ASC LIMIT 1",
                (denomination,),
            ).fetchone()
        return dict(row) if row else None

    def get_respendable_token(self, denomination: int) -> dict | None:
        """Return the oldest unredeemed received token that can be re-spent (has new_secret)."""
        with self._lock:
            row = self._conn.execute(
                "SELECT id, token_hex, commitment, from_node, new_secret "
                "FROM received_tokens "
                "WHERE denomination = ? AND redeemed = 0 AND new_secret IS NOT NULL "
                "ORDER BY ts ASC LIMIT 1",
                (denomination,),
            ).fetchone()
        return dict(row) if row else None

    def mark_token_redeemed(self, token_id: int) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE received_tokens SET redeemed = 1 WHERE id = ?", (token_id,)
            )
            self._conn.commit()

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
_respend_vk_path: str = ""
_zkey_path: str = ""
_claim_zkey_path: str = ""
_respend_zkey_path: str = ""
_wasm_path: str = ""
_claim_wasm_path: str = ""
_respend_wasm_path: str = ""
_snarkjs_path: str = ""


def init_payments(config) -> CryptoWallet | None:
    """
    Initialise the payment wallet from config. Returns the wallet instance,
    or None if payments are disabled or mesh_cash_crypto is not installed.

    Call once at startup from main.py before creating WalletPlugin.
    """
    global _wallet, _vk_path, _claim_vk_path, _respend_vk_path
    global _zkey_path, _claim_zkey_path, _respend_zkey_path
    global _wasm_path, _claim_wasm_path, _respend_wasm_path, _snarkjs_path
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
        _vk_path           = str(mc_path / "keys" / "verification_key.json")
        _claim_vk_path     = str(mc_path / "keys" / "claim_verification_key.json")
        _respend_vk_path   = str(mc_path / "keys" / "respend_verification_key.json")
        _zkey_path         = str(mc_path / "keys" / "meshcash_spend_dev.zkey")
        _claim_zkey_path   = str(mc_path / "keys" / "meshcash_claim_dev.zkey")
        _respend_zkey_path = str(mc_path / "keys" / "meshcash_respend_dev.zkey")
        _wasm_path         = str(mc_path / "circuit" / "build" / "meshcash_spend_js" / "meshcash_spend.wasm")
        _claim_wasm_path   = str(mc_path / "circuit" / "build" / "meshcash_claim_js" / "meshcash_claim.wasm")
        _respend_wasm_path = str(mc_path / "circuit" / "build" / "meshcash_spend_recursive_js" / "meshcash_spend_recursive.wasm")
        _snarkjs_path      = str(mc_path / "node_modules" / ".bin" / "snarkjs")
    else:
        log.warning("payments.mesh_cash_path not set or missing — verify/prove paths unavailable")

    _wallet = CryptoWallet(config.payments.wallet_db)
    _wallet.ensure_keypair()
    log.info(f"Payment wallet opened: {config.payments.wallet_db}")
    return _wallet


def get_wallet() -> CryptoWallet | None:
    return _wallet


def set_post_init_hook(cb) -> None:
    """Register a callback(wallet) run after payments are enabled live via
    ?wallet — main.py uses it to wire the token receiver, transport callbacks
    and heartbeat pubkey without a restart."""
    global _post_init_hook
    _post_init_hook = cb


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

        if denomination not in (1, 5, 10, 20, 50, 100):
            return f"invalid denomination ${denomination} — valid: $1 $5 $10 $20 $50 $100"

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

        # Try unbound token first; fall back to re-spending a received token
        unbound = _wallet.claim_unbound_token(denomination)

        if unbound is not None:
            # ── Direct spend path ─────────────────────────────────────────────
            try:
                sk = _wallet.get_keypair()[0]
                new_secret = mc.nullifier(sk)
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

            if self._transport is not None:
                # Send new_secret sidecar BEFORE the token so the receiver can associate them
                self._transport.send_new_secret(recipient_node, bytes.fromhex(new_secret))
                self._transport.send_token(recipient_node, token_bytes)
                sent = True
            else:
                sent = False

            if self._db is not None:
                self._db.insert_payment_out(recipient_node, denomination, token_hex)

            return f"sent ${denomination} to {target_handle} ✓" if sent else \
                   f"token generated but could not send (offline?) — stored in sent log"

        # ── Re-spend fallback ─────────────────────────────────────────────────
        received = _wallet.get_respendable_token(denomination)
        if received is None:
            return (
                f"no ${denomination} tokens available to send.\n"
                f"Run ?mint {denomination} then ?deposit {denomination} to fund your wallet,\n"
                f"or wait to receive a token from someone else."
            )

        if not _respend_zkey_path or not _respend_wasm_path:
            return "payments.mesh_cash_path not configured — cannot generate re-spend proof"

        kp = _wallet.get_keypair()
        if kp is None:
            return "wallet has no keypair — run ?wallet first"
        sk_hex, pk_hex = kp

        prior_rc_hex = received["token_hex"][2:66]   # bytes[1:33] of the token as hex
        prior_new_secret_hex = received["new_secret"]
        BN254_P = 21888242871839275222246405745257275088548364400416034343698204186575808495617
        fresh_new_secret_hex = secrets.randbelow(BN254_P).to_bytes(32, "big").hex()

        try:
            respend_token_hex = mc.generate_respend_token(
                _respend_zkey_path, _respend_wasm_path, _snarkjs_path,
                sk_hex, prior_new_secret_hex, fresh_new_secret_hex,
                prior_rc_hex, denomination, recipient_pubkey,
            )
        except Exception as e:
            log.error(f"re-spend proof generation failed: {e}")
            return f"re-spend proof generation failed: {e}"

        _wallet.mark_token_redeemed(received["id"])   # prevent double-spend
        respend_bytes = bytes.fromhex(respend_token_hex)
        original_token_bytes = bytes.fromhex(received["token_hex"])

        if self._transport is not None:
            # Send: sidecar (original spend token) + new_secret + re-spend token
            self._transport.send_spend_sidecar(recipient_node, original_token_bytes)
            self._transport.send_new_secret(recipient_node, bytes.fromhex(fresh_new_secret_hex))
            self._transport.send_token(recipient_node, respend_bytes)
            sent = True
        else:
            sent = False

        if self._db is not None:
            self._db.insert_payment_out(recipient_node, denomination, respend_token_hex)

        return f"re-sent ${denomination} to {target_handle} ✓" if sent else \
               f"re-spend token generated but could not send (offline?)"

    def balance_str(self) -> str:
        if _wallet is None:
            return "n/a (payments disabled)"
        return _wallet.balance_str()


class MintPlugin(Plugin):
    """
    `?mint [count] [denomination]` — generate unbound tokens locally.

    Creates (secret, commitment) pairs and stores them in the wallet.
    These represent the local half of a deposit: after minting, register
    each commitment on-chain by depositing face value in USDC to the
    MeshCash contract.

    Examples:
      ?mint          → 1 token at $5 (defaults)
      ?mint 5        → 5 tokens at $5
      ?mint 3 10     → 3 tokens at $10
    """

    cmd = "mint"
    description = "generate unbound tokens  e.g. ?mint 5 10"
    local = True

    def handle(self, query: str, from_node: str | None = None) -> str:
        mc = _mc()
        if mc is None or _wallet is None:
            return "payments not active — run ?wallet first"

        parts = query.split()
        try:
            count = int(parts[0]) if parts else 1
            denomination = int(parts[1].lstrip("$")) if len(parts) > 1 else 5
        except ValueError:
            return "usage: ?mint [count] [denomination]  e.g. ?mint 5 10"

        if count < 1 or count > 50:
            return "count must be 1–50"
        if denomination not in (1, 5, 10, 20, 50, 100):
            return f"invalid denomination ${denomination} — valid: $1 $5 $10 $20 $50 $100"

        minted = []
        for _ in range(count):
            try:
                sk = mc.generate_keypair()[0]
                commitment = mc.commitment(sk, denomination)
                _wallet.add_unbound_token(denomination, sk, commitment)
                minted.append(commitment[:12] + "...")
            except Exception as e:
                log.error(f"mint error: {e}")
                return f"minted {len(minted)}/{count} before error: {e}"

        inv = _wallet.unbound_count()
        total_line = "  ".join(f"${d}×{c}" for d, c in sorted(inv.items()))
        lines = [
            f"minted {count} × ${denomination} unbound token{'s' if count > 1 else ''}",
            f"wallet unbound: {total_line}",
            "",
            "Next: deposit face value USDC per token to the MeshCash contract,",
            "using each token's commitment as the deposit identifier.",
        ]
        return "\n".join(lines)


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


class PubkeyPlugin(Plugin):
    """
    `?pubkey` — print payment pubkey on its own line for easy copying.
    """

    cmd = "pubkey"
    description = "show payment pubkey for sharing"
    local = True

    def handle(self, query: str, from_node: str | None = None) -> str:
        if _wallet is None:
            return "payments not enabled — run ?wallet first"
        pk = _wallet.get_pubkey()
        return pk if pk else "no pubkey yet — restart after enabling payments"


class WalletInfoPlugin(Plugin):
    """
    `?wallet` — show payment layer status, or interactively enable payments.

    When not yet set up, offers a y/n prompt; on confirmation it writes
    payments.enabled=true to config.yaml and calls init_payments() live —
    no restart needed (the crypto library must already be installed via
    scripts/install_payments.sh).
    When active, shows pubkey, balance, and unbound token inventory.
    """

    cmd = "wallet"
    description = "payment wallet status and setup"
    local = True
    accepts_force = True

    def handle(self, query: str, from_node: str | None = None, force: bool = False) -> str:
        cfg = _config_ref
        mc = _mc()
        enabled = cfg is not None and cfg.payments.enabled

        if not enabled:
            # force=True is the y-confirmation replay from the UI prompt
            if force:
                return self._enable(cfg)
            return "[?] Enable payments? [y/n]"

        if mc is None:
            return (
                "── PAYMENT LAYER ──────────────────────\n"
                "  enabled:     YES\n"
                "  crypto lib:  NOT INSTALLED\n\n"
                "  Run:  bash scripts/install_payments.sh\n"
                "  then restart chiba-deck.\n"
                "────────────────────────────────────────"
            )

        lines = ["── PAYMENT LAYER ──────────────────────"]
        lines.append("  enabled:     YES")
        lines.append("  crypto lib:  OK")

        if _wallet is None:
            lines.append("  wallet:      NOT OPEN (restart to retry)")
            return "\n".join(lines)

        pk = _wallet.get_pubkey()
        lines.append(f"  pubkey:      {pk}" if pk else "  pubkey:      (generating...)")

        bal = _wallet.balance()
        bal_str = _wallet.balance_str() if bal else "$0.00"
        lines.append(f"  balance:     {bal_str}")

        unbound = _wallet.unbound_count()
        if unbound:
            parts = "  ".join(f"${d}×{c}" for d, c in sorted(unbound.items()))
            lines.append(f"  unbound:     {parts}  (ready to send)")
        else:
            lines.append("  unbound:     none")
            lines.append("")
            lines.append("  Run ?mint <count> <denomination> to generate tokens,")
            lines.append("  then deposit face value USDC per token to the MeshCash contract.")

        stuck = _wallet.in_use_count()
        if stuck:
            lines.append(f"  in-flight:   {stuck} token(s) claimed by an interrupted send")

        lines.append("────────────────────────────────────────")
        return "\n".join(lines)

    def _enable(self, cfg) -> str:
        if _mc() is None:
            return (
                "── PAYMENT LAYER ──────────────────────\n"
                "  crypto lib not installed.\n\n"
                "  Run:  bash scripts/install_payments.sh\n"
                "  Then try ?wallet again.\n"
                "────────────────────────────────────────"
            )
        cfg.payments.enabled = True
        try:
            save_config(cfg)
        except Exception as e:
            cfg.payments.enabled = False
            return f"failed to save config: {e}"
        importlib.invalidate_caches()
        wallet_instance = init_payments(cfg)
        if wallet_instance is None:
            cfg.payments.enabled = False
            save_config(cfg)
            return "payments failed to initialize — check chiba.log"
        if _post_init_hook is not None:
            try:
                _post_init_hook(wallet_instance)
            except Exception as e:
                log.error(f"payments post-init wiring failed: {e}", exc_info=True)
        pk = wallet_instance.get_pubkey() or "(generating...)"
        return (
            "── PAYMENT LAYER ──────────────────────\n"
            "  enabled:     YES\n"
            "  crypto lib:  OK\n"
            f"  pubkey:      {pk}\n"
            "  balance:     $0.00\n"
            "  unbound:     none\n"
            "────────────────────────────────────────\n"
            "Payments enabled."
        )
