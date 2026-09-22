# Onboarding — zero to automatic PR reviews

This is the full path for someone (or some machine) standing up a hosted
Agent Zero that reviews PRs on push. ~15 minutes once you have Docker.

## 1. Start Agent Zero

Any A0 works — desktop app, VPS, container. Container (the server-001 pattern):

```bash
docker run -d --name agent-zero \
  -p 5000:80 \
  -v /srv/agent-zero/usr:/a0/usr \
  agent0ai/agent-zero:latest
```

Everything user-owned lives in the `usr/` bind mount — plugins, settings,
secrets, projects. Back up that directory and the install is reproducible.

## 2. Add credentials

In the A0 UI: **Settings → API keys / External services**, or drop them in
`usr/.env` / `usr/secrets.env` (the plugin reads A0's secrets store, so any
of these work — values never go in `config.json`):

| Needed for | Key |
|---|---|
| Model calls (review + flow) | `OPENROUTER_API_KEY` — one BYOK key covers all models |
| PR preflight + sticky comments | A GitHub PAT with `repo` scope — any name, e.g. `PERSONAL_GITHUB_PAT` |

Set the A0 model to whatever you want reviews narrated by — Argus calls
OpenRouter directly with its own model choice; the A0 chat model only needs
to be good enough to invoke tools and summarize.

## 3. Install this plugin

Until the index PR lands, install is a clone into the plugins dir:

```bash
git clone https://github.com/duketopceo/a0-plugin-argus /srv/agent-zero/usr/plugins/argus
docker restart agent-zero   # loads tools + prompt fragments
```

The `install` hook probes the host (node/npm/git) and vendors
`argus-reviewer-e2e` into the plugin dir. Missing pieces are recorded in
`probe-cache.json` and narrated as friendly preflight errors — the plugin
still loads either way.

**If the container lacks node/npm**: `argus_review` needs them on the A0
host PATH. Docker users can `docker exec agent-zero apt-get install -y nodejs npm`
(bake it into an image for permanence) or run A0 where node already exists.

## 4. Point the plugin at your secret names

`usr/plugins/argus/config.json`:

```json
{
  "github_token_env": "PERSONAL_GITHUB_PAT",
  "openrouter_key_env": "OPENROUTER_API_KEY"
}
```

(These are env *names*, not values.) Optional knobs — `trust_checkout`,
`flow_budget_usd`, `argus_version_pin` — see the README settings table.

## 5. Verify

In the A0 chat: `review duketopceo/kurultai#363` — the agent calls
`argus_review`, streams progress, narrates verdict/cost/findings. Add
"post it" or the tool call's `post:true` to upsert the `<!-- argus-reviewer -->`
sticky on the PR.

## 6. Automatic reviews on PR push (optional)

`contrib/pr-watch/` is a poller that runs *wherever can reach both GitHub
(`gh` authed) and the A0 API* — usually the A0 host itself:

```bash
cp contrib/pr-watch/config.example.json ~/.config/a0-pr-watch/config.json
# edit `repos` — strings for global post:true, objects for per-repo control
python3 contrib/pr-watch/a0_pr_watch.py --seed   # baseline existing PRs
# cron every 2 min:
(crontab -l; echo '*/2 * * * * /usr/bin/python3 /path/to/a0_pr_watch.py >> ~/.local/state/a0-pr-watch/watch.log 2>&1') | crontab -
```

It diffs head SHAs (pushes trigger; comments don't), posts `api_message` to
A0, and keeps all reviews in one chat. The API key is derived from A0's own
`usr/.env` — no secret handling. Details: `contrib/pr-watch/README.md`.

Why polling, not webhooks: a tailnet/VPN-only A0 can't be reached by GitHub.
If your A0 is publicly reachable (with its auth on), a repo webhook or a
GitHub Actions step POSTing `api_message` gives instant triggers instead.

## Recommended companion plugins (from the a0-plugins index)

| Plugin | Why it pairs with Argus |
|---|---|
| `a0_playwright_cli` | Auto-installs Playwright + Chromium — unblocks `argus_flow` (our flow lane needs it; `argus_review` doesn't) |
| `a0_worktree` | Gives the agent an isolated git worktree — the safe-checkout complement to `trust_checkout` |
| `a0_openrouter` | Same BYOK key also buys image/audio/embedding tools — one key, more surface |
| `a0_swarm` | If you want parallel sub-agents reviewing different PRs at once |

## Current limits (honest ones)

- Reviews run from the PR diff + GitHub API unless you pass a `checkout`
  path — evidence linkage is `inconclusive` without local source
- `argus_flow` needs `trust_checkout: true`, a real checkout, an explicit
  `--url`, and Playwright installed
- Fork-PR reviews on a *self-hosted* A0 inherit whatever token you give it —
  the sticky comment posts as that identity
