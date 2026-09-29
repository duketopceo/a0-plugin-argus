"""Shared machinery for the argus plugin tools.

Everything the two Tool classes need that isn't A0-specific: PR
normalization, settings merge, the allowlist child-env builder, the
subprocess runner (process-group kill on every exit path), review-cwd
preparation (scratch / git-archive copy / trusted real checkout), report
parsers, and the GitHub post lanes (sticky-comment upsert + the serialized
batched-review poster).

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
# Executable config extensions only — .json is pure data and may safely load.
_EXEC_CONFIG_GLOBS = tuple(
    f"{base}.{ext}"
    for base in ("argus-reviewer.config", "vision-e2e.config")
    for ext in ("ts", "js", "mjs", "cjs", "mts", "cts")
)
# git always reads <checkout>/.git/config — a hostile repo can arm
# core.hooksPath/credential helpers there. Command-line -c overrides local.
_GIT_SAFE_FLAGS = ("-c", "core.hooksPath=", "-c", "core.fsmonitor=false",
                   "-c", "credential.helper=")


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
    """Resolve a secret-bearing NAME to its value (never logged).
    Process env first, then the A0 secrets store (usr/secrets.env + .env)."""
    name = str(settings.get(key) or "").strip() or default_env
    value = os.environ.get(name)
    if value:
        return value, name
    try:
        from helpers.secrets import get_secrets_manager  # type: ignore

        value = get_secrets_manager().load_secrets().get(name)
    except Exception:
        value = None
    return value, name


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


def checkout_matches_repo(checkout, owner, repo):
    """True when checkout's origin remote resolves to owner/repo (case-insensitive)."""
    origin = _repo_from_origin(checkout)
    return origin is not None and (origin[0].lower(), origin[1].lower()) == (
        owner.lower(),
        repo.lower(),
    )


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
            ["git", *_GIT_SAFE_FLAGS, "-C", str(cwd), *args],
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
            ["git", *_GIT_SAFE_FLAGS, "-C", str(path), "archive",
             "--format=tar", "HEAD"],
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
    for glob in _EXEC_CONFIG_GLOBS:
        for cfg in Path(scratch).glob(glob):
            try:
                cfg.unlink()
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
    """Disambiguate 404/401/403 before spend. Returns the PR payload dict on
    success (post_review consumes its coordinates) or raises ArgusError."""
    status, body, _h = _gh("GET", f"{GH_API}/repos/{owner}/{repo}/pulls/{number}", token)
    if status == 200:
        return body if isinstance(body, dict) else None
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
            "Issues + Pull requests read+write (classic: repo/public_repo scope)"
        )
    if status == 404:
        raise ArgusError(f"can't comment on {owner}/{repo} (404) — repo or PR not visible to the token")
    raise ArgusError(f"GitHub comment failed ({status}) on {owner}/{repo}")


# --------------------------------------------------------------------------
# Review lane (KTD3 — the report carries a pre-rendered surface; this poster
# renders nothing. It freshness-gates the report, dedups on the serialized
# key, validates anchors against the live diff, dismisses stale self-reviews,
# and POSTs one batched review on a bounded retry ladder. Ported from
# action/sticky-comment.cjs — keep the two in lockstep.)
# --------------------------------------------------------------------------

_HUNK_RIGHT = re.compile(r"@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@")
_SUGGESTION_FENCE = re.compile(r"\r?\n(`{4,})suggestion\r?\n([\s\S]*?)\r?\n\1")
_DISMISS_MESSAGE = "Superseded by a newer argus-reviewer review."


def _short_hash(s):
    """djb2 → 8 hex chars over UTF-16 code units — matches shortHash() in
    argus src/cli.ts byte-for-byte (JS strings index by code unit, so astral
    chars hash as surrogate pairs). Dedup identity only, not a boundary."""
    h = 5381
    data = s.encode("utf-16-le", "surrogatepass")
    for i in range(0, len(data), 2):
        h = ((h << 5) + h + (data[i] | (data[i + 1] << 8))) & 0xFFFFFFFF
    return f"{h:08x}"


