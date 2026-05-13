"""
Handles inbound Port 256 token messages (225-byte bound Groth16 proofs).

Rate-limited: max N verifications per sender per minute (proof verify is CPU-intensive).
Token verification uses mesh_cash_crypto (Rust/PyO3).
"""

import logging
import time

from ..events import Event, EventQueue, EventType

log = logging.getLogger(__name__)

_wallet = None
_db = None
_display_queue: EventQueue | None = None
_rate_limit_per_min: int = 4
_rate_buckets: dict[str, list[float]] = {}


def init(wallet, db, display_queue: EventQueue, config) -> None:
    global _wallet, _db, _display_queue, _rate_limit_per_min
    _wallet = wallet
    _db = db
    _display_queue = display_queue
    _rate_limit_per_min = config.payments.rate_limit_per_min


def _check_rate_limit(from_node: str) -> bool:
    now = time.time()
    cutoff = now - 60.0
    bucket = [t for t in _rate_buckets.get(from_node, []) if t > cutoff]
    if len(bucket) >= _rate_limit_per_min:
        log.debug(f"token rate limit hit for {from_node}")
        return False
    bucket.append(now)
    _rate_buckets[from_node] = bucket
    return True


def handle_token_message(token_bytes: bytes, from_node: str) -> None:
    if _wallet is None:
        return
    if len(token_bytes) != 225:
        log.debug(f"malformed token from {from_node}: {len(token_bytes)} bytes")
        return
    if not _check_rate_limit(from_node):
        return

    try:
        from .wallet import _mc, _vk_path
        mc = _mc()
        if mc is None:
            return

        pk = _wallet.get_pubkey()
        if not pk:
            return

        valid = mc.verify_token(_vk_path, token_bytes.hex(), pk)
    except Exception as e:
        log.warning(f"token verification error from {from_node}: {e}")
        return

    if not valid:
        log.debug(f"invalid token from {from_node}")
        return

    denomination = token_bytes[0]
    if denomination not in (1, 5, 10, 20, 50, 100):
        log.debug(f"invalid denomination {denomination} from {from_node}")
        return

    commitment_hex = token_bytes[1:33].hex()
    token_id = _wallet.add_received_token(token_bytes.hex(), denomination, commitment_hex, from_node)

    if _db is not None:
        _db.insert_payment_in(from_node, denomination, token_bytes.hex())

    handle = ""
    if _db is not None:
        handle = _db.get_node_handle(from_node) or from_node

    log.info(f"received ${denomination} token (id={token_id}) from {handle}")

    if _display_queue is not None:
        _display_queue.put_nowait(Event(
            type=EventType.PAYMENT_IN,
            from_node=from_node,
            payload=f"PAYMENT RECEIVED ${denomination}.00 from {handle}",
            meta={"denomination": denomination, "token_id": token_id},
        ))
