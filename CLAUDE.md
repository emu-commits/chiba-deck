# Chiba Deck — Project Context for Claude Code

## What This Is

A Linux terminal client that extends Meshtastic LoRa mesh radio into a "sub-internet" — a self-organizing network of services, markets, chat, and eventually offline payments. The client is the user-facing layer that makes the mesh useful, sticky, and commercially viable.

Two repos:
- **chiba-deck** (this repo) — the terminal client, mesh services, NLP layer
- **separate repo (TBD)** — the ZK payment system (crypto layer, circuit, smart contract)

The business model: provide essential mesh infrastructure and UX, then bridge real value onto the mesh via USDC-backed ZK tokens so users can transact without internet.

---

## Repo: chiba-deck — Terminal Client

### What Already Exists (Do Not Rebuild)

Running on hardware today:
- Pi Zero W + Muzi R1 Neo + MQTT bridge → radio modem layer
- Pi 3B + wiki-bot (systemd service) → application/compute layer
- SQLite wiki content store
- `?`-prefix command dispatch (`?wiki`, `?nodes`, `?ping`, `?help`)
- 15-second per-sender cooldown → bandwidth discipline
- DM-only responses → unicast pattern

### Hard Constraints

- Runs entirely in Linux terminal — no GUI, no browser
- No LLM at runtime — GloVe 50d embeddings + regex only (Pi Zero W class: 512MB RAM)
- Fully offline capable after initial setup
- Targets Pi Zero W minimum; Pi 3B/4 preferred for payment proof generation
- Never flood the mesh — all data flows are rate-limited and unicast where possible
- Stock Meshtastic firmware only — never custom firmware on T-Beam

### Architecture

```
Client (terminal) ←MQTT/WiFi← Pi 3B daemon ←MQTT← Pi Zero W ←LoRa← T-Beam/R1 Neo
```

The terminal client is a pure UI layer. It never talks to the radio directly. Everything goes through MQTT.

MQTT topics:
- `msh/xxxx/rx` — inbound from mesh
- `msh/xxxx/tx` — outbound to mesh

Command namespace: `?` prefix for bot commands. Plain text = chat → DM.

### Terminal UI

Library: Python **textual** (curses fallback).

Layout: cyberpunk/cyberdeck aesthetic.
```
+═══════════════════════════════════════════════════+
║ CHIBA MESH ● 3 nodes ● $47.50 ● 2 msgs           ║  ← status bar
+═══════════════════════════════════════════════════+
║                                                   ║
║  [stream panel — new events scroll in]            ║
║                                                   ║
║  12:04 Maria: hey are you at the market           ║
║  12:05 NEW LISTING Bob: tomatoes $2/kg            ║
║  12:06 PAYMENT RECEIVED $5.00 from Alex           ║
║  12:07 WIKI "jaguar" — Panthera onca...           ║
║                                                   ║
+═══════════════════════════════════════════════════+
║ > _                                               ║  ← input line
+═══════════════════════════════════════════════════+
```

Status bar: live node count, wallet balance, unread message count.

Event queue pattern — never dump everything at once:
```python
event_queue = asyncio.Queue()

async def display_loop():
    while True:
        event = await event_queue.get()
        render_event(event)
        await asyncio.sleep(0.3)  # controlled pace
```

Everything displayed is simultaneously written to SQLite. SQLite is source of truth; stream panel is a view.

### NLP Layer — No LLM

Three layers composed in sequence:

**Layer 1 — Intent Classification (GloVe 50d embeddings)**

Pre-compute embeddings for canonical phrases per intent at build time. At runtime: cosine similarity against the matrix. Microseconds, no inference.

```python
intents = {
    "check_balance":   ["how much do I have", "wallet balance", ...],
    "send_payment":    ["pay someone", "send money to", ...],
    "query_wiki":      ["look up", "what is", "tell me about", ...],
    "list_market":     ["what's for sale", "show listings", ...],
    "list_nodes":      ["who's online", "active nodes", ...],
    "buy_item":        [...],
    "post_listing":    [...],
    "check_history":   [...],
    "ping_node":       [...],
    "get_help":        [...],
    "show_status":     [...],
    "receive_payment": [...],
}
```

**Layer 2 — Entity Extraction (regex, per-intent)**

```python
# send_payment:
amount = re.search(r'\$?(\d+\.?\d*)', user_input)
name   = re.search(r'to (\w+)', user_input)

# query_wiki:
topic  = re.search(r'(?:about|lookup|what is) (.+)', user_input)
```

**Layer 3 — Confirmation before action**

When confidence < threshold, show interpretation and ask `[y/n]`. Wrong interpretations become implicit training data.

