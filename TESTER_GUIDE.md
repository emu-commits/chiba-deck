# Chiba Deck — Tester Guide

Send ZK bearer tokens over LoRa mesh and redeem them for USDC on Base.
No internet is needed to send or receive — only for initial deposit and final redemption.

This guide gets you from zero to sending and receiving mesh payments on Base Sepolia (testnet).

---

## What you're testing

Chiba Deck is a Linux terminal mesh client built on Meshtastic.  The payment layer sends
225-byte Groth16 proofs over LoRa radio.  The client uses natural-language command input
and a plugin architecture for mesh services.

Testnet contract on Base Sepolia: `0x5ceE62435b801AB3ae0c5c48BE39639F18Ed5De0`

---

## Hardware

**Minimum (MQTT mode — no radio required):**
- Any Linux machine or Raspberry Pi
- Internet connection (for deposit/redeem)
- A shared MQTT broker (or run `mosquitto` locally)

**Full mesh (BLE mode):**
- Any Linux machine or Raspberry Pi
- A Meshtastic device connected via Bluetooth (T-Beam, Heltec, RAK, etc.)
- At least one other tester with their own Meshtastic node within radio range

MQTT mode lets you test the full payment flow without hardware.
BLE mode tests the real over-the-air token exchange.

---

## Prerequisites

### Python 3.11+
```bash
python3 --version   # need 3.11+
```

### Rust (for the crypto library)
```bash
curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh
source ~/.cargo/env
```

### Node.js 18+ (for snarkjs proof generation)
```bash
node --version   # need 18+
```

### A funded Base Sepolia wallet
Create an Ethereum wallet (e.g. MetaMask), export the private key, and set it as an
environment variable:
```bash
export ETH_PRIVATE_KEY=0xYourPrivateKeyHere
```

**Never use a wallet with real funds on a testnet.**

---

## Installation

### 1. Clone both repos
```bash
git clone https://github.com/emu-commits/chiba-deck
git clone https://github.com/emu-commits/mesh-cash

# They should be siblings:
# ~/projects/chiba-deck
# ~/projects/mesh-cash
```

### 2. Install chiba-deck
```bash
cd chiba-deck
pip install -e ".[payments]"   # includes web3
```

For BLE support:
```bash
pip install -e ".[payments,ble]"
```

### 3. Build the crypto library
```bash
bash scripts/install_payments.sh
# Builds mesh_cash_crypto (Rust) for your Python.  Takes ~30 seconds.
```

Verify:
```bash
python3 -c "import mesh_cash_crypto; print('crypto: OK')"
```

### 4. Install snarkjs
```bash
cd ../mesh-cash
npm install
```

### 5. Configure chiba-deck
```bash
cd ../chiba-deck
cp config.example.yaml config.yaml
```

Edit `config.yaml`:
```yaml
node_id: "!YourMeshtasticNodeID"   # from your Meshtastic device, e.g. !a1b2c3d4
node_handle: "yourname"            # how others see you on the mesh

# MQTT mode (no radio hardware needed):
mqtt:
  broker: "localhost"   # or a shared broker IP
  port: 1883
  topic_rx: "msh/test/rx"
  topic_tx: "msh/test/tx"

# BLE mode (comment out mqtt section, uncomment ble):
# ble:
#   enabled: true
#   device_name: ""    # leave blank to auto-detect

payments:
  enabled: true
  mesh_cash_path: "/absolute/path/to/mesh-cash"   # required for proof generation
  rpc_url: "https://base-sepolia-rpc.publicnode.com"
  contract_address: "0x5ceE62435b801AB3ae0c5c48BE39639F18Ed5De0"
  usdc_address: "0x036CbD53842c5426634e7929541eC2318f3dCF7e"
  chain_id: 84532
```

---

## Get test USDC

1. Go to **https://faucet.circle.com**
2. Select "Base Sepolia" network
3. Enter your wallet address
4. Receive 10 test USDC (repeatable)

Base Sepolia ETH for gas: **https://www.alchemy.com/faucets/base-sepolia**
(Gas on Base is ~$0.001 per tx.)

---

## Running chiba-deck

```bash
cd chiba-deck
chiba
```

Or directly:
```bash
python3 -m chiba.main
```

The UI shows a message stream at top and a command input at the bottom.
Commands start with `?`.  You can also type natural language — the NLP layer
will interpret it and ask for confirmation before acting.

---

## Full payment walkthrough

### Step 1 — Enable payments
```
?wallet
```
First run shows wallet status and prompts you to enable payments.  Type `y` to enable.
Payments require `ETH_PRIVATE_KEY` to be set in your environment.

After enabling, `?wallet` shows:
- your BN254 pubkey (used by senders to bind tokens to you)
- token inventory by denomination
- any "in-flight" tokens (claimed by a send that was interrupted — these can't be
  re-spent since the commitment may already be on the mesh)

### Step 2 — Mint unbound tokens
```
?mint 1 5
```
Creates one $5 unbound token locally (just a secret + commitment hash — no network).

```
?mint 3 5
```
Creates three $5 tokens.  Supported denominations: $1, $5, $10, $20, $50, $100.

### Step 3 — Deposit on-chain
```
?deposit 5
```
Registers each $5 token's commitment on the MeshCash contract and deposits $5 USDC per
token into the yield vault.  Requires your `ETH_PRIVATE_KEY` and internet.

