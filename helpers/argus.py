"""Shared machinery for the argus plugin tools.

Everything the two Tool classes need that isn't A0-specific: PR
normalization, settings merge, the allowlist child-env builder, the
subprocess runner (process-group kill on every exit path), review-cwd
preparation (scratch / git-archive copy / trusted real checkout), report
parsers, and the GitHub sticky-comment post lane.

Secrets travel via env only — never argv, never return values, never echoed
in errors.
"""

import asyncio
import io
import json
import os
import re
import signal
import subprocess
import tarfile
import tempfile
import urllib.error
import urllib.request
from pathlib import Path

PLUGIN_DIR = Path(__file__).resolve().parents[1]
VENDORED_BIN = PLUGIN_DIR / "node_modules" / ".bin" / "argus-reviewer"
DEFAULT_CONFIG = PLUGIN_DIR / "default_config.yaml"
GH_API = "https://api.github.com"
SENTINEL = "<!-- argus-reviewer -->"
MAX_FINDING_ROWS = 25
TAIL_BYTES = 4096
_EXEC_CONFIG_NAMES = ("argus-reviewer.config.ts", "vision-e2e.config.ts")


class ArgusError(Exception):
    """User-facing failure — message is shown to the agent verbatim."""


# --------------------------------------------------------------------------
# Settings
# --------------------------------------------------------------------------

def _parse_scalar(raw):
    v = raw.strip()
    if v in ("", '""', "''", "~", "null"):
        return ""
    if v in ("true", "True"):
        return True
    if v in ("false", "False"):
        return False
    try:
        return int(v)
    except ValueError:
        pass
    try:
        return float(v)
    except ValueError:
        pass
    return v.strip('"\'')


def _truthy(v):
    """A0 passes tool args as strings; settings may be bool/int."""
    if isinstance(v, str):
        return v.strip().lower() in ("1", "true", "yes", "on")
    return bool(v)


def _bounded(v, lo, hi, default):
    try:
        n = int(v)
    except (TypeError, ValueError):
        return default
    return max(lo, min(hi, n))


def load_default_config():
    """default_config.yaml is flat `key: scalar` — parse it without requiring
    PyYAML in the framework runtime."""
    try:
        import yaml  # type: ignore

        data = yaml.safe_load(DEFAULT_CONFIG.read_text())
        return data if isinstance(data, dict) else {}
    except Exception:
        out = {}
        try:
            for line in DEFAULT_CONFIG.read_text().splitlines():
                line = line.strip()
                if not line or line.startswith("#") or ":" not in line:
                    continue
                key, _, raw = line.partition(":")
                key = key.strip()
                raw = raw.split(" #", 1)[0]
                out[key] = _parse_scalar(raw)
        except OSError:
            pass
        return out


def resolve_settings(agent=None):
    """default_config.yaml merged under the runtime plugin config."""
    settings = dict(load_default_config())
    try:
        from helpers.plugins import get_plugin_config  # type: ignore

        runtime_cfg = get_plugin_config("argus", agent=agent) or {}
        settings.update({k: v for k, v in runtime_cfg.items() if v is not None})
    except Exception:
        pass
    return settings


def env_value(settings, key, default_env=""):
    """Resolve a secret-bearing env var NAME to its value (never logged)."""
    name = str(settings.get(key) or "").strip() or default_env
    return os.environ.get(name), name


# --------------------------------------------------------------------------
# PR normalization
# --------------------------------------------------------------------------

_PR_URL = re.compile(r"^https?://github\.com/([^/\s]+)/([^/\s]+)/pull/(\d+)/?$")
_PR_SLUG_PULL = re.compile(r"^([^/\s]+)/([^/\s]+)/pull/(\d+)$")
_PR_SLUG_HASH = re.compile(r"^([^/\s]+)/([^/\s]+)#(\d+)$")
_PR_NUMBER = re.compile(r"^#?(\d+)$")
_REMOTE_GH = re.compile(r"github\.com[:/]([^/\s]+)/([^/\s]+?)(?:\.git)?$")