```
> send ten bucks to maria for the tomatoes
  Understood: Send $10.00 to Maria (@a3f2b891)
  Confirm? [y/n] _
```

**Future upgrade path**: NLP classifier is a swappable component. Same tool registry + execution layer works unchanged when a local LLM replaces GloVe on Pi 3B/4 hardware.

### Tool Architecture

```python
class Tool:
    name: str
    intent: str
    required_entities: list[str]
    optional_entities: list[str]

    def can_execute(self, entities: dict) -> bool
    def execute(self, entities: dict) -> Event
    def describe(self) -> str  # used by ?help
```

**Dynamic tool registry from mesh heartbeats** — this is the key architectural insight. Every node that broadcasts capabilities is registering tools. The mesh IS the tool registry. A node goes offline → its tools disappear automatically (heartbeat expiry). New node joins with `caps: ["medical"]` → every other node gains a new tool.

### Heartbeat & Node Discovery

Nodes broadcast once per day (±10 min jitter to prevent synchronised floods). Human-readable plaintext so non-participating Meshtastic users understand what they see:

```
>: ?wiki ?nodes ?ping | ?help for info
```

`?help` returns a DM with full capability list — keeps broadcast minimal.

### SQLite Schema

```sql
CREATE TABLE nodes (
    node_id   TEXT PRIMARY KEY,
    last_seen INTEGER,       -- unix timestamp
    caps      TEXT,          -- JSON array: ["wiki","market",...]
    hops      INTEGER,       -- from Meshtastic packet metadata
    snr       REAL
);

-- nodes unseen 2+ days considered offline

CREATE TABLE messages        (/* all sent/received text */);
CREATE TABLE market_listings (/* active marketplace posts */);
CREATE TABLE payments_in     (/* received token records */);
CREATE TABLE payments_out    (/* sent token records */);
CREATE TABLE wiki_cache      (/* query→answer + timestamp */);
CREATE TABLE events          (/* raw event log, all types */);
```

History queries are natural language too:
- "what did maria say yesterday" → `SELECT * FROM messages WHERE sender='maria' AND timestamp > yesterday`
- "show my payments this week" → `SELECT * FROM payments_in WHERE timestamp > week_ago`

### Build Components & Order

1. **Embedding index builder** — offline, one-time. Input: canonical phrases. Output: matrix file. Stack: Python + numpy (~50 lines)
2. **Intent classifier** — runtime, microseconds. Cosine similarity against GloVe matrix
3. **Entity extractor** — runtime, per-intent regex
4. **Tool registry** — dict lookup + execute()
5. **Terminal UI** — Python textual, two-panel
6. **Event store** — SQLite, one table per event type
7. **Heartbeat service** — periodic broadcast + upsert received heartbeats

**Immediate next task**: Wiki subject narrowing (2nd arg disambiguation for `?wiki` queries).

**Then**: Heartbeat broadcaster + node table → Terminal UI shell → Embedding index + intent classifier → Tool registry scaffold → Payment tools (when ZK layer ready).

---

## Separate Repo: ZK Payment System

*(Summarized here for client integration context. Full implementation lives in the payment repo.)*

### What It Is

A self-sovereign, offline-capable payment system. Tokens backed 1:1 by USDC held by an LLC in a smart contract. LLC earns yield on the float (Tether model). No fees to users. Tokens work offline indefinitely after minting. Internet required only at deposit and redemption.

**The core insight**: Redemption capability anchors value — not the act of redemption. Like Bretton Woods: nobody goes to Fort Knox, but the gold is real. Tokens circulate as value itself. The longer they circulate without redemption, the more yield accrues to the LLC.

### Built On

- LoRa mesh radio (Meshtastic) — transport, stock firmware only
- ZK cryptography (Groth16 + Circom) — trust layer
- USDC on Ethereum — collateral layer
- Raspberry Pi + T-Beam — hardware layer
- LLC structure — legal and business layer

### Core Design Principles (Non-Negotiable)

- No phones required — Pi + T-Beam only
- No custom firmware — stock Meshtastic throughout
- No shared infrastructure — each member runs their own node
- No token expiry — valid until spent, period
- No internet after initial mint
- No fees to users — yield on float is the business model
- No gossip broadcasts — double-spend prevented by recipient binding
- The ZK proof IS the token

### Token Wire Format

```
Field                  Bytes   Notes
──────────────────────────────────────────────────
Denomination           1       e.g. $1, $5, $10, $20
Recipient commitment   32      includes pubkey binding
Groth16 proof          192     constant size, all hops
──────────────────────────────────────────────────
Total                  225     fits 237-byte LoRa limit
```

