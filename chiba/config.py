from dataclasses import dataclass, field
from pathlib import Path
import yaml


@dataclass
class ExternalPlugin:
    """Config entry for an exec-based plugin (command → shell program)."""
    cmd: str = ""
    description: str = ""
    exec_cmd: str = ""     # shell command string; query appended as final arg
    timeout: int = 8       # seconds before the subprocess is killed
    max_chars: int = 200   # reply truncated to this length (fits a mesh DM)


@dataclass
class MQTTConfig:
    broker: str = "localhost"
    port: int = 1883
    topic_rx: str = "msh/test/rx"
    topic_tx: str = "msh/test/tx"


@dataclass
class BLEConfig:
    enabled: bool = False
    device_name: str = ""     # device name or BLE MAC; empty = auto-scan
    adapter: str = ""         # HCI adapter, e.g. "hci0"; empty = system default


@dataclass
class MeshConfig:
    cooldown_seconds: int = 15
    heartbeat_interval_seconds: int = 86400
    heartbeat_jitter_seconds: int = 600
    reply_timeout_seconds: int = 30


@dataclass
class NLPConfig:
    embeddings_path: str = "embeddings/intent_matrix.npz"


@dataclass
class PaymentsConfig:
    enabled: bool = False
    wallet_db: str = "wallet.db"
    mesh_cash_path: str = ""        # absolute path to mesh-cash repo root
    rate_limit_per_min: int = 4     # max token verifications per sender per minute
    # Ethereum / Base — needed for on-chain deposit and redeem
    rpc_url: str = ""               # e.g. https://base-sepolia-rpc.publicnode.com
    contract_address: str = ""      # MeshCash contract address
    usdc_address: str = ""          # USDC token address
    private_key: str = ""           # hex key; falls back to ETH_PRIVATE_KEY env var
    chain_id: int = 84532           # 84532 = Base Sepolia, 8453 = Base mainnet


@dataclass
class Config:
    node_id: str = "!000000"
    node_handle: str = "chiba"
    mqtt: MQTTConfig = field(default_factory=MQTTConfig)
    ble: BLEConfig = field(default_factory=BLEConfig)
    mesh: MeshConfig = field(default_factory=MeshConfig)
    nlp: NLPConfig = field(default_factory=NLPConfig)
    payments: PaymentsConfig = field(default_factory=PaymentsConfig)
    plugins: list = field(default_factory=list)   # list[ExternalPlugin]
    db_path: str = "chiba.db"


def save_config(config: Config, path: str = "config.yaml"):
    # Read existing file first so we preserve keys we don't manage
    for src in [path, "config.example.yaml"]:
        if Path(src).exists():
            with open(src) as f:
                data = yaml.safe_load(f) or {}
            break
    else:
        data = {}

    data["node_id"] = config.node_id
    data.setdefault("mqtt", {})
    data["mqtt"]["broker"] = config.mqtt.broker
    data["mqtt"]["port"] = config.mqtt.port
    data["mqtt"]["topic_rx"] = config.mqtt.topic_rx
    data["mqtt"]["topic_tx"] = config.mqtt.topic_tx
    data.setdefault("ble", {})
    data["ble"]["enabled"] = config.ble.enabled
    data["ble"]["device_name"] = config.ble.device_name
    data["ble"]["adapter"] = config.ble.adapter
    data.setdefault("payments", {})
    data["payments"]["enabled"] = config.payments.enabled
    data["payments"]["rpc_url"] = config.payments.rpc_url
    data["payments"]["contract_address"] = config.payments.contract_address
    data["payments"]["usdc_address"] = config.payments.usdc_address
    data["payments"]["chain_id"] = config.payments.chain_id
    # private_key intentionally not persisted here — use ETH_PRIVATE_KEY env var

    with open(path, "w") as f:
        yaml.dump(data, f, default_flow_style=False, sort_keys=False)


def load_config(path: str = "config.yaml") -> Config:
    for candidate in [path, "config.example.yaml"]:
        if Path(candidate).exists():
            with open(candidate) as f:
                data = yaml.safe_load(f) or {}
            break
    else:
        return Config()

    mqtt = MQTTConfig(**data.get("mqtt", {}))
    bd = data.get("ble", {})
    ble = BLEConfig(
        enabled=bd.get("enabled", False),
        device_name=bd.get("device_name", ""),
        adapter=bd.get("adapter", ""),
    )
    mesh = MeshConfig(**data.get("mesh", {}))
    nlp_d = data.get("nlp", {})
    nlp = NLPConfig(embeddings_path=nlp_d.get("embeddings_path", "embeddings/intent_matrix.npz"))

    pd = data.get("payments", {})
    payments = PaymentsConfig(
        enabled=pd.get("enabled", False),
        wallet_db=pd.get("wallet_db", "wallet.db"),
        mesh_cash_path=pd.get("mesh_cash_path", ""),
        rate_limit_per_min=pd.get("rate_limit_per_min", 4),
        rpc_url=pd.get("rpc_url", ""),
        contract_address=pd.get("contract_address", ""),
        usdc_address=pd.get("usdc_address", ""),
        private_key=pd.get("private_key", ""),
        chain_id=pd.get("chain_id", 84532),
    )

    plugin_cfgs = []
    for p in data.get("plugins", []):
        if not isinstance(p, dict) or not p.get("exec") or not p.get("cmd"):
            continue
        plugin_cfgs.append(ExternalPlugin(
            cmd=str(p["cmd"]),
            description=str(p.get("description", "")),
            exec_cmd=str(p["exec"]),
            timeout=int(p.get("timeout", 8)),
            max_chars=int(p.get("max_chars", 200)),
        ))

    return Config(
        node_id=data.get("node_id", "!000000"),
        node_handle=data.get("node_handle", "chiba"),
        mqtt=mqtt,
        ble=ble,
        mesh=mesh,
        nlp=nlp,
        payments=payments,
        plugins=plugin_cfgs,
        db_path=data.get("db", {}).get("path", "chiba.db"),
    )
