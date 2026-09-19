"""Lifecycle hooks for the argus plugin.

Runs inside the A0 framework runtime. install() probes the environment and
vendors the argus CLI into the plugin directory; it never raises — a hosted
environment without node/npm records the gap in the probe cache so the tools
can surface a friendly preflight error instead of breaking plugin load.
Re-running install (e.g. after an update or once node becomes available)
retries vendoring.
"""

from usr.plugins.argus.helpers import runtime


def _pin():
    try:
        from helpers.plugins import get_plugin_config

        cfg = get_plugin_config("argus") or {}
        pin = cfg.get("argus_version_pin")
        return str(pin).strip() if pin else ""
    except Exception:
        return ""


def install():
    runtime.install_dependencies(argus_version_pin=_pin())


def pre_update():
    # No plugin-owned processes; vendoring is re-run by install() after update.
    pass


def uninstall():
    runtime.remove_owned_dependencies()