def _repo_from_origin(checkout):
    if not checkout:
        return None
    code, out, _ = _git(checkout, ["remote", "get-url", "origin"])
    if code != 0:
        return None
    m = _REMOTE_GH.search(out.strip())
    return (m.group(1), m.group(2)) if m else None


def normalize_pr(pr, checkout=None):
    """-> (owner, repo, number). Accepts a PR URL, o/r/pull/N, o/r#N, or a
    bare number (repo derived from the checkout's origin remote)."""
    pr = (pr or "").strip()
    for rx in (_PR_URL, _PR_SLUG_PULL, _PR_SLUG_HASH):
        m = rx.match(pr)
        if m:
            return m.group(1), m.group(2), int(m.group(3))
    if re.match(r"^https?://", pr) and not pr.startswith(("https://github.com", "http://github.com")):
        raise ArgusError(
            f"unsupported PR host in '{pr}' — GitHub Enterprise is not supported in v0.1"
        )
    m = _PR_NUMBER.match(pr)
    if m:
        repo = _repo_from_origin(checkout)
        if repo is None:
            raise ArgusError(
                "bare PR number needs a checkout with a github.com origin remote "
                "(couldn't read `git remote get-url origin`); pass the full "
                "owner/repo#N form instead"
            )
        return repo[0], repo[1], int(m.group(1))
    raise ArgusError(
        f"couldn't parse PR '{pr}' — expected https://github.com/o/r/pull/N, "
        "o/r/pull/N, o/r#N, or a PR number"
    )


# --------------------------------------------------------------------------
# Child environment (allowlist, never passthrough)
# --------------------------------------------------------------------------

_ENV_ALLOWLIST = (
    "PATH",
    "HOME",
    "USER",
    "LOGNAME",
    "SHELL",
    "LANG",
    "TMPDIR",
    "TEMP",
    "TMP",
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "NO_PROXY",
    "http_proxy",
    "https_proxy",
    "no_proxy",
    "SSL_CERT_FILE",
    "SSL_CERT_DIR",
    "SYSTEMROOT",
    "SystemRoot",
)


def build_child_env(overlays):
    """Start from an allowlist of innocuous runtime vars — ambient
    ARGUS_*/GITHUB_*/GH_TOKEN/GIT_*/NODE_*/NPM_CONFIG_*/ACTIONS_* and
    unrelated secrets never reach the child — then apply explicit overlays."""
    env = {k: os.environ[k] for k in _ENV_ALLOWLIST if k in os.environ}
    env.update({k: v for k, v in os.environ.items() if k.startswith("LC_")})
    env["GIT_CONFIG_NOSYSTEM"] = "1"
    env["GIT_CONFIG_GLOBAL"] = os.devnull
    for k, v in overlays.items():
        if v is None:
            env.pop(k, None)
        else:
            env[k] = str(v)
    return env


# --------------------------------------------------------------------------
# Subprocess runner
# --------------------------------------------------------------------------

_USERINFO = re.compile(r"(https?://)[^\s/@]+@")


def scrub_line(line):
    """Mask URL userinfo so a credential-bearing remote can't leak into
    progress lines."""
    return _USERINFO.sub(r"\1***@", line)


