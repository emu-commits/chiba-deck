"""
Handles inbound payment messages on all three payment ports:
  Port 255 (new_secret): 32-byte re-spend secret sidecar — buffered by sender
  Port 256 (token):      225-byte direct or re-spend token
  Port 257 (spend_sidecar): 225-byte original spend token for re-spend redemption

Rate-limited: max N token verifications per sender per minute.
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

# Buffers keyed by from_node — cleared after association or TTL
_new_secret_buf:    dict[str, tuple[str, float]] = {}   # from_node → (secret_hex, ts)
_spend_sidecar_buf: dict[str, tuple[str, float]] = {}   # from_node → (token_hex, ts)
_BUFFER_TTL = 120.0  # seconds — discard unassociated sidecars after 2 min


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


def _expire_buffers():
    cutoff = time.time() - _BUFFER_TTL
    for node in [k for k, v in _new_secret_buf.items() if v[1] < cutoff]:
        del _new_secret_buf[node]
    for node in [k for k, v in _spend_sidecar_buf.items() if v[1] < cutoff]:
        del _spend_sidecar_buf[node]


def handle_new_secret_message(secret_bytes: bytes, from_node: str) -> None:
    """Buffer a 32-byte new_secret sidecar from a sender."""
    if len(secret_bytes) != 32:
        return
    _expire_buffers()
    _new_secret_buf[from_node] = (secret_bytes.hex(), time.time())
    log.debug(f"buffered new_secret from {from_node}")


def handle_spend_sidecar_message(token_bytes: bytes, from_node: str) -> None:
    """Buffer a 225-byte original spend token sidecar from a sender."""
    if len(token_bytes) != 225:
        return
    _expire_buffers()
    _spend_sidecar_buf[from_node] = (token_bytes.hex(), time.time())
    log.debug(f"buffered spend_sidecar from {from_node}")


def handle_token_message(token_bytes: bytes, from_node: str) -> None:
    if _wallet is None:
        return
    if len(token_bytes) != 225:
        log.debug(f"malformed token from {from_node}: {len(token_bytes)} bytes")
        return
    if not _check_rate_limit(from_node):
        return

    try:
        from .wallet import _mc, _vk_path, _respend_vk_path
        mc = _mc()
        if mc is None:
            return

        pk = _wallet.get_pubkey()
        if not pk:
            return

        # Pop buffered sidecars for this sender
        new_secret_hex = _new_secret_buf.pop(from_node, (None,))[0]
        spend_sidecar  = _spend_sidecar_buf.pop(from_node, (None,))[0]

        # Try direct spend token first
        is_respend = False
        token_hex  = token_bytes.hex()
        valid      = False

        try:
            valid = mc.verify_token(_vk_path, token_hex, pk)
        except Exception as e:
            log.debug(f"verify_token error from {from_node}: {e}")

        if not valid and _respend_vk_path:
            try:
                valid = mc.verify_respend_token(_respend_vk_path, token_hex, pk)
                if valid:
                    is_respend = True
            except Exception as e:
                log.debug(f"verify_respend_token error from {from_node}: {e}")

        if not valid:
            log.debug(f"invalid token from {from_node}")
            return

        denomination = token_bytes[0]
        if denomination not in (1, 5, 10, 20, 50, 100):
            log.debug(f"invalid denomination {denomination} from {from_node}")
            return

    except Exception as e:
        log.warning(f"token verification error from {from_node}: {e}")
        return

    try:
        if is_respend:
            # bytes[1:33] = new_recipient_commitment; bytes[161:193] = prior_recipient_commitment
            commitment_hex = token_bytes[1:33].hex()

            # Verify sidecar links to this re-spend token:
            # spend_sidecar[1:33] (recipient_commitment) must equal token[161:193] (prior_rc)
            if spend_sidecar:
                sidecar_bytes = bytes.fromhex(spend_sidecar)
                if len(sidecar_bytes) == 225 and sidecar_bytes[1:33] != token_bytes[161:193]:
                    log.warning(f"spend_sidecar intermediateRC mismatch from {from_node} — discarding")
                    spend_sidecar = None

            token_id = _wallet.add_received_respend_token(
                token_hex=token_hex,
                denomination=denomination,
                commitment_hex=commitment_hex,
                spend_token_hex=spend_sidecar or "",
                from_node=from_node,
                new_secret=new_secret_hex,
            )
        else:
            commitment_hex = token_bytes[1:33].hex()
            token_id = _wallet.add_received_token(
                token_hex=token_hex,
                denomination=denomination,
                commitment_hex=commitment_hex,
                from_node=from_node,
                new_secret=new_secret_hex,
            )
    except Exception as e:
        log.error(f"wallet store error from {from_node}: {e}")
        return

    if token_id is None:
        log.info(f"duplicate token from {from_node} ignored (already in wallet)")
        return

    if _db is not None:
        _db.insert_payment_in(from_node, denomination, token_hex)

    handle = ""
    if _db is not None:
        handle = _db.get_node_handle(from_node) or from_node

    kind = "RE-SPEND " if is_respend else ""
    log.info(f"received {kind}${denomination} token (id={token_id}) from {handle}")

    if _display_queue is not None:
        _display_queue.put_nowait(Event(
            type=EventType.PAYMENT_IN,
            from_node=from_node,
            payload=f"PAYMENT RECEIVED ${denomination}.00 from {handle}",
            meta={"denomination": denomination, "token_id": token_id, "is_respend": is_respend},
        ))
