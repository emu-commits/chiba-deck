"""
?redeem — redeem a received MeshCash token for USDC on Base.

Usage:
  ?redeem              — redeem any received token (picks $5 first if available)
  ?redeem 10           — redeem a $10 token
  ?redeem 5 0xABC...   — redeem $5, send USDC to explicit ETH address

Requires:
  - payments.enabled = true, private key in ETH_PRIVATE_KEY env var
  - payments.rpc_url, contract_address set in config.yaml
  - web3 installed (pip install web3)
  - mesh_cash_crypto built with proof_bytes_to_solidity support
"""

import json
import logging
import os
import re

from ..plugins.base import Plugin

log = logging.getLogger(__name__)

_MESHCASH_REDEEM_ABI = [
    {
        "name": "redeem",
        "type": "function",
        "stateMutability": "nonpayable",
        "inputs": [
            {"name": "spendPA",             "type": "uint256[2]"},
            {"name": "spendPB",             "type": "uint256[2][2]"},
            {"name": "spendPC",             "type": "uint256[2]"},
            {"name": "claimPA",             "type": "uint256[2]"},
            {"name": "claimPB",             "type": "uint256[2][2]"},
            {"name": "claimPC",             "type": "uint256[2]"},
            {"name": "commitment",          "type": "bytes32"},
            {"name": "nullifier",           "type": "bytes32"},
            {"name": "recipientCommitment", "type": "bytes32"},
            {"name": "recipientPubkey",     "type": "uint256"},
            {"name": "denomination",        "type": "uint256"},
            {"name": "ethRecipient",        "type": "address"},
        ],
        "outputs": [],
    },
    {
        "name": "redeemRespend",
        "type": "function",
        "stateMutability": "nonpayable",
        "inputs": [
            {"name": "spendPA",                  "type": "uint256[2]"},
            {"name": "spendPB",                  "type": "uint256[2][2]"},
            {"name": "spendPC",                  "type": "uint256[2]"},
            {"name": "respendPA",                "type": "uint256[2]"},
            {"name": "respendPB",                "type": "uint256[2][2]"},
            {"name": "respendPC",                "type": "uint256[2]"},
            {"name": "claimPA",                  "type": "uint256[2]"},
            {"name": "claimPB",                  "type": "uint256[2][2]"},
            {"name": "claimPC",                  "type": "uint256[2]"},
            {"name": "commitment",               "type": "bytes32"},
            {"name": "spendNullifier",           "type": "bytes32"},
            {"name": "intermediateRC",           "type": "bytes32"},
            {"name": "intermediateHolderPubkey", "type": "uint256"},
            {"name": "respendNewRC",             "type": "bytes32"},
            {"name": "respendNullifier",         "type": "bytes32"},
            {"name": "finalRecipientPubkey",     "type": "uint256"},
            {"name": "denomination",             "type": "uint256"},
            {"name": "ethRecipient",             "type": "address"},
        ],
        "outputs": [],
    },
    {
        "name": "nullifiers",
        "type": "function",
        "stateMutability": "view",
        "inputs": [{"name": "", "type": "bytes32"}],
        "outputs": [{"name": "", "type": "bool"}],
    },
]


def _proof_to_solidity(mc, proof_hex: str):
    """Decompress 128-byte proof hex → (pA, pB, pC) as Python int arrays."""
    p = json.loads(mc.proof_bytes_to_solidity(proof_hex))
    pA = [int(p["pA"][0], 16), int(p["pA"][1], 16)]
    pB = [
        [int(p["pB"][0][0], 16), int(p["pB"][0][1], 16)],
        [int(p["pB"][1][0], 16), int(p["pB"][1][1], 16)],
    ]
    pC = [int(p["pC"][0], 16), int(p["pC"][1], 16)]
    return pA, pB, pC