def _extract_suggestion(body):
    """Pull the fenced ```suggestion``` block out of a posted comment body.
    The CLI's fence is longest-backtick-run+1 (min 4), so a run of exactly the
    fence's length can only appear as the closing fence — the backreference
    is safe against interior ``` runs."""
    m = _SUGGESTION_FENCE.search(body or "")
    return m.group(2) if m else ""


def _posted_dedup_key(comment):
    """Reconstruct the serialized dedupKey for an already-posted review
    comment: `path:line:bodyFirstLine:hash8(suggestion|'')` — identical to the
    key the CLI serialized, so a corrected suggestion re-posts instead of
    colliding."""
    body = str(comment.get("body") or "")
    first = body.split("\n", 1)[0]
    return (
        f"{comment.get('path')}:{comment.get('line')}:{first}:"
        f"{_short_hash(_extract_suggestion(body))}"
    )


def _list_all(url, token):
    """Every page of a GitHub list endpoint (100/page). -> list, or None on
    non-200/non-list so callers degrade rather than post blind."""
    out = []
    page = 1
    while True:
        status, body, _h = _gh("GET", f"{url}?per_page=100&page={page}", token)
        if status != 200 or not isinstance(body, list):
            return None
        out.extend(body)
        if len(body) < 100:
            return out
        page += 1


def list_review_comments(owner, repo, number, token):
    return _list_all(f"{GH_API}/repos/{owner}/{repo}/pulls/{number}/comments", token)


def list_pr_files(owner, repo, number, token):
    return _list_all(f"{GH_API}/repos/{owner}/{repo}/pulls/{number}/files", token)


def _right_side_lines(patch):
    """RIGHT-side line numbers covered by a unified-diff patch. Every line in
    a hunk's `+c,d` range is a valid anchor (context or added); `-` lines
    aren't counted in `d`, so the range is contiguous."""
    lines = set()
    for m in _HUNK_RIGHT.finditer(patch):
        start = int(m.group(1))
        count = int(m.group(2)) if m.group(2) is not None else 1
        lines.update(range(start, start + count))
    return lines


def _is_on_diff(comment, diff_lines):
    """True when a serialized comment anchors inside the live diff — path in
    the file list and `line` (plus `start_line` when present) on a RIGHT-side
    hunk line. Files without a `patch` (large/binary) accept no comments."""
    if not isinstance(comment, dict):
        return False
    valid = diff_lines.get(comment.get("path"))
    if valid is None or comment.get("line") not in valid:
        return False
    start = comment.get("start_line")
    return start is None or start in valid


def _review_body(report, note=None):
    """Verdict line + honest blocker counts (reproduced and p-gated are never
    lumped). Always non-empty — REQUEST_CHANGES requires a body. Carries the
    sentinel so stale-review dismissal can self-identify."""
    parts = [f"verdict **{report.get('verdict') or 'unknown'}**"]
    proven = report.get("provenBlockers") or 0
    high = report.get("highConfidenceBlockers") or 0
    if proven:
        parts.append(f"⛔ {proven} reproduced blocker(s)")
    if high:
        parts.append(f"◎ {high} high-confidence blocker(s)")
    body = f"{SENTINEL}\n**argus-reviewer** — {' · '.join(parts)}"
    if note:
        body += f"\n\n*{note}*"
    return body


def _token_login(token):
    """The login this token posts as — self-identification for stale-review
    dismissal (KTD5). None when the token can't resolve /user."""
    status, body, _h = _gh("GET", f"{GH_API}/user", token)
    if status == 200 and isinstance(body, dict):
        login = body.get("login")
        if isinstance(login, str) and login:
            return login
    return None


def _pr_coords(pr):
    """-> (owner, repo, number) from a pulls payload, or None."""
    if not isinstance(pr, dict):
        return None
    base = (pr.get("base") or {}).get("repo") or {}
    owner = pr.get("owner") or (base.get("owner") or {}).get("login")
    repo = pr.get("repo") or base.get("name")
    try:
        number = int(pr.get("number"))
    except (TypeError, ValueError):
        return None
    if not owner or not repo:
        return None
    return str(owner), str(repo), number


