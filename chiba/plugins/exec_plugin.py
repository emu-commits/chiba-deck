import logging
import shlex
import subprocess

from .base import Plugin

log = logging.getLogger(__name__)


class ExecPlugin(Plugin):
    """Plugin backed by an external shell command.

    The query string is appended as the final positional argument.
    stdout (or stderr on failure) is returned as the reply, truncated to max_chars.

    No shell interpolation is used — the query is passed as a literal arg,
    which means there is no command-injection risk from mesh input.
    """

    mesh_visible = True

    def __init__(self, cmd: str, description: str, exec_args: list[str],
                 timeout: int = 8, max_chars: int = 200):
        self.cmd = cmd
        self.description = description
        self._exec_args = exec_args
        self._timeout = timeout
        self._max_chars = max_chars

    @classmethod
    def from_config(cls, cmd: str, description: str, exec_str: str,
                    timeout: int = 8, max_chars: int = 200) -> "ExecPlugin":
        exec_args = shlex.split(exec_str)
        if not exec_args:
            raise ValueError(f"empty exec for plugin {cmd!r}")
        return cls(cmd, description, exec_args, timeout, max_chars)

    def handle(self, query: str, from_node: str | None = None) -> str:
        args = self._exec_args + ([query] if query.strip() else [])
        try:
            result = subprocess.run(
                args,
                capture_output=True,
                text=True,
                timeout=self._timeout,
            )
            out = (result.stdout or result.stderr or "").strip()
            return out[:self._max_chars] if out else "(no output)"
        except subprocess.TimeoutExpired:
            log.warning(f"exec plugin {self.cmd!r}: timed out after {self._timeout}s")
            return f"timeout after {self._timeout}s"
        except FileNotFoundError:
            log.error(f"exec plugin {self.cmd!r}: not found: {self._exec_args[0]!r}")
            return "plugin command not found"
        except Exception as e:
            log.error(f"exec plugin {self.cmd!r}: {e}")
            return f"error: {e}"