class RedeemPlugin(Plugin):
    """
    `?redeem [amount] [eth_address]` — redeem a received token for USDC on Base.

    Generates a claim proof proving ownership of the recipient pubkey, then
    submits spend + claim proofs to MeshCash.redeem() on-chain.
    """

    cmd = "redeem"
    description = "redeem a received token for USDC  e.g. ?redeem 5"
    local = True

    def handle(self, query: str, from_node: str | None = None) -> str:
        from .wallet import (
            _mc, _wallet, _config_ref,
            _claim_zkey_path, _claim_wasm_path, _snarkjs_path,
            _respend_vk_path,
        )

        mc = _mc()
        if mc is None or _wallet is None:
            return "payments not active — run ?wallet for setup"

        if not hasattr(mc, "proof_bytes_to_solidity"):
            return (
                "mesh_cash_crypto needs rebuilding with proof_bytes_to_solidity support.\n"
                "In the mesh-cash repo: cd crypto && maturin develop --features pyo3-bindings"
            )

        cfg = _config_ref
        if cfg is None or not cfg.payments.rpc_url or not cfg.payments.contract_address:
            return "payments.rpc_url and payments.contract_address must be set in config.yaml"

        private_key = cfg.payments.private_key or os.environ.get("ETH_PRIVATE_KEY", "")
        if not private_key:
            return "ETH_PRIVATE_KEY env var not set (or payments.private_key in config.yaml)"

        # ── Parse query ────────────────────────────────────────────────────────
        parts = query.strip().split()
        denomination = 5
        eth_recipient = None

        for part in parts:
            if re.fullmatch(r'\$?(\d+)', part):
                try:
                    denomination = int(part.lstrip("$"))
                except ValueError:
                    pass
            elif re.fullmatch(r'0x[0-9a-fA-F]{40}', part):
                eth_recipient = part

        if denomination not in (1, 5, 10, 20, 50, 100):
            return f"invalid denomination ${denomination} — valid: $1 $5 $10 $20 $50 $100"

        # ── Find a token to redeem ─────────────────────────────────────────────
        token_row = _wallet.get_unredeemed_token(denomination)
        if token_row is None:
            bal = _wallet.balance()
            if bal:
                avail = "  ".join(f"{c}×${d}" for d, c in sorted(bal.items()))
                return f"no unredeemed ${denomination} tokens. Available: {avail}"
            return "no tokens to redeem — receive a payment first"

        token_hex = token_row["token_hex"]
        token_id  = token_row["id"]
        token_bytes = bytes.fromhex(token_hex)

        if len(token_bytes) != 225:
            return f"stored token has unexpected length {len(token_bytes)}"

        is_respend = bool(token_row.get("is_respend"))

        # ── Connect to Base ────────────────────────────────────────────────────
        try:
            from web3 import Web3
        except ImportError:
            return "web3 not installed — pip install 'web3>=7.0'"

        try:
            w3 = Web3(Web3.HTTPProvider(cfg.payments.rpc_url))
            chain_id = w3.eth.chain_id
        except Exception as e:
            return f"cannot connect to RPC {cfg.payments.rpc_url}: {e}"

        account = w3.eth.account.from_key(private_key)
        if eth_recipient is None:
            eth_recipient = account.address
        eth_recipient_cs = Web3.to_checksum_address(eth_recipient)
        eth_padded = eth_recipient.lower().lstrip("0x").zfill(64)

        meshcash = w3.eth.contract(
            address=Web3.to_checksum_address(cfg.payments.contract_address),
            abi=_MESHCASH_REDEEM_ABI,
        )

        # ── Keypair ────────────────────────────────────────────────────────────
        kp = _wallet.get_keypair()
        if kp is None:
            return "wallet has no keypair — run ?wallet first"
        sk_hex, pk_hex = kp

        if not _claim_zkey_path or not _claim_wasm_path or not _snarkjs_path:
            return (
                "payments.mesh_cash_path not configured — cannot generate claim proof.\n"
                "Set mesh_cash_path to the mesh-cash repo root in config.yaml."
            )

        if is_respend:
            return _redeem_respend(
                token_bytes, token_row, token_id,
                mc, w3, meshcash, account, chain_id,
                sk_hex, pk_hex, eth_recipient_cs, eth_padded,
                _claim_zkey_path, _claim_wasm_path, _snarkjs_path, _wallet,
            )

        # ── Direct token: parse fields ─────────────────────────────────────────
        # Layout: denomination(1) + recipient_commitment(32) + proof(128)
        #         + commitment(32) + redeem_nullifier(32)
        denom_byte           = token_bytes[0]
        recipient_commitment = token_bytes[1:33]
        spend_proof_bytes    = token_bytes[33:161]
        commitment           = token_bytes[161:193]
        nullifier_bytes      = token_bytes[193:225]

        if meshcash.functions.nullifiers(nullifier_bytes).call():
            _wallet.mark_token_redeemed(token_id)
            return f"nullifier already spent on-chain — marking token #{token_id} redeemed"

        try:
            spA, spB, spC = _proof_to_solidity(mc, spend_proof_bytes.hex())
        except Exception as e:
            return f"spend proof decompression failed: {e}"

        try:
            claim_proof_hex = mc.generate_claim_proof(
                _claim_zkey_path, _claim_wasm_path, _snarkjs_path,
                sk_hex, pk_hex, eth_padded,
            )
            clA, clB, clC = _proof_to_solidity(mc, claim_proof_hex)
        except Exception as e:
            return f"claim proof failed: {e}"

        recipient_pubkey_int = int(pk_hex, 16)
        try:
            fn = meshcash.functions.redeem(
                spA, spB, spC, clA, clB, clC,
                commitment, nullifier_bytes, recipient_commitment,
                recipient_pubkey_int, denom_byte, eth_recipient_cs,
            )
            tx      = fn.build_transaction({
                "from": account.address,
                "nonce": w3.eth.get_transaction_count(account.address),
                "gas": 2_000_000, "gasPrice": w3.to_wei("0.01", "gwei"), "chainId": chain_id,
            })
            receipt = w3.eth.wait_for_transaction_receipt(
                w3.eth.send_raw_transaction(account.sign_transaction(tx).raw_transaction)
            )
        except Exception as e:
            return f"redeem transaction failed: {e}"

        if receipt["status"] != 1:
            return f"redeem reverted ({receipt['transactionHash'].hex()}) — check contract state"

        _wallet.mark_token_redeemed(token_id)
        return f"redeemed ${denom_byte} → {eth_recipient_cs}\ntx: {receipt['transactionHash'].hex()}"