async def run_argus(argv, cwd, env, timeout_s, on_line=None, abort_check=None):
    """Spawn the argus CLI. Returns a dict — never raises on spawn failure.
    The process group is killed in `finally` on every exit path (timeout,
    abort, exception) so a cancelled call can't orphan a spending child."""
    result = {"code": None, "tail": "", "timed_out": False, "aborted": False, "spawn_error": None}
    try:
        proc = await asyncio.create_subprocess_exec(
            *argv,
            cwd=str(cwd),
            env=env,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            start_new_session=True,
        )
    except OSError as e:
        result["spawn_error"] = f"couldn't start {argv[0]}: {e.strerror or e}"
        return result

    tail = io.StringIO()

    async def _pump():
        assert proc.stdout is not None
        async for raw in proc.stdout:
            line = scrub_line(raw.decode("utf-8", "replace")).rstrip("\n")
            tail.write(line + "\n")
            if on_line:
                try:
                    on_line(line)
                except Exception:
                    pass

    async def _watch_abort():
        while abort_check is not None and proc.returncode is None:
            r = abort_check()
            if asyncio.iscoroutine(r):
                # e.g. agent.handle_intervention() — raises InterventionException
                # on user abort; the caller re-raises after the group is killed.
                await r
            elif r:
                return
            await asyncio.sleep(1.0)

    pump = asyncio.ensure_future(_pump())
    waiter = asyncio.ensure_future(proc.wait())
    watcher = asyncio.ensure_future(_watch_abort()) if abort_check else None
    try:
        done, _pending = await asyncio.wait(
            [waiter] + ([watcher] if watcher else []),
            timeout=timeout_s,
            return_when=asyncio.FIRST_COMPLETED,
        )
        if watcher is not None and watcher in done and watcher.exception() is not None:
            raise watcher.exception()  # e.g. InterventionException — finally kills the group
        if not done:
            result["timed_out"] = True
        elif waiter not in done and proc.returncode is None:
            result["aborted"] = True
    finally:
        # Kill before reaping — on timeout/abort the group dies now, not when
        # the child would have finished on its own.
        if proc.returncode is None:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass
        await proc.wait()
        await pump
        if watcher:
            watcher.cancel()
    result["code"] = proc.returncode
    result["tail"] = tail.getvalue()[-TAIL_BYTES:]
    return result


def _git(cwd, args, timeout=15):
    try:
        p = subprocess.run(
            ["git", "-C", str(cwd), *args],
            capture_output=True,
            text=True,
            timeout=timeout,
            stdin=subprocess.DEVNULL,
            env=build_child_env({}),
        )
        return p.returncode, p.stdout or "", p.stderr or ""
    except (OSError, subprocess.TimeoutExpired):
        return 128, "", "git failed"


# --------------------------------------------------------------------------
# Review cwd (KTD3 — never run untrusted checkout code)
# --------------------------------------------------------------------------

def prepare_review_cwd(checkout, trusted):
    """-> (cwd, note). No checkout → empty scratch dir. Trusted → the real
    checkout. Untrusted checkout → git-archive copy minus executable configs."""
    if not checkout:
        return tempfile.mkdtemp(prefix="argus-review-"), "scratch dir (no checkout — evidence inconclusive)"
    path = Path(checkout)
    if not path.is_dir():
        raise ArgusError(f"checkout '{checkout}' is not a directory")
    if trusted:
        return str(path), "trusted checkout"
    code, _, err = _git(path, ["rev-parse", "--git-dir"])
    if code != 0:
        raise ArgusError(
            f"checkout '{checkout}' isn't a git repository — pass a git checkout "
            "for evidence depth, or omit checkout entirely"
        )
    scratch = tempfile.mkdtemp(prefix="argus-review-")
    try:
        p = subprocess.run(
            ["git", "-C", str(path), "archive", "--format=tar", "HEAD"],
            capture_output=True,
            timeout=60,
            stdin=subprocess.DEVNULL,
            env=build_child_env({}),
        )
    except (OSError, subprocess.TimeoutExpired) as e:
        raise ArgusError(f"`git archive` failed on '{checkout}': {e}")
    if p.returncode != 0:
        err = (p.stderr or b"").decode("utf-8", "replace").strip()[:200]
        raise ArgusError(f"`git archive` failed on '{checkout}': {err}")
    try:
        with tarfile.open(fileobj=io.BytesIO(p.stdout), mode="r:") as tf:
            tf.extractall(scratch, filter="data")
    except tarfile.TarError as e:
        raise ArgusError(f"couldn't extract git archive of '{checkout}': {e}")
    for name in _EXEC_CONFIG_NAMES:
        try:
            (Path(scratch) / name).unlink()
        except OSError:
            pass
    return scratch, f"archive copy of {checkout} (config code stripped)"


