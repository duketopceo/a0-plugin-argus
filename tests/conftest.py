"""Make `usr.plugins.argus.*` imports resolvable under pytest — the same
qualified path the A0 runtime uses inside usr/plugins/argus/ — and stub the
framework's `helpers.tool` module so tools/*.py import outside A0."""

import sys
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _pkg(name, path=None):
    mod = types.ModuleType(name)
    mod.__path__ = [str(path)] if path else []
    return mod


_usr = _pkg("usr")
_plugins = _pkg("usr.plugins")
_argus = _pkg("usr.plugins.argus", ROOT)
_usr.plugins = _plugins
_plugins.argus = _argus
sys.modules.setdefault("usr", _usr)
sys.modules.setdefault("usr.plugins", _plugins)
sys.modules["usr.plugins.argus"] = _argus


# --- minimal A0 framework stub ------------------------------------------------

class Response:
    def __init__(self, message="", break_loop=False, additional=None, **kw):
        self.message = message
        self.break_loop = break_loop
        self.additional = additional or {}


class Tool:
    def __init__(self, agent=None, name="", method=None, args=None,
                 message="", loop_data=None, **kw):
        self.agent = agent
        self.name = name
        self.method = method
        self.args = dict(args or {})
        self.message = message
        self.loop_data = loop_data
        self.progress = ""

    def add_progress(self, content):
        if content:
            self.progress += str(content)

    async def execute(self, **kwargs):
        raise NotImplementedError


_helpers = _pkg("helpers")
_tool = types.ModuleType("helpers.tool")
_tool.Tool = Tool
_tool.Response = Response
_helpers.tool = _tool
sys.modules.setdefault("helpers", _helpers)
sys.modules["helpers.tool"] = _tool


class FakeAgent:
    """What the tools touch on `self.agent`."""

    def __init__(self, abort=False):
        self._abort = abort
        self.intervention = None

    async def handle_intervention(self):
        if self._abort:
            raise RuntimeError("intervention")
