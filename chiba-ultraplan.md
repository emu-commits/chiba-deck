# Chiba Deck — MVP Build Plan (revised v2)

## Context

The repo is empty of code. CLAUDE.md has the full architectural spec. The client is a **bidirectional mesh service layer**: it discovers remote services via heartbeat and proxies user requests to them, AND it can host local service plug-ins that are announced in its own heartbeat and respond to inbound `?cmd` requests from other mesh nodes. Payments are a first-class native feature (ZK phase later).

---

## Key Architectural Principle

```
INBOUND DISCOVERY (remote services)
    other node heartbeat: ">: ?wiki ?market | wiki bot v2"
    → registry registers "wiki" → node "!abc123" (proxy)
    → user types "?wiki jaguar" → client DMs !abc123
    → !abc123 replies → displayed in stream

OUTBOUND SERVICE HOSTING (local plug-ins)
    we register a local plugin: LocalPlugin(cmd="market", handler=fn)
    → heartbeat announces ">: ?market ?nodes ?help | chiba-deck"
    → other node DMs us "?market list"
    → router dispatches to local plugin handler
    → handler reply sent back as DM to requesting node

Both directions use the same ServiceRegistry and plug-in interface.
```

---

## Directory Structure

```
chiba-deck/
├── pyproject.toml
├── config.example.yaml
├── embeddings/
│   └── intent_matrix.npz       # pre-built, committed to repo — no runtime download
├── scripts/
│   └── build_embeddings.py     # dev-only: regenerate after editing intents.py
│                               # requires internet (Stanford GloVe), not run by users
└── chiba/
    ├── __init__.py
    ├── main.py                  # async entrypoint, wires all tasks
    ├── config.py                # load config.yaml / env vars
    ├── db.py                    # SQLite schema + typed query helpers
    ├── events.py                # Event dataclasses + asyncio EventQueue
    ├── transport.py             # MQTTTransport (paho) + BLETransport stub
    ├── heartbeat.py             # self-broadcast: includes all local plugin cmds
    ├── registry.py              # ServiceRegistry: local plugins + remote proxies
    ├── router.py                # dispatch: inbound ?cmd → local or remote
    ├── nlp/
    │   ├── __init__.py
    │   ├── intents.py           # canonical phrase dict (human-editable)
    │   ├── classifier.py        # cosine sim intent classification
    │   └── extractor.py         # per-intent regex entity extraction
    ├── plugins/
    │   ├── __init__.py
    │   ├── base.py              # Plugin ABC: cmd, description, handle(query)->str
    │   ├── nodes_plugin.py      # built-in: ?nodes → online node list from DB
    │   ├── help_plugin.py       # built-in: ?help → registry summary
    │   ├── status_plugin.py     # built-in: ?status → wallet + node stats
    │   └── loader.py            # scan plugins/ dir, auto-register on startup
    ├── payments/
    │   ├── __init__.py
    │   └── wallet.py            # key management stub; ZK phase TBD
    └── ui/
        ├── __init__.py
        ├── app.py               # textual App: layout + input loop
        ├── status_bar.py        # node count | wallet balance | unread
        └── stream_panel.py      # scrolling RichLog event stream
```

Three built-in local plugins (`nodes`, `help`, `status`) are always registered and always announced in the heartbeat. Additional local plugins drop into `plugins/` and are auto-loaded.

---

## Component Design

### Data Flow (complete picture)

```
                    ┌─────────────────────────────┐
                    │   chiba-deck client          │
                    │                              │
  msh/xxxx/rx ──►  │  transport.py                │
                    │       │ parse → Event        │
                    │       ▼                      │
                    │  events.py EventQueue        │
                    │       │                      │
                    │  ┌────┴────────────┐         │
                    │  │  heartbeat evt  │──► registry.upsert_remote()
                    │  │  message evt    │──► router.py
                    │  └─────────────────┘         │
                    │                              │
                    │  router.py                   │
                    │  ├─ ?cmd from another node   │
                    │  │    └─ registry.local(cmd) │
                    │  │         → plugin.handle() │
                    │  │         → send_dm(reply)  │
                    │  │                           │
                    │  └─ user input (TUI)         │
                    │       ├─ local plugin cmd    │
                    │       │    → plugin.handle() │
                    │       └─ remote service cmd  │
                    │            → send_dm(node)   │
                    │            → await reply     │
                    │                              │
                    │  heartbeat.py                │
                    │  → broadcasts all local      │
                    │    plugin cmds               │
                    └─────────────────────────────┘
                              │  msh/xxxx/tx
                              ▼
                          MQTT broker → mesh
```

### `plugins/base.py` — Plugin ABC

```python
class Plugin(ABC):
    cmd: str             # "nodes", "wiki", "market", …
    description: str     # shown in ?help
    local: bool = True   # False = remote proxy stub

    @abstractmethod
    def handle(self, query: str, from_node: str | None = None) -> str:
        ...  # returns reply text (max 200 chars, mesh-safe)
```

Any file in `plugins/` that subclasses `Plugin` is auto-registered by `loader.py` on startup. Local plugins are announced in the heartbeat. The same `Plugin` interface is used to represent remote services (with `local=False`, `handle()` raises to indicate routing needed).

### `registry.py` — ServiceRegistry

```python
class ServiceRegistry:
    # local plugins (hosted here)
    def register_local(self, plugin: Plugin)
    def get_local(self, cmd: str) -> Plugin | None

    # remote services (discovered via heartbeat)
    def upsert_remote(self, node_id, caps: list[str], ts: float)
    def get_remote(self, cmd: str) -> str | None   # returns node_id

    # heartbeat payload: all local cmds
    def local_caps(self) -> list[str]

    # for ?help: both local + remote
    def describe_all(self) -> list[dict]

    # housekeeping
    def expire_offline(self, threshold_s=172800)
```