def fresh_report_dir():
    return tempfile.mkdtemp(prefix="argus-report-")


# --------------------------------------------------------------------------
# CLI resolution (KTD4 — vendored binary is the only untrusted path)
# --------------------------------------------------------------------------

def resolve_cli(checkout=None, trusted=False):
    """-> executable path or None. Untrusted: vendored bin only. Trusted:
    checkout-local bin, then vendored."""
    if trusted and checkout:
        local = Path(checkout) / "node_modules" / ".bin" / "argus-reviewer"
        if local.exists():
            return str(local)
    if VENDORED_BIN.exists():
        return str(VENDORED_BIN)
    return None


# --------------------------------------------------------------------------
# GitHub API (stdlib only)
# --------------------------------------------------------------------------

def _gh(method, url, token, body=None, timeout=30):
    req = urllib.request.Request(
        url,
        method=method,
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "a0-plugin-argus",
        },
        data=json.dumps(body).encode() if body is not None else None,
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, json.loads(resp.read() or b"null"), dict(resp.headers)
    except urllib.error.HTTPError as e:
        try:
            payload = json.loads(e.read() or b"null")
        except json.JSONDecodeError:
            payload = None
        return e.code, payload, dict(e.headers)
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        raise ArgusError(f"network error reaching api.github.com: {e}")


def preflight_pr(owner, repo, number, token):
    """Disambiguate 404/401/403 before spend. Returns None or raises ArgusError."""
    status, _body, _h = _gh("GET", f"{GH_API}/repos/{owner}/{repo}/pulls/{number}", token)
    if status == 200:
        return
    if status == 401:
        raise ArgusError("GitHub token is invalid or expired (401)")
    if status == 403:
        raise ArgusError(
            f"GitHub token lacks access to {owner}/{repo} (403) — check repo scope or rate limit"
        )
    if status == 404:
        raise ArgusError(
            f"PR {owner}/{repo}#{number} not found (404) — check the repo/PR or token visibility"
        )
    raise ArgusError(f"GitHub API returned {status} checking {owner}/{repo}#{number}")


# --------------------------------------------------------------------------
# Post lane (KTD1 — sticky sentinel upsert)
# --------------------------------------------------------------------------

def _cell(s, limit=200):
    return str(s if s is not None else "").replace("|", "\\|").replace("\r", " ").replace("\n", " ")[:limit]


def _usd(v):
    try:
        return f"${float(v):.4f}"
    except (TypeError, ValueError):
        return "$0.0000"


def render_sticky_body(report):
    """Markdown body mirroring action/sticky-comment.mjs — sanitized, capped,
    with a fixed disclaimer (model text is shaped by PR-controlled diffs)."""
    lines = [SENTINEL, ""]
    verdict = _cell(report.get("verdict") or "unknown")
    icon = {"pass": "✅", "approve": "✅", "needs_changes": "❌", "skipped": "⚪"}.get(verdict, "❔")
    if report.get("skipped"):
        lines += [f"## argus-reviewer ⚪ skipped", "", _cell(report.get("summary")), ""]
    else:
        lines += [
            f"## argus-reviewer {icon} {_cell(report.get('verdict'))}",
            "",
            _cell(report.get("summary")),
            "",
            f"**Cost:** {_usd(report.get('visionCostUsd'))} · {_cell(report.get('tokens'))}tok · {_cell(report.get('model'))}"
            + (" · ⚠️ budget exceeded" if report.get("budgetExceeded") else ""),
            "",
        ]
        findings = report.get("findings") or []
        if findings:
            lines += [
                "<details>",
                f"<summary>Findings ({len(findings)})</summary>",
                "",
                "| File | Severity | Finding |",
                "| --- | --- | --- |",
            ]
            for f in findings[:MAX_FINDING_ROWS]:
                loc = f"{_cell(f.get('file'))}:{_cell(f.get('line'))}" if f.get("line") else _cell(f.get("file"))
                lines.append(f"| `{loc}` | {_cell(f.get('severity'))} | {_cell(f.get('message'))} |")
            if len(findings) > MAX_FINDING_ROWS:
                lines.append(f"| … | — | {len(findings) - MAX_FINDING_ROWS} more findings in `code-review.json` |")
            lines += ["", "</details>", ""]
    lines += [
        "---",
        "*Automated review by [Argus](https://github.com/duketopceo/Argus) via Agent Zero — verify findings before acting.*",
    ]
    return "\n".join(lines)