def _redeem_respend(
    token_bytes, token_row, token_id,
    mc, w3, meshcash, account, chain_id,
    sk_hex, pk_hex, eth_recipient_cs, eth_padded,
    claim_zkey, claim_wasm, snarkjs, wallet,
):
    """Handle redeemRespend() for a token that passed through one re-spend hop."""
    spend_token_hex = token_row.get("spend_token_hex") or ""
    from_node       = token_row.get("from_node") or ""

    if not spend_token_hex:
        return (
            "Re-spend token is missing the original spend token sidecar.\n"
            "This token was received before sidecar support was added (pre-0.2),\n"
            "or was sent by a node that hasn't updated.  Cannot redeem without it."
        )

    spend_bytes = bytes.fromhex(spend_token_hex)
    if len(spend_bytes) != 225:
        return f"stored spend_token_hex has unexpected length {len(spend_bytes)}"

    # ── Re-spend token layout ──────────────────────────────────────────────────
    # [0]:      denomination
    # [1:33]:   new_recipient_commitment  (respendNewRC)
    # [33:161]: re-spend proof
    # [161:193]:prior_recipient_commitment (intermediateRC)
    # [193:225]:respend_nullifier
    denom_byte   = token_bytes[0]
    respend_new_rc       = token_bytes[1:33]
    respend_proof_bytes  = token_bytes[33:161]
    intermediate_rc      = token_bytes[161:193]  # = spend_bytes[1:33]
    respend_nullifier    = token_bytes[193:225]

    # ── Original spend token layout ────────────────────────────────────────────
    # [1:33]:   recipient_commitment (= intermediate_rc, verified below)
    # [33:161]: spend proof
    # [161:193]:commitment (original deposit commitment)
    # [193:225]:spend_nullifier
    if spend_bytes[1:33] != intermediate_rc:
        return (
            "Sidecar mismatch: spend_token.recipient_commitment ≠ respend_token.prior_rc.\n"
            "The sidecar may belong to a different token."
        )

    spend_proof_bytes = spend_bytes[33:161]
    commitment        = spend_bytes[161:193]
    spend_nullifier   = spend_bytes[193:225]

    # ── intermediate_holder_pubkey — Bob's BN254 pubkey ────────────────────────
    # Fetched from node directory (broadcast via Port 259 heartbeat),
    # via the receiver's injected db handle.
    intermediate_pubkey_hex = ""
    from . import receiver as _recv
    db = getattr(_recv, "_db", None)
    if db is not None and from_node:
        try:
            intermediate_pubkey_hex = db.get_node_pubkey(from_node) or ""
        except Exception:
            pass

    if not intermediate_pubkey_hex:
        return (
            f"Cannot find BN254 pubkey for {from_node} (the intermediate holder).\n"
            "Ensure they have been seen on the mesh with payments enabled."
        )

    intermediate_pubkey_int = int(intermediate_pubkey_hex, 16)

    # ── Guard: check nullifiers ───────────────────────────────────────────────
    if meshcash.functions.nullifiers(spend_nullifier).call():
        wallet.mark_token_redeemed(token_id)
        return "spend nullifier already used — marking token redeemed"
    if meshcash.functions.nullifiers(respend_nullifier).call():
        wallet.mark_token_redeemed(token_id)
        return "respend nullifier already used — marking token redeemed"

    # ── Decompress proofs ─────────────────────────────────────────────────────
    try:
        spA, spB, spC = _proof_to_solidity(mc, spend_proof_bytes.hex())
    except Exception as e:
        return f"spend proof decompression failed: {e}"

    try:
        rsA, rsB, rsC = _proof_to_solidity(mc, respend_proof_bytes.hex())
    except Exception as e:
        return f"re-spend proof decompression failed: {e}"

    # ── Generate claim proof ──────────────────────────────────────────────────
    try:
        claim_proof_hex = mc.generate_claim_proof(
            claim_zkey, claim_wasm, snarkjs, sk_hex, pk_hex, eth_padded,
        )
        clA, clB, clC = _proof_to_solidity(mc, claim_proof_hex)
    except Exception as e:
        return f"claim proof failed: {e}"

    final_pubkey_int = int(pk_hex, 16)

    # ── Submit redeemRespend() ────────────────────────────────────────────────
    try:
        fn = meshcash.functions.redeemRespend(
            spA, spB, spC,
            rsA, rsB, rsC,
            clA, clB, clC,
            commitment,
            spend_nullifier,
            intermediate_rc,
            intermediate_pubkey_int,
            respend_new_rc,
            respend_nullifier,
            final_pubkey_int,
            denom_byte,
            eth_recipient_cs,
        )
        tx = fn.build_transaction({
            "from": account.address,
            "nonce": w3.eth.get_transaction_count(account.address),
            "gas": 3_000_000, "gasPrice": w3.to_wei("0.01", "gwei"), "chainId": chain_id,
        })
        receipt = w3.eth.wait_for_transaction_receipt(
            w3.eth.send_raw_transaction(account.sign_transaction(tx).raw_transaction)
        )
    except Exception as e:
        return f"redeemRespend transaction failed: {e}"

    if receipt["status"] != 1:
        return f"redeemRespend reverted ({receipt['transactionHash'].hex()})"

    wallet.mark_token_redeemed(token_id)
    return f"redeemed (re-spend) ${denom_byte} → {eth_recipient_cs}\ntx: {receipt['transactionHash'].hex()}"