def post_review(report, pr, token):
    """Post the serialized review surface as one batched PR review.

    Thin consumer — the CLI already eligibility-filtered, sanitized,
    severity-sorted, capped, and keyed `reviewComments[]`; this does only the
    post-time work: freshness (R9), dedup (R10), live-diff validation (R8),
    stale self-review dismissal (KTD5), one POST with the serialized event,
    and the bounded retry ladder (R4/KTD4, at most three POSTs).

    -> None when the report has no serialized surface (a pre-0.3.0 report —
    sticky-only by design), else a dict narrated by narrate_review():
    `{status: 'posted'|'skipped'|'failed', ...}`. Never raises for API
    failures — the sticky has already posted and a review failure must not
    fail the tool.
    """
    if not isinstance(report, dict) or report.get("skipped"):
        return None
    serialized = report.get("reviewComments")
    if not isinstance(serialized, list):
        return None
    coords = _pr_coords(pr)
    if coords is None:
        return {
            "status": "skipped",
            "reason": "PR coordinates unavailable — head SHA unverifiable",
        }
    owner, repo, number = coords
    base_url = f"{GH_API}/repos/{owner}/{repo}/pulls/{number}"

    # R9/KTD6 — re-resolve the head at post time (the preflight payload is
    # stale by the length of the review). A planted or stale report must
    # never produce committable suggestions or a blocking review.
    status, fresh_pr, _h = _gh("GET", base_url, token)
    head_sha = ((fresh_pr or {}).get("head") or {}).get("sha") if status == 200 else None
    binding = report.get("headBinding")
    intended = binding.get("intendedSha") if isinstance(binding, dict) else None
    if not head_sha or intended != head_sha:
        return {
            "status": "skipped",
            "reason": f"report head binding ({intended or 'missing'}) does not match "
                      f"PR head ({head_sha or 'unresolved'})",
        }

    event = "REQUEST_CHANGES" if report.get("reviewEvent") == "request_changes" else "COMMENT"
    overflow = report.get("commentsOverflow")
    meta = {
        "event": event,
        "deduped": 0,
        "dropped": 0,
        "overflow": overflow if isinstance(overflow, int) else 0,
        "dismissed": 0,
        "warnings": [],
    }

    # R10 — paginate fully and scope to the current head so comments on older
    # commits can't suppress still-valid findings. Keys are reconstructed from
    # the posted body, so a corrected suggestion re-posts instead of colliding.
    existing = list_review_comments(owner, repo, number, token)
    if existing is None:
        return {"status": "skipped",
                "reason": "couldn't list existing review comments — review not posted"}
    posted_keys = {
        _posted_dedup_key(c)
        for c in existing
        if isinstance(c, dict)
        and c.get("commit_id") == head_sha
        and str(c.get("body") or "").startswith("**argus-reviewer")
    }
    fresh = [
        c for c in serialized
        if isinstance(c, dict) and c.get("dedupKey") not in posted_keys
    ]
    meta["deduped"] = len(serialized) - len(fresh)

    # R8 — the diff is authoritative only at post time; drop anchors that
    # aren't RIGHT-side lines in the current PR diff.
    files = list_pr_files(owner, repo, number, token)
    if files is None:
        return {"status": "skipped",
                "reason": "couldn't list PR files for diff validation — review not posted"}
    diff_lines = {}
    for f in files:
        if isinstance(f, dict) and isinstance(f.get("patch"), str):
            diff_lines[f.get("filename")] = _right_side_lines(f["patch"])
    comments = [c for c in fresh if _is_on_diff(c, diff_lines)]
    meta["dropped"] = len(fresh) - len(comments)

    # KTD5 — dismiss stale self reviews so a fixed PR is never left gated by
    # an obsolete REQUEST_CHANGES. Self = authored by the token's login AND
    # (empty body or sentinel) — a foreign review posted under the same token
    # is never dismissed. When /user can't resolve the login, dismissal is
    # skipped outright: we can't tell our reviews from the Action's or a
    # human's, and dismissing a stranger's gate is the wrong failure.
    reviews = _list_all(f"{base_url}/reviews", token)
    login = _token_login(token)
    if reviews is None or login is None:
        meta["warnings"].append("couldn't enumerate prior reviews — stale dismissal skipped")
    else:
        self_logins = {login, f"{login}[bot]"}
        for r in reviews:
            if not isinstance(r, dict):
                continue
            rbody = r.get("body")
            stale = (
                r.get("state") in ("CHANGES_REQUESTED", "PENDING")
                and (r.get("user") or {}).get("login") in self_logins
                and (not isinstance(rbody, str) or rbody == "" or SENTINEL in rbody)
            )
            if not stale:
                continue
            d_status, _p, _h = _gh(
                "PUT",
                f"{base_url}/reviews/{r.get('id')}/dismissals",
                token,
                {"message": _DISMISS_MESSAGE},
            )
            if d_status in (200, 201):
                meta["dismissed"] += 1
            else:
                meta["warnings"].append(
                    f"failed to dismiss stale review {r.get('id')} ({d_status})"
                )

    # A COMMENT review with nothing to say posts nothing — the sticky already
    # carries the verdict. REQUEST_CHANGES posts even with zero comments: the
    # gate intent must land.
    if event != "REQUEST_CHANGES" and not comments:
        return {"status": "skipped", "reason": "no new inline comments", **meta}

    # KTD4 — (1) post the serialized event; (2) on 403/422 (own-PR,
    # permissions) retry COMMENT with a downgrade note in the body; (3) on a
    # comment-caused 422, drop anchors failing diff membership and retry.
    # dedupKey is poster-local — the API gets path/line/side/body
    # (+start_line/start_side) verbatim.
    note = None
    last_status = None
    for attempt in range(3):
        status, resp, _h = _gh(
            "POST",
            f"{base_url}/reviews",
            token,
            {
                "commit_id": head_sha,
                "event": event,
                "body": _review_body(report, note),
                "comments": [
                    {k: v for k, v in c.items() if k != "dedupKey"} for c in comments
                ],
            },
        )
        if status in (200, 201):
            return {
                "status": "posted",
                "url": (resp or {}).get("html_url", "") if isinstance(resp, dict) else "",
                "comments": len(comments),
                "note": note,
                **meta,
                "event": event,
            }
        last_status = status
        if attempt == 0 and event == "REQUEST_CHANGES" and status in (403, 422):
            event = "COMMENT"
            note = f"REQUEST_CHANGES downgraded to COMMENT — {status}"
            continue
        if status == 422 and comments:
            kept = [c for c in comments if _is_on_diff(c, diff_lines)]
            if len(kept) < len(comments):
                meta["dropped"] += len(comments) - len(kept)
                comments = kept
                continue
        break
    reason = f"review post failed ({last_status})"
    if last_status in (401, 403):
        reason = (
            f"can't post a review on {owner}/{repo} ({last_status}) — the comment "
            "token needs Pull requests read+write (classic: repo scope); it can "
            "post blocking reviews"
        )
    return {"status": "failed", "reason": reason, **meta}


def _review_line(result):
    """One narration line for a post_review() result dict."""
    status = result.get("status")
    if status == "posted":
        bits = [f"{result.get('comments', 0)} inline comment(s)"]
        if result.get("deduped"):
            bits.append(f"{result['deduped']} already posted")
        if result.get("dropped"):
            bits.append(f"{result['dropped']} off-diff dropped")
        if result.get("overflow"):
            bits.append(f"{result['overflow']} over cap")
        if result.get("dismissed"):
            bits.append(f"{result['dismissed']} stale review(s) dismissed")
        line = f"review posted ({result.get('event', 'COMMENT')}): " + " · ".join(bits)
        if result.get("note"):
            line += f" — {result['note']}"
    elif status == "skipped":
        line = f"review skipped: {result.get('reason') or 'no reason recorded'}"
    else:
        line = f"review post failed: {result.get('reason') or 'unknown'}"
    for w in result.get("warnings") or []:
        line += f" (warning: {w})"
    return line


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


def narrate_review(report, report_dir, posted=None, cwd_note="", review=None):
    """Compact narration block per KTD5 — fields-if-present. `review` is the
    post_review() result dict (None when the report had no review surface)."""
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
    if review:
        lines.append(_review_line(review))
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