def find_sticky(owner, repo, number, token):
    """Paginate ALL comment pages (the action only reads page 1)."""
    page = 1
    while True:
        status, body, _h = _gh(
            "GET",
            f"{GH_API}/repos/{owner}/{repo}/issues/{number}/comments?per_page=100&page={page}",
            token,
        )
        if status != 200 or not isinstance(body, list):
            return None
        for c in body:
            if isinstance(c, dict) and SENTINEL in str(c.get("body") or ""):
                return c.get("id")
        if len(body) < 100:
            return None
        page += 1


def post_sticky(owner, repo, number, body, token):
    """Upsert the sentinel comment. -> (comment_url, 'created'|'updated')."""
    existing = find_sticky(owner, repo, number, token)
    if existing is not None:
        status, payload, _h = _gh(
            "PATCH", f"{GH_API}/repos/{owner}/{repo}/issues/comments/{existing}", token, {"body": body}
        )
        if status == 200:
            return (payload or {}).get("html_url", ""), "updated"
        if status != 404:
            _raise_post_error(status, owner, repo)
        # PATCH-404 → fall through to POST
    status, payload, _h = _gh(
        "POST", f"{GH_API}/repos/{owner}/{repo}/issues/{number}/comments", token, {"body": body}
    )
    if status == 201:
        return (payload or {}).get("html_url", ""), "created"
    if status == 422 and len(body) > 10000:
        # oversized body — truncate findings and retry once
        body = body[:9000] + "\n\n…truncated"
        status, payload, _h = _gh(
            "POST", f"{GH_API}/repos/{owner}/{repo}/issues/{number}/comments", token, {"body": body}
        )
        if status == 201:
            return (payload or {}).get("html_url", ""), "created"
    _raise_post_error(status, owner, repo)


def _raise_post_error(status, owner, repo):
    if status in (401, 403):
        raise ArgusError(
            f"can't comment on {owner}/{repo} ({status}) — the comment token needs "
            "Issues read+write (classic: repo/public_repo scope)"
        )
    if status == 404:
        raise ArgusError(f"can't comment on {owner}/{repo} (404) — repo or PR not visible to the token")
    raise ArgusError(f"GitHub comment failed ({status}) on {owner}/{repo}")


# --------------------------------------------------------------------------
# Report parsers (KTD2/KTD5 — JSON is truth, never exit code)
# --------------------------------------------------------------------------

_SEV_ORDER = ("bug", "risk", "nit", "q")


def parse_review_report(report_dir):
    path = Path(report_dir) / "code-review.json"
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return None


def _sev_counts(findings):
    counts = {}
    for f in findings:
        sev = str(f.get("severity") or "?")
        counts[sev] = counts.get(sev, 0) + 1
    return counts


def _sort_key(f):
    sev = str(f.get("severity") or "")
    return (_SEV_ORDER.index(sev) if sev in _SEV_ORDER else len(_SEV_ORDER))