No spend_nullifier field — gossip eliminated entirely. 12 bytes freed vs prior design.

### How Tokens Work

**Minting:**
1. User deposits USDC to smart contract (internet, one-time per top-up)
2. Contract registers commitment permanently: `commitment = Poseidon(secret, 0)` — stored in `mapping(bytes32 => bool)`, never expires, never removed
3. Pi generates Groth16 proof locally (batch job, overnight)
4. Unbound intermediate tokens stored as files on Pi SD card

**Spending (Point of Sale):**
1. Sender gets recipient's pubkey (from heartbeat / node directory)
2. Sender's Pi generates BOUND transfer proof (~5-20s phone, ~2-8min Pi 3B): proves valid commitment + denomination + bound to recipient pubkey P
3. Sender transmits 225-byte bound token over mesh (1 LoRa packet)
4. Recipient's Pi verifies Groth16 proof locally (<50ms): checks proof valid + binding matches own pubkey
5. Done — no internet, no gossip, no network coordination

**Re-spending (P2P Transfer):**
- Recursive proof wrapping: wraps prior proof_N-1 + binds to new recipient pubkey
- Proof size stays CONSTANT regardless of transfer depth
- Proving time stays CONSTANT per hop

**Redemption:**
1. Token holder goes online when convenient (no urgency)
2. Submits token bytes + private redeem_nullifier to Ethereum
3. Contract verifies proof, checks nullifier not previously submitted
4. Contract pays USDC to specified address

### Double-Spend Security Model

**Why gossip fails**: nodes can be offline, travel between communities, target different mesh segments. Gossip is probabilistic; attacks are deterministic.

**Recipient binding closes the window**: Token proof asserts THREE things simultaneously:
1. Valid commitment exists in Ethereum registry
2. Denomination correctly bound
3. Token redeemable ONLY by holder of privkey for pubkey P

Attack is impossible regardless of whether any node is online, whether gossip reaches anyone, or whether attacker visits multiple communities. The math enforces recipient exclusivity.

**Security layers:**
- Layer 1: Recipient binding (cryptographic, offline, instant)
- Layer 2: Ethereum contract nullifier check (authoritative, at redemption)

### ZK Cryptography

**Proof system: Groth16** (decided, closed)
- Smallest proof size (~192 bytes) — fits LoRa packet
- Fastest verification (<50ms on Pi)
- Tooling: Circom, snarkjs, arkworks
- Halo2 rejected — 1-3KB proof breaks single-packet constraint

**Commitment registry: simple mapping, not Merkle tree** — critical design decision. Merkle trees require proofs against a specific root; roots change as new deposits arrive, causing tokens proved against old roots to expire. A currency cannot have expiring tokens.

```solidity
mapping(bytes32 => bool) public commitments;  // permanent, never expire
```

**Single nullifier** — stays private until redemption. Never broadcast, never transmitted. Submitted to Ethereum only at redemption.

### Circom Circuit

```
Single spend (base circuit):
  1. I know secret S such that Poseidon(S, 0) exists in commitment registry
  2. denomination is correctly bound to this commitment
  3. recipient_commitment = Poseidon(recipient_pubkey, new_secret, 0)
  4. redeem_nullifier = Poseidon(S, 1)  [private, submitted at redemption only]

Re-spend (recursive extension):
  5. prior proof_N-1 is valid [recursive wrapping]
  6. new recipient binding for next hop
```

### Smart Contract (Solidity)

```solidity
contract MeshCash {
    IERC20 public usdc;
    mapping(bytes32 => bool) public commitments;  // permanent
    mapping(bytes32 => bool) public nullifiers;   // spent tokens
    address public owner;
    bool public paused;

    function deposit(uint256 amount, bytes32 commitment) external notPaused;
    function redeem(bytes calldata zkProof, bytes32 redeemNullifier,
                    address recipient, uint256 denomination) external notPaused;
    function batchRedeem(bytes[] calldata zkProofs, bytes32[] calldata redeemNullifiers,
                         address recipient, uint256[] calldata denominations) external notPaused;
    function pause() external onlyOwner;
    function unpause() external onlyOwner;
    function freezeCommitment(bytes32 c) external onlyOwner;
}
```

USDC held in contract deposited to yield protocol (Aave, Compound). Yield accrues to LLC wallet. Users always redeem exact face value.

### Hardware Stack

Per-member node ($89-109 total):
- Raspberry Pi 3B ($35) or Pi 4 ($55) — compute, wallet, daemon
- T-Beam ESP32 ($31) — LoRa radio modem, stock firmware only
- SD Card 32GB ($8)
- Power supply + case ($15)

