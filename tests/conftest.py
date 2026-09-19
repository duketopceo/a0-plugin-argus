"""Make `usr.plugins.argus.*` imports resolvable under pytest — the same
qualified path the A0 runtime uses inside usr/plugins/argus/."""

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
