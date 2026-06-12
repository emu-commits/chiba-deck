"""Python plugin loader — for internal / advanced use only.

The standard plugin extension point is exec plugins defined in config.yaml.
This loader is not called automatically; it exists as an escape hatch for
plugins that need direct Python access to internals (e.g. test harnesses).

To use: call load_plugins() and register the results manually with the registry.
"""
import importlib
import inspect
import logging
from pathlib import Path

from .base import Plugin

log = logging.getLogger(__name__)

_BUILTIN_CMDS = {"nodes", "help", "status", "pay", "balance", "history"}


def load_plugins(plugin_dir: Path | None = None) -> list[Plugin]:
    if plugin_dir is None:
        plugin_dir = Path(__file__).parent

    plugins: list[Plugin] = []
    for path in sorted(plugin_dir.glob("*_plugin.py")):
        module_name = f"chiba.plugins.{path.stem}"
        try:
            mod = importlib.import_module(module_name)
            for _, obj in inspect.getmembers(mod, inspect.isclass):
                if (
                    issubclass(obj, Plugin)
                    and obj is not Plugin
                    and obj.cmd
                    and obj.cmd not in _BUILTIN_CMDS
                ):
                    plugins.append(obj())
        except Exception as e:
            log.warning(f"loader: skipped {path.name}: {e}")

    return plugins
