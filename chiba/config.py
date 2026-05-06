from dataclasses import dataclass, field
from pathlib import Path
import yaml


@dataclass
class MQTTConfig:
    broker: str = "localhost"
    port: int = 1883
    topic_rx: str = "msh/test/rx"
    topic_tx: str = "msh/test/tx"


@dataclass
class BLEConfig:
    enabled: bool = False
    device_name: str = ""


@dataclass
class MeshConfig:
    cooldown_seconds: int = 15
    heartbeat_interval_seconds: int = 86400
    heartbeat_jitter_seconds: int = 600
    reply_timeout_seconds: int = 30


@dataclass
class NLPConfig:
    confidence_threshold: float = 0.65
    embeddings_path: str = "embeddings/intent_matrix.npz"


@dataclass
class Config:
    node_id: str = "!000000"
    node_handle: str = "chiba"
    mqtt: MQTTConfig = field(default_factory=MQTTConfig)
    ble: BLEConfig = field(default_factory=BLEConfig)
    mesh: MeshConfig = field(default_factory=MeshConfig)
    nlp: NLPConfig = field(default_factory=NLPConfig)
    db_path: str = "chiba.db"
    payments_wallet_path: str = "wallet.json"


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
    ble = BLEConfig(**data.get("ble", {}))
    mesh = MeshConfig(**data.get("mesh", {}))
    nlp = NLPConfig(**data.get("nlp", {}))

    return Config(
        node_id=data.get("node_id", "!000000"),
        node_handle=data.get("node_handle", "chiba"),
        mqtt=mqtt,
        ble=ble,
        mesh=mesh,
        nlp=nlp,
        db_path=data.get("db", {}).get("path", "chiba.db"),
        payments_wallet_path=data.get("payments", {}).get("wallet_path", "wallet.json"),
    )
