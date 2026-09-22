#!/usr/bin/env python3
"""a0_pr_watch — poll GitHub for PR pushes and trigger argus_review on a
hosted Agent Zero instance via /api/api_message.

Runs as a single-shot poll (cron/systemd timer). For each configured repo it
lists open PRs, diffs head SHAs against a state file, and for every
opened/synchronize (new head SHA) posts a message asking A0 to run the
argus_review tool. One A0 chat context is reused across all triggers.

State file + config live outside the repo. No secrets in argv — the A0 API
key is read from the A0 settings file (or env), never logged.
"""

import argparse
import base64
import fcntl
import hashlib
import json
import os
import subprocess
import sys
import urllib.request
from pathlib import Path

DEFAULT_CONFIG = {
    "repos": [],
    "a0_url": "http://localhost:5000",
    "a0_env": "/home/khan/agent-zero/usr/.env",
    "state_file": "~/.local/state/a0-pr-watch/state.json",
    "post": True,
    "skip_authors": [],
    "max_triggers_per_run": 8,
    "project_name": "",
    "lifetime_hours": 24,
}


def load_config(path):
    cfg = dict(DEFAULT_CONFIG)
    cfg.update(json.loads(Path(path).read_text()))
    return cfg


def api_key(cfg):
    """Derive A0's API token the same way A0 does (helpers/settings.py
    create_auth_token): sha256(runtime_id:auth_login:auth_password), first 16
    chars of urlsafe-b64. runtime_id + auth creds live in the A0 usr .env —
    self-heals across restarts since the id is persisted there."""
    if key := os.environ.get("A0_API_KEY"):
        return key
    env = {}
    for line in Path(cfg["a0_env"]).read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, _, v = line.partition("=")
            env[k.strip()] = v.strip().strip('"').strip("'")
    runtime_id = env.get("A0_PERSISTENT_RUNTIME_ID", "")
    login = env.get("AUTH_LOGIN", "")
    password = env.get("AUTH_PASSWORD", "")
    digest = hashlib.sha256(f"{runtime_id}:{login}:{password}".encode()).digest()
    return base64.urlsafe_b64encode(digest).decode().replace("=", "")[:16]


def load_state(path):
    try:
        return json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return {"prs": {}, "context_id": ""}


def open_prs(repo):
    out = subprocess.run(
        ["gh", "pr", "list", "--repo", repo, "--state", "open",
         "--json", "number,headRefOid,title,author", "--limit", "50"],
        capture_output=True, text=True, timeout=60,
    )
    if out.returncode != 0:
        print(f"warn: gh pr list {repo} failed: {out.stderr.strip()[:200]}",
              file=sys.stderr)
        return []
    return json.loads(out.stdout)


def post_message(cfg, key, message, context_id):
    body = {
        "message": message,
        "lifetime_hours": cfg["lifetime_hours"],
    }
    if context_id:
        body["context_id"] = context_id
    if cfg.get("project_name"):
        body["project_name"] = cfg["project_name"]
    req = urllib.request.Request(
        f"{cfg['a0_url'].rstrip('/')}/api/api_message",
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json", "X-API-KEY": key},
        method="POST",
    )
    # api_message is synchronous — it returns the agent's reply, so the POST
    # can block for the length of a review. Generous timeout; the lock file
    # prevents a slow review from stacking up cron runs.
    with urllib.request.urlopen(req, timeout=cfg.get("a0_timeout_s", 1500)) as resp:
        return json.loads(resp.read())


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default="~/.config/a0-pr-watch/config.json")
    ap.add_argument("--seed", action="store_true",
                    help="record current head SHAs without triggering")
    args = ap.parse_args()

    cfg = load_config(os.path.expanduser(args.config))
    state_path = Path(os.path.expanduser(cfg["state_file"]))
    state_path.parent.mkdir(parents=True, exist_ok=True)

    # Single instance — a slow A0 review must not overlap the next cron tick
    # (SHA dedup is written at exit; concurrent runs would double-trigger).
    lock_fd = os.open(state_path.parent / "watch.lock", os.O_CREAT | os.O_WRONLY)
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        print("another run in progress — skipping")
        return

    state = load_state(state_path)
    key = api_key(cfg)

    triggered = 0
    for repo in cfg["repos"]:
        name = repo if isinstance(repo, str) else repo["name"]
        post = cfg["post"] if isinstance(repo, str) else repo.get("post", cfg["post"])
        for pr in open_prs(name):
            pr_key = f"{name}#{pr['number']}"
            head = pr["headRefOid"]
            if state["prs"].get(pr_key) == head:
                continue
            # Record skipped authors so re-pushes don't re-evaluate forever;
            # they simply never trigger.
            if pr["author"]["login"] in cfg["skip_authors"]:
                state["prs"][pr_key] = head
                continue
            if args.seed:
                state["prs"][pr_key] = head
                continue
            if triggered >= cfg["max_triggers_per_run"]:
                continue  # not recorded — retried next poll
            sha8 = head[:8]
            msg = (
                f"PR push detected: {pr_key} — \"{pr['title']}\" "
                f"(head {sha8}, author {pr['author']['login']}). "
                f"Use the argus_review tool with pr=\"{pr_key}\""
                + (", post=\"true\"" if post else "")
                + " and report the verdict."
            )
            try:
                resp = post_message(cfg, key, msg, state["context_id"])
                state["prs"][pr_key] = head  # only after a successful trigger
                if isinstance(resp, dict) and resp.get("context_id"):
                    state["context_id"] = resp["context_id"]
                triggered += 1
                print(f"triggered: {pr_key} @ {sha8}")
            except Exception as e:
                print(f"error posting {pr_key}: {e}", file=sys.stderr)

    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(json.dumps(state, indent=2) + "\n")
    if not triggered:
        print("no new PR heads")


if __name__ == "__main__":
    main()