Performance:
- Proof verification: <50ms (Pi 3B), <20ms (Pi 4)
- Bound proof generation: 2-8 min (Pi 3B), 30-90s (Pi 4)
- Batch mint (overnight): 2-8 min/token (Pi 3B)

Pi 3B: fine for verification-only merchant nodes, overnight batch minting.
Pi 4: recommended for point-of-sale bound proof generation.

### Software Components (Payment Repo)

1. **ZK Circuit** (Circom) | Difficulty 7/10 | Risk CRITICAL — reference: Tornado Cash Nova. Requires professional audit before mainnet.
2. **Ethereum Smart Contract** (Solidity) | Difficulty 4/10 | Risk HIGH — Groth16 verifier auto-generated by Circom, deployed as separate immutable contract.
3. **Shared Rust Crypto Library** | Difficulty 5/10 | Risk MEDIUM — Groth16 verify, proof generation, nullifier derivation, commitment generation, recipient binding, token serialization. Single implementation, all nodes link via FFI.
4. **Pi Node Daemon** (Python + Rust FFI) | Difficulty 4/10 — Meshtastic serial handler, token wallet (SQLite), batch proof scheduler, Ethereum client.
5. **Meshtastic Transport** | Difficulty 3/10 — Port 256: bound token messages (225 bytes). Port 257 REMOVED (nullifier gossip eliminated). All token transfers unicast.

### Meshtastic Port Map

- Port 256: bound token messages (225 bytes, unicast)
- ~~Port 257~~: nullifier gossip — ELIMINATED in v2.0

### Build Sequence (Payment Repo)

**Stage 1 — Cryptographic Foundation (3-4 months)**
- ZK circuit: base circuit with recipient binding (no recursion yet)
- Smart contract on testnet: commitment registry + recipient-bound redemption
- Rust crypto library: verify + generate + binding
- Milestone: Alice mints, generates bound proof for Bob, Bob redeems on testnet

**Stage 2 — Hardware Integration (2-3 months)**
- Pi node daemon + Meshtastic transport
- Overnight batch mint test, point-of-sale bound proof generation test
- Milestone: Alice's Pi generates bound token, transmits to Bob's Pi, Bob's Pi verifies, Bob redeems to testnet USDC

**Stage 3 — Recursive Re-spend (3-4 months)**
- Upgrade circuit for recursive proof wrapping with recipient binding
- Milestone: 10-hop transfer, proof stays 225 bytes, final holder redeems

**Stage 4 — Hardening (3-6 months)**
- Professional ZK circuit audit — NON-NEGOTIABLE
- Smart contract audit — NON-NEGOTIABLE
- Bug bounty program
- Mainnet with $10 maximum token value cap
- Milestone: $10,000 total float, real community transacting

**Stage 5 — Distribution**
- Packaged Pi OS image, one-command setup, hardware guide

### Legal & Business

- Entity: LLC (jurisdiction TBD — legal counsel required before any real funds)
- Model: Tether model — earn yield on USDC float, no fees to users
- KYC: delegated upstream — any address holding USDC was already KYC'd at acquisition (Circle, Coinbase, Kraken, Gemini)
- Regulatory: FinCEN MSB registration + AML program almost certainly required; state money transmitter licenses vary; SEC analysis needed. Legal counsel required before taking real user funds.

### Risk Summary

**EXISTENTIAL:**
- ZK circuit bug → forge tokens / bypass recipient binding → mitigate: professional audit, value cap at launch
- Smart contract exploit → drains USDC vault → mitigate: audit, OpenZeppelin, pause function

**SERIOUS:**
- Trusted setup compromise → forged proofs possible → mitigate: air-gapped key generation, published verification key
- Regulatory action → mitigate: LLC structure, legal counsel first

**MANAGEABLE:**
- Proving key loss (not secret, just large — backup)
- Pi hardware failure (SQLite + token files → external backup)
- Recipient pubkey unavailable → mitigate: pubkeys cached from heartbeats

### What This Is Not

- Not a general-purpose blockchain
- Not a decentralized protocol (LLC is the issuer)
- Not trustless (trust LLC for collateral; trust math for tokens)
- Not gossip-dependent (double-spend prevented by recipient binding)
- Not anonymous at deposit (Ethereum address visible on-chain)
- Not real-time global settlement (Ethereum settlement at merchant convenience)

Closest analogs: Cashu (online only), Fedimint (online only), Tether (internet required), Zcash (internet required, no binding). This combines Zcash bearer note model + Tether LLC issuer model + recipient binding for offline double-spend prevention, running on a $31 radio with no internet. That combination does not currently exist in deployable form.