def narrate_review(report, report_dir, posted=None, cwd_note=""):
    """Compact narration block per KTD5 — fields-if-present."""
    if report is None:
        return f"argus code-review finished but wrote no report at {report_dir} — check the run output above."
    if report.get("skipped"):
        return (
            f"argus code-review **skipped**: {report.get('summary') or 'no reason recorded'}"
            + (f"\nreport: {report_dir}" if report_dir else "")
        )
    findings = report.get("findings") or []
    counts = _sev_counts(findings)
    counts_str = ", ".join(f"{k}: {counts[k]}" for k in _SEV_ORDER if counts.get(k))
    for k in sorted(counts):
        if k not in _SEV_ORDER:
            counts_str += f", {k}: {counts[k]}"
    lines = [
        f"**argus code-review — verdict: {report.get('verdict', 'unknown')}**"
        + (" ⚠️ budget exceeded" if report.get("budgetExceeded") else ""),
        f"model `{report.get('model', '?')}` · {report.get('tokens', 0)}tok · {_usd(report.get('visionCostUsd'))}",
        "",
        str(report.get("summary") or ""),
        "",
        f"**{len(findings)} finding(s)**" + (f" ({counts_str})" if counts_str else ""),
    ]
    ranked = sorted(findings, key=_sort_key)
    for f in ranked[:10]:
        loc = f"{f.get('file')}:{f.get('line')}" if f.get("line") else str(f.get("file") or "?")
        ev = f.get("evidence") or {}
        ev_str = f" · {ev.get('status')}" if ev.get("status") else ""
        extra = ""
        if f.get("category"):
            extra += f" [{f['category']}]"
        if f.get("p") is not None:
            extra += f" p={f['p']}"
        lines.append(f"- `{loc}` [{f.get('severity', '?')}]{extra} {f.get('message', '')}{ev_str}")
    if len(ranked) > 10:
        lines.append(f"- …{len(ranked) - 10} more in the report")
    # Forward-compat lanes — narrated only when the vendored schema has them.
    for key, label in (
        ("probeLaneSkipped", "probe lane skipped"),
        ("secretsScan", "secrets scan"),
        ("triage", "triage"),
    ):
        v = report.get(key)
        if isinstance(v, str) and v:
            lines.append(f"_{label}: {v}_")
        elif key == "secretsScan" and isinstance(v, dict) and v.get("skipped"):
            lines.append(f"_secrets scan skipped: {v['skipped']}_")
        elif key == "triage" and isinstance(v, dict) and v.get("mode"):
            lines.append(f"_triage: {v.get('mode')} risk {v.get('risk', '?')}/5_")
    probes = report.get("probes")
    if isinstance(probes, list) and probes:
        reproduced = sum(1 for p in probes if p.get("outcome") == "reproduced")
        lines.append(f"_probes: {len(probes)} run, {reproduced} reproduced_")
    if cwd_note:
        lines.append(f"_cwd: {cwd_note}_")
    lines.append(f"report: {report_dir}")
    if posted:
        url, how = posted
        lines.append(f"posted ({how}): {url}")
    return "\n".join(lines)


def parse_run_report(report_dir):
    path = Path(report_dir) / "run.json"
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return None


def narrate_run(report, report_dir):
    if report is None:
        return f"argus run finished but wrote no report at {report_dir} — check the run output above."
    totals = report.get("totals") or {}
    n_tests = totals.get("tests", 0)
    if n_tests == 0:
        return "argus run matched **no test files** — check the `pattern` and the checkout's tests dir."
    passed = totals.get("passed", 0)
    lines = [
        f"**argus run — {passed}/{n_tests} passed**"
        + (" ⚠️ budget exceeded" if totals.get("budgetExceeded") else ""),
        f"{totals.get('visionCalls', 0)} vision calls · {_usd(totals.get('visionCostUsd'))}"
        + (f" · {totals.get('sandboxSeconds', 0)}s sandbox" if totals.get("sandboxSeconds") else ""),
    ]
    for t in report.get("tests") or []:
        if not t.get("ok"):
            reason = t.get("failureMessage") or "failed"
            lines.append(f"- ❌ `{t.get('name', '?')}` — {reason}")
    lines.append(f"report: {report_dir}")
    return "\n".join(lines)
