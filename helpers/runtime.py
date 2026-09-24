"""Runtime probing and vendored-CLI management for the argus plugin.

Called by hooks.py (framework runtime). Probes never raise — a hosted A0
environment missing node/npm must not break plugin load; tools surface the
cached gap as a friendly preflight error instead.
"""

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Optional, TypedDict

PLUGIN_DIR = Path(__file__).resolve().parents[1]
PROBE_CACHE = PLUGIN_DIR / "helpers" / "probe-cache.json"
VENDOR_DIR = PLUGIN_DIR / "node_modules"
VENDORED_BIN = VENDOR_DIR / ".bin" / "argus-reviewer"
ARGUS_PACKAGE = "argus-reviewer-e2e"
MIN_NODE = (20, 19)

_PROBE_TIMEOUT_S = 3
_VENDOR_TIMEOUT_S = 180


class ProbeResult(TypedDict):
    """Capability matrix written by probe()/install_dependencies() and read
    back by tools via read_probe_cache(). Writer and readers must use these
    exact keys — see tools/argus_flow.py."""

    node: bool
    node_version: Optional[str]
    node_ok: bool
    npm: bool
    npx: bool
    git: bool
    playwright_ok: bool
    playwright_note: Optional[str]
    vendored: bool
    vendor_error: Optional[str]


_PROBE_DEFAULTS: ProbeResult = {
    "node": False,
    "node_version": None,
    "node_ok": False,
    "npm": False,
    "npx": False,
    "git": False,
    "playwright_ok": False,
    "playwright_note": None,
    "vendored": False,
    "vendor_error": None,
}


def _run(cmd, timeout=_PROBE_TIMEOUT_S, env=None):
    try:
        p = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
            stdin=subprocess.DEVNULL,
            env=env,
        )
        return p.returncode, (p.stdout or "").strip(), (p.stderr or "").strip()
    except (OSError, subprocess.TimeoutExpired):
        return None, "", ""


def _node_version():
    code, out, _ = _run(["node", "--version"])
    if code != 0 or not out.startswith("v"):
        return None
    try:
        major, minor, *_ = out[1:].split(".")
        return int(major), int(minor)
    except ValueError:
        return None


def _playwright_probe():
    """Check the Playwright browser cache. Returns (ok, note) where the note
    names the cache location found, or what was checked when missing."""
    home = Path.home()
    for rel in (".cache/ms-playwright", "Library/Caches/ms-playwright"):
        d = home / rel
        if d.is_dir() and any(d.iterdir()):
            return True, f"browser cache present at {d}"
    return (
        False,
        f"no browser cache under {home / '.cache/ms-playwright'} "
        "or ~/Library/Caches/ms-playwright",
    )


def probe(argus_version_pin=""):
    """Capability matrix — never raises."""
    result: ProbeResult = {
        "node": False,
        "node_version": None,
        "node_ok": False,
        "npm": False,
        "npx": False,
        "git": False,
        "playwright_ok": False,
        "playwright_note": None,
        "vendored": False,
        "vendor_error": None,
    }
    v = _node_version()
    if v is not None:
        result["node"] = True
        result["node_version"] = f"{v[0]}.{v[1]}"
        result["node_ok"] = v >= MIN_NODE
    result["npm"] = _run(["npm", "--version"])[0] == 0
    result["npx"] = _run(["npx", "--version"])[0] == 0
    result["git"] = _run(["git", "--version"])[0] == 0
    ok, note = _playwright_probe()
    result["playwright_ok"] = ok
    result["playwright_note"] = note

    if result["node_ok"] and result["npm"]:
        ok, err = _vendor(argus_version_pin)
        result["vendored"] = ok
        if not ok:
            result["vendor_error"] = err
    elif not result["node"]:
        result["vendor_error"] = "node not found"
    elif not result["node_ok"]:
        result["vendor_error"] = f"node {result['node_version']} < {MIN_NODE[0]}.{MIN_NODE[1]}"
    elif not result["npm"]:
        result["vendor_error"] = "npm not found"
    return result


def _vendor(pin):
    """Install the argus CLI into the plugin dir. Returns (ok, error)."""
    spec = f"{ARGUS_PACKAGE}@{pin}" if pin else ARGUS_PACKAGE
    pkg_json = PLUGIN_DIR / "package.json"
    if not pkg_json.exists():
        pkg_json.write_text(
            json.dumps(
                {"name": "a0-plugin-argus-vendored", "private": True, "version": "0.0.0"}
            )
            + "\n"
        )
    env = {
        "PATH": os.environ.get("PATH", ""),
        "HOME": os.environ.get("HOME", ""),
        "TMPDIR": os.environ.get("TMPDIR", ""),
        "npm_config_loglevel": "error",
    }
    code, _out, err = _run(
        ["npm", "install", "--prefix", str(PLUGIN_DIR), spec],
        timeout=_VENDOR_TIMEOUT_S,
        env=env,
    )
    if code != 0:
        return False, (err or f"npm exited {code}").strip()[:300]
    if not VENDORED_BIN.exists():
        return False, "npm install succeeded but node_modules/.bin/argus-reviewer is missing"
    return True, None


def read_probe_cache():
    """Read the cached ProbeResult. Caches written before the shared schema
    used a bare "playwright" bool — map it to "playwright_ok" so old caches
    still validate."""
    try:
        raw = json.loads(PROBE_CACHE.read_text())
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(raw, dict):
        return None
    data = dict(_PROBE_DEFAULTS)
    data.update({key: raw[key] for key in _PROBE_DEFAULTS if key in raw})
    if "playwright_ok" not in raw and "playwright" in raw:
        data["playwright_ok"] = bool(raw["playwright"])
    return data


def write_probe_cache(data):
    try:
        PROBE_CACHE.write_text(json.dumps(data, indent=2) + "\n")
    except OSError:
        pass


def install_dependencies(argus_version_pin=""):
    """Probe the environment and vendor the CLI. Always succeeds — failures
    are recorded in the probe cache for tools to narrate."""
    result = probe(argus_version_pin)
    write_probe_cache(result)
    return result


def remove_owned_dependencies():
    """Uninstall: remove only plugin-owned artifacts."""
    for p in (PROBE_CACHE, PLUGIN_DIR / "package.json", PLUGIN_DIR / "package-lock.json"):
        try:
            p.unlink()
        except OSError:
            pass
    shutil.rmtree(VENDOR_DIR, ignore_errors=True)


if __name__ == "__main__":
    print(json.dumps(install_dependencies(sys.argv[1] if len(sys.argv) > 1 else ""), indent=2))