Remote services stored in `db.service_registry`. Registry rebuilt from DB on startup (persists across restarts until expiry).

### `router.py` — Bidirectional Dispatch

```
Inbound (from mesh node !xyz → us):
    "?market list" received as DM to our node_id
    → registry.get_local("market")
    → plugin.handle("list", from_node="!xyz")
    → transport.send_dm("!xyz", result)          ← rate-limited 15s/dest

Outbound (user typed in TUI):
    "?wiki jaguar"
    → NLP: intent=query_wiki, entity="jaguar"
    → registry.get_local("wiki") → None
    → registry.get_remote("wiki") → "!abc123"
    → transport.send_dm("!abc123", "?wiki jaguar")
    → pending_reply["!abc123"] set, 30s timeout
    → reply arrives as message event → display in stream

Built-ins always served locally, never proxied out:
    ?nodes, ?help, ?status → always registry.get_local()
```

### `heartbeat.py`

- Broadcasts once per day ± random jitter 0–600s (anti-flood sync)
- Payload built from `registry.local_caps()`:
  `">: ?nodes ?help ?status | chiba-deck v0.1"`
  (if user adds a market plugin: `">: ?nodes ?help ?status ?market | …"`)
- Inbound heartbeat: `registry.upsert_remote()` + `db.upsert_node()`

### `db.py` — Schema

```sql
nodes(node_id, handle, caps_json, hops, snr, last_seen)
messages(id, from_id, to_id, text, ts, direction)
service_registry(cmd, node_id, description, last_seen)   -- remote only
market_listings(id, seller_node, item, price_token, ts)
payments_in(id, from_node, amount, token, ts, status)
payments_out(id, to_node, amount, token, ts, status)
```

Key helpers: `upsert_node`, `get_nodes_online`, `register_remote_service`,
`expire_offline_services`, `insert_message`.

### `nlp/` — Intent Classification

`intents.py`: human-editable dict → canonical phrases per intent.

`embeddings/intent_matrix.npz`: **pre-built and committed to the repo**. Normal setup (`pip install -e .`) never touches GloVe. To regenerate after editing `intents.py`, run `scripts/build_embeddings.py` (requires internet, developer-only step).

`classifier.py`: at runtime loads `intent_matrix.npz` → tokenize input → avg GloVe vectors → cosine sim → (intent, confidence). No network call at any point during normal operation.

`extractor.py`: per-intent regex → entities dict.

Confidence threshold 0.65 → below: show interpretation + `[y/n]` before sending.

### `payments/wallet.py` — Stub

Stubs `?pay` / `?balance` as a local plugin so the router slot exists now. ZK repo plugs in here later without router changes.

### `ui/app.py` — Textual Layout

```
┌──────────────────────────────────────────────┐
│ StatusBar: 4 nodes online | wallet: n/a | 0  │
├──────────────────────────────────────────────┤
│                                              │
│  StreamPanel  (RichLog, events scroll in)    │
│  [10:42] !abc → you: "?wiki jaguar"          │
│  [10:42] wiki(!abc): Jaguar — big cat of … │
│                                              │
├──────────────────────────────────────────────┤
│ > _                                          │
└──────────────────────────────────────────────┘
```

`display_loop()`: `await event_queue.get()` → `render_event()` → `stream_panel.write()`

---

## Build Order

1. `pyproject.toml` + `config.example.yaml`
2. `chiba/config.py` + `chiba/db.py`
3. `chiba/events.py`
4. `chiba/transport.py`
5. `chiba/plugins/base.py` + `chiba/plugins/loader.py`
6. `chiba/registry.py`
7. `chiba/plugins/nodes_plugin.py` + `help_plugin.py` + `status_plugin.py`
8. `chiba/payments/wallet.py`
9. `chiba/nlp/intents.py` + `scripts/build_embeddings.py`
10. `chiba/nlp/classifier.py` + `chiba/nlp/extractor.py`
11. `chiba/router.py`
12. `chiba/ui/status_bar.py` + `stream_panel.py` + `app.py`
13. `chiba/heartbeat.py`
14. `chiba/main.py`

---

## Verification

```bash
pip install -e .
# embeddings/intent_matrix.npz is already committed — no download needed
# (dev only: python scripts/build_embeddings.py to regenerate after editing intents.py)

cp config.example.yaml config.yaml   # edit broker IP

mosquitto &   # local test broker
python -m chiba.main

# Built-in smoke tests (no hardware):
# ?help   → lists nodes/help/status (built-in local plugins)
# ?nodes  → "no nodes seen yet"
# ?status → "wallet: not configured | nodes: 0"

# Simulate a remote node heartbeat:
mosquitto_pub -t msh/test/rx \
  -m '{"from":"!abc","type":"text","payload":">: ?wiki ?market | wiki-bot"}'
# → stream shows node arrival, ?help now lists wiki+market from !abc

# Test inbound service request (another node asking us):
mosquitto_pub -t msh/test/rx \
  -m '{"from":"!xyz","to":"<our_id>","type":"text","payload":"?nodes"}'
# → client calls nodes_plugin.handle(), sends DM reply to !xyz

# Test outbound proxy:
# ?wiki jaguar → "no wiki service seen" (before heartbeat)
# → after heartbeat above: routes DM to !abc, 30s await, reply displayed
```
