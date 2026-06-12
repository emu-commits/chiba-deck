"""
?deposit — deposit unbound tokens on-chain to activate them for spending.

Usage:
  ?deposit           — deposit all unbound tokens (any denomination)
  ?deposit 5         — deposit only $5 unbound tokens

Each unbound token was created by ?mint.  Depositing registers the token's
commitment on the MeshCash contract and transfers face-value USDC into the
yield vault.  After this, the token can be sent to another node with ?pay.

Requires:
  - payments.enabled + private key in ETH_PRIVATE_KEY env var
  - payments.rpc_url, contract_address, usdc_address set in config.yaml
  - web3 installed (pip install web3)
"""

import logging
import os
import re

from ..plugins.base import Plugin

log = logging.getLogger(__name__)

USDC_SCALE = 10 ** 6

_DEPOSIT_ABI = [
    {
        "name": "deposit",
        "type": "function",
        "stateMutability": "nonpayable",
        "inputs": [
            {"name": "denomination", "type": "uint256"},
            {"name": "commitment",   "type": "bytes32"},
        ],
        "outputs": [],
    },
    {
        "name": "commitmentDenominations",
        "type": "function",
        "stateMutability": "view",
        "inputs":  [{"name": "", "type": "bytes32"}],
        "outputs": [{"name": "", "type": "uint256"}],
    },
]

_ERC20_ABI = [
    {
        "name": "balanceOf",
        "type": "function",
        "stateMutability": "view",
        "inputs":  [{"name": "account", "type": "address"}],
        "outputs": [{"name": "",        "type": "uint256"}],
    },
    {
        "name": "approve",
        "type": "function",
        "stateMutability": "nonpayable",
        "inputs":  [{"name": "spender", "type": "address"},
                    {"name": "amount",  "type": "uint256"}],
        "outputs": [{"name": "", "type": "bool"}],
    },
]