Output:
```
depositing 1 token(s)  ($5 USDC total)
approve: 0x3f8a1c...
  $5 deposited  0x7b2e4d...
done — 1/1 deposited
```

### Step 4 — Send to another tester
```
?pay 5 to alice
```
Generates a ZK spend proof bound to Alice's BN254 pubkey and transmits it over the mesh.
Proof generation takes 2–8 minutes on a Pi 3B, ~10 seconds on a laptop.

Alice's node must have been seen on the mesh and have a registered payment pubkey.
Pubkeys are broadcast automatically at startup and on a daily heartbeat.

### Step 5 — Share your pubkey (for receiving)
Your pubkey is broadcast automatically.  To see or share it manually:
```
?pubkey
```

### Step 6 — Check your balance
```
?balance
```
Shows received tokens not yet redeemed, e.g. `$5.00  (1×$5)`.

### Step 7 — Redeem for USDC
```
?redeem 5
```
Generates a claim proof (milliseconds), submits it to the MeshCash contract, and
receives $5 USDC at your ETH address.

Works for both direct tokens (one hop: deposit → you) and re-spend tokens
(two hops: deposit → someone else → you).  For re-spend tokens, `?redeem` automatically
uses `redeemRespend()` with the original spend proof that arrived alongside the token.

With explicit recipient:
```
?redeem 5 0xYourOtherAddress
```

Output (direct):
```
redeemed $5 → 0xYourAddress
tx: 0x9a3f...
```

Output (re-spend):
```
redeemed (re-spend) $5 → 0xYourAddress
tx: 0x9a3f...
```

---

## Messaging and discovery

### See who's on the mesh
```
?nodes
```
Lists nodes heard in the last 48 hours with handle, hops, and SNR.

### Send a direct message
```
?dm alice hey are you at the market?
```
Or in natural language: `send a message to alice asking if she's at the market`

### Broadcast to everyone
```
?say hello from chiba
```

### Message history
```
?history            — last 20 messages
?history alice      — messages with alice
?history alice 50   — last 50 messages with alice
```

---

## Verifying on-chain

Base Sepolia explorer: **https://sepolia.basescan.org**

- MeshCash contract: `0x5ceE62435b801AB3ae0c5c48BE39639F18Ed5De0`
- Check `commitmentDenominations(bytes32)` to verify a deposit is registered
- Check `nullifiers(bytes32)` to verify a token has been redeemed
- `Deposit` and `Redeem` events are indexed and searchable

---

## On-chain status report

From the mesh-cash repo:
```bash
cd mesh-cash
python3 scripts/status.py
```
Shows contract balances, vault state, total liabilities, and invariant checks.

---

## Security model notes for testers

Wallet commands (`?pay`, `?balance`, `?mint`, `?deposit`, `?redeem`) are **local-only**.
Remote nodes cannot trigger your wallet by sending you DM commands — attempting to do so
is silently blocked and logged.  This is intentional: only commands explicitly marked as
mesh-visible (`?nodes`, `?help`) are reachable from the mesh.

Remote service bindings are **first-claim-wins**.  If node A announces `?wiki` and later
node B also announces `?wiki`, your client stays bound to node A.  The binding expires
naturally when node A goes offline (48 h timeout).

---

## Known limitations (testnet beta)

- **Single-denomination per `?pay`** — each send is one token.  Batch sending planned.
- **Proof time** — spend proof generation is 2–8 min on Pi 3B, ~10s on a laptop.
  Claim proof is always fast (milliseconds).
- **MQTT is not end-to-end encrypted** — use BLE or a private MQTT broker for testing.
- **Dev proving keys** — `meshcash_spend_dev.zkey` has a known toxic waste for testnet.
  Production will use a ceremony-derived key.
- **Natural language mode** — NLP uses TF-IDF embeddings with no LLM.  When confidence
  is low it asks `[y/n]` before acting.  You can always use the `?command` form directly
  to skip NLP entirely.

---

## Reporting issues

Open an issue at: **https://github.com/emu-commits/chiba-deck/issues**

Include:
- Your OS and Python version (`python3 --version`)
- The command that failed
- Output from `chiba.log` (in the chiba-deck directory)
- Whether you're using BLE or MQTT mode

For contract-level issues, include the transaction hash from `sepolia.basescan.org`.

---

## Command reference

### Chat
| Command | Description |
|---|---|
| `?say <message>` | Broadcast to all nodes  (also: `?s`, `?chat`) |
| `?dm <handle> <message>` | Send a direct message to a node |
| `?nodes` | List nodes seen in last 48 h |
| `?history [handle] [n]` | Recent message history (default 20) |
| `?help` | Full command list |
| `?status` | Node count and connection status |

### Wallet
| Command | Description |
|---|---|
| `?wallet` | Payment status, enable payments, token inventory |
| `?pubkey` | Show your BN254 payment pubkey |
| `?balance` | Show received token balance |
| `?mint [count] [denom]` | Generate unbound tokens locally |
| `?deposit [denom]` | Deposit unbound tokens on-chain (needs internet) |
| `?pay <amount> to <handle>` | Send a ZK token over the mesh |
| `?redeem [amount] [0xAddr]` | Redeem a received token for USDC (needs internet) |

### App controls
| Key | Action |
|---|---|
| `Ctrl+O` | Open config (MQTT broker, topics) |
| `Ctrl+Y` | Copy last 100 lines to clipboard |
| `Ctrl+Q` | Quit |
| `Escape` | Clear input line |
