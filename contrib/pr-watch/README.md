# pr-watch — PR-push → Agent Zero `argus_review` trigger

Polls GitHub for open PRs on configured repos, detects head-SHA changes
(opened/synchronize — not comments/labels), and posts a message to a hosted
Agent Zero instance asking it to run the `argus_review` tool. All triggers
share one A0 chat context so reviews accumulate in a single conversation.

Why polling: the hosted A0 is tailnet-only — GitHub-hosted runners and
webhooks can't reach it. A 2-minute poll on the A0 host needs no inbound
networking and no new credentials (`gh` is already authed; the API key is
read from A0's own `settings.json`).

## Setup (on the A0 host)

```bash
mkdir -p ~/.config/a0-pr-watch
cp config.example.json ~/.config/a0-pr-watch/config.json
# edit repos — strings use global `post`, or objects {"name": "o/r", "post": false}

# baseline existing open PRs without triggering reviews
python3 a0_pr_watch.py --seed

# cron — every 2 minutes
(crontab -l; echo '*/2 * * * * /usr/bin/python3 /path/to/a0_pr_watch.py >> ~/.local/state/a0-pr-watch/watch.log 2>&1') | crontab -
```

## Config

| Key | Default | Meaning |
|---|---|---|
| `repos` | `[]` | `owner/repo` strings or `{"name", "post"}` objects |
| `a0_url` | `http://localhost:5000` | A0 base URL (loopback on the host) |
| `a0_settings` | `…/usr/settings.json` | Source of `mcp_server_token` (the `X-API-KEY`) |
| `state_file` | `~/.local/state/a0-pr-watch/state.json` | head-SHA dedup + shared `context_id` |
| `post` | `true` | Ask A0 to post the sticky comment |
| `skip_authors` | `[]` | Author logins that never trigger (e.g. `dependabot[bot]`); their SHAs are still recorded so re-pushes are free |
| `max_triggers_per_run` | `8` | Burst cap — overflow retries next poll (SHAs are recorded only after a successful POST) |
| `lifetime_hours` | `24` | A0 message lifetime |