class DepositPlugin(Plugin):
    """
    `?deposit [denomination]` — deposit unbound tokens on-chain.

    Approves USDC and calls MeshCash.deposit() for each unbound token that
    hasn't been registered yet.  Tokens already on-chain are skipped.
    """

    cmd = "deposit"
    description = "deposit unbound tokens on-chain  e.g. ?deposit 5"
    local = True

    def handle(self, query: str, from_node: str | None = None) -> str:
        from .wallet import _wallet, _config_ref

        if _wallet is None:
            return "payments not active — run ?wallet for setup"

        cfg = _config_ref
        if cfg is None or not cfg.payments.rpc_url or not cfg.payments.contract_address:
            return "payments.rpc_url and payments.contract_address must be set in config.yaml"
        if not cfg.payments.usdc_address:
            return "payments.usdc_address must be set in config.yaml"

        private_key = cfg.payments.private_key or os.environ.get("ETH_PRIVATE_KEY", "")
        if not private_key:
            return "ETH_PRIVATE_KEY env var not set (or payments.private_key in config.yaml)"

        # ── Parse query ────────────────────────────────────────────────────────
        denomination: int | None = None
        m = re.search(r'\$?(\d+)', query)
        if m:
            try:
                denomination = int(m.group(1))
            except ValueError:
                pass
            if denomination is not None and denomination not in (1, 5, 10, 20, 50, 100):
                return f"invalid denomination ${denomination} — valid: $1 $5 $10 $20 $50 $100"

        # ── Get unbound tokens ─────────────────────────────────────────────────
        tokens = _wallet.get_ready_unbound_tokens(denomination)
        if not tokens:
            if denomination:
                return (
                    f"no unbound ${denomination} tokens — run ?mint {denomination} first\n"
                    f"(wallet: {_wallet.balance_str()})"
                )
            return "no unbound tokens — run ?mint <count> <denomination> first"

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

        account  = w3.eth.account.from_key(private_key)
        mc_addr  = Web3.to_checksum_address(cfg.payments.contract_address)
        usdc_addr = Web3.to_checksum_address(cfg.payments.usdc_address)

        meshcash = w3.eth.contract(address=mc_addr,   abi=_DEPOSIT_ABI)
        usdc     = w3.eth.contract(address=usdc_addr, abi=_ERC20_ABI)

        # ── Filter already-deposited tokens ───────────────────────────────────
        to_deposit = []
        already = 0
        for t in tokens:
            commitment_bytes = bytes.fromhex(t["commitment"])
            stored = meshcash.functions.commitmentDenominations(commitment_bytes).call()
            if stored != 0:
                already += 1
            else:
                to_deposit.append(t)

        if not to_deposit:
            return (
                f"all {len(tokens)} token{'s' if len(tokens) != 1 else ''} already deposited on-chain"
            )

        # ── Check USDC balance ─────────────────────────────────────────────────
        total_needed = sum(t["denomination"] for t in to_deposit) * USDC_SCALE
        usdc_bal = usdc.functions.balanceOf(account.address).call()

        if usdc_bal < USDC_SCALE:
            return (
                f"USDC balance too low: {usdc_bal / USDC_SCALE:.2f} USDC\n"
                f"Need {total_needed / USDC_SCALE:.2f} USDC for {len(to_deposit)} token(s).\n"
                "Get test USDC at https://faucet.circle.com"
            )

        if usdc_bal < total_needed:
            # Deposit as many as balance allows (smallest denominations first)
            to_deposit.sort(key=lambda t: t["denomination"])
            affordable = []
            running = 0
            for t in to_deposit:
                cost = t["denomination"] * USDC_SCALE
                if running + cost <= usdc_bal:
                    affordable.append(t)
                    running += cost
                else:
                    break
            skipped = len(to_deposit) - len(affordable)
            to_deposit = affordable
            log.info(
                f"USDC balance {usdc_bal/USDC_SCALE:.2f} — depositing "
                f"{len(to_deposit)}, skipping {skipped} (insufficient funds)"
            )
            if not to_deposit:
                return (
                    f"USDC balance ({usdc_bal / USDC_SCALE:.2f}) too low for "
                    f"smallest token (${min(t['denomination'] for t in tokens)})\n"
                    "Get test USDC at https://faucet.circle.com"
                )

        deposit_total = sum(t["denomination"] for t in to_deposit) * USDC_SCALE

        # ── Approve USDC ───────────────────────────────────────────────────────
        lines = [f"depositing {len(to_deposit)} token(s)  (${deposit_total // USDC_SCALE} USDC total)"]
        try:
            nonce = w3.eth.get_transaction_count(account.address)
            tx = usdc.functions.approve(mc_addr, deposit_total).build_transaction({
                "from":     account.address,
                "nonce":    nonce,
                "gas":      100_000,
                "gasPrice": w3.to_wei("0.01", "gwei"),
                "chainId":  chain_id,
            })
            signed  = account.sign_transaction(tx)
            receipt = w3.eth.wait_for_transaction_receipt(
                w3.eth.send_raw_transaction(signed.raw_transaction)
            )
            if receipt["status"] != 1:
                return "USDC approve reverted"
            nonce += 1
            lines.append(f"approve: {receipt['transactionHash'].hex()[:16]}...")
        except Exception as e:
            return f"USDC approve failed: {e}"

        # ── Deposit each token ─────────────────────────────────────────────────
        deposited = 0
        for t in to_deposit:
            commitment_bytes = bytes.fromhex(t["commitment"])
            try:
                tx = meshcash.functions.deposit(
                    t["denomination"], commitment_bytes
                ).build_transaction({
                    "from":     account.address,
                    "nonce":    nonce,
                    "gas":      300_000,
                    "gasPrice": w3.to_wei("0.01", "gwei"),
                    "chainId":  chain_id,
                })
                signed  = account.sign_transaction(tx)
                receipt = w3.eth.wait_for_transaction_receipt(
                    w3.eth.send_raw_transaction(signed.raw_transaction)
                )
                if receipt["status"] != 1:
                    lines.append(f"  ${t['denomination']} deposit reverted")
                else:
                    deposited += 1
                    lines.append(
                        f"  ${t['denomination']} deposited  "
                        f"{receipt['transactionHash'].hex()[:16]}..."
                    )
                    nonce += 1
            except Exception as e:
                lines.append(f"  ${t['denomination']} failed: {e}")

        summary_parts = [f"{deposited}/{len(to_deposit)} deposited"]
        if already:
            summary_parts.append(f"{already} already on-chain")
        lines.append("done — " + ", ".join(summary_parts))
        return "\n".join(lines)
