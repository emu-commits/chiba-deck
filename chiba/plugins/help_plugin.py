from .base import Plugin


class HelpPlugin(Plugin):
    cmd = "help"
    description = "list available commands"
    local = True

    def __init__(self):
        self._registry = None

    def set_registry(self, registry):
        self._registry = registry

    def handle(self, query: str, from_node: str | None = None) -> str:
        if not self._registry:
            return "?say <msg> ?nodes ?help ?status | chiba-deck v0.1"
        items = self._registry.describe_all()
        local_cmds = [f"?{i['cmd']}" for i in items if i.get("local")]
        remote_cmds = [f"?{i['cmd']}(@{i.get('node_id','?')})" for i in items if not i.get("local")]
        all_cmds = ["?say <message>"] + local_cmds + remote_cmds
        return " ".join(all_cmds) + " | chiba-deck v0.1"
