# a0-plugin-argus

An [Agent Zero](https://github.com/agent0ai/agent-zero) plugin that adds two
tools backed by [Argus](https://github.com/duketopceo/Argus)
(`argus-reviewer-e2e`), an open-source, self-hosted, BYOK pull-request reviewer:

- **`argus_review`** — reviews a GitHub pull request with a code/vision model and
  narrates the verdict, findings, model, tokens, and cost into the conversation.
  Optionally posts (or updates) a single sticky comment on the PR.
- **`argus_flow`** — replays recorded Argus flows against an explicit URL inside
  a checkout you trust, and narrates pass/fail with failure messages.

Bring your own keys — the plugin never ships or stores secrets. Tokens live as
environment variables on your A0 host; the plugin only reads their names from
settings.

## Requirements

In the **A0 tool environment** (the container/host where A0 executes tools):

| Requirement | Why |
|---|---|
| `node >= 20.19.0` | The vendored argus CLI's engine floor |
| `npm` | Used once at plugin install to vendor `argus-reviewer-e2e` |
| `git` | PR context copies (`git archive`), bare-PR-number remote lookup |
| `GITHUB_TOKEN` | Read the PR + diff via the GitHub API (see scopes below) |
| `OPENROUTER_API_KEY` | BYOK model spend for the review model |
| Playwright browsers | `argus_flow` only — `npx playwright install chromium` on the host |

Install-time probes cache what was found; the plugin loads even when pieces are
missing and reports what's absent when a tool needs it.

## Installation

**New to this stack?** [`docs/ONBOARDING.md`](docs/ONBOARDING.md) is the
zero→auto-reviews walkthrough: start A0, add OpenRouter + a PAT, install
this plugin, verify, then optionally wire `contrib/pr-watch` so PR pushes
trigger reviews automatically. Recommended companion plugins listed there.

From Agent Zero's plugin management, add the repository URL:

```
https://github.com/duketopceo/a0-plugin-argus
```

At install, `hooks.py` probes `node`/`npm`/`git`/Playwright and runs
`npm install argus-reviewer-e2e` (respecting `argus_version_pin`) into the
plugin directory — the review path spawns this **vendored** binary, never a
binary found inside an untrusted checkout. If vendoring fails (no npm, no
network), the plugin still loads; `argus_review` tells you to reinstall or
enable `trust_checkout` with a checkout that has argus as a dependency.

## Credentials (host-level)

Set these as environment variables on the A0 host **or** in A0's secrets store
(Settings → Secrets, `usr/secrets.env`) — the plugin checks process env first,
then the secrets store. Not in the plugin config, not in chat. Settings hold
only the *names* of these variables, so `github_token_env: PERSONAL_GITHUB_PAT`
works when your token already lives in the secrets store.

### GitHub token — fine-grained PAT (recommended)

- **Repository access:** the repos you'll review.
- **Permissions:** `Pull requests: Read`, `Contents: Read`, and
  `Issues: Read and Write` (the last only if you'll use `post:"true"`).

### GitHub token — classic PAT (alternative)

- `public_repo` for public repos; `repo` for private. Same token covers reading
  and sticky-comment posting.

### OpenRouter key

- A spend-limited key is strongly recommended — review spend is reported after
  the fact, and in v0.1 only `argus_flow` spend is capped by a plugin setting.

If posting should use a *different* token than reading, set `comment_token_env`
to a second variable name holding an Issues-capable token.

## Settings

All settings live in the plugin config (defaults in `default_config.yaml`).
Values are env-var **names** and policy only — never secret values.

| Setting | Default | Meaning |
|---|---|---|
| `github_token_env` | `GITHUB_TOKEN` | Env var with the PR-read token (and posting fallback) |
| `comment_token_env` | `""` | Env var with the posting token; empty = use `github_token_env` |
| `openrouter_key_env` | `OPENROUTER_API_KEY` | Env var with the OpenRouter key |
| `trust_checkout` | `false` | Gates `argus_flow` and the review trust path (cwd + CLI resolution) — see Security model |
| `review_timeout_s` | `1200` | Wall-clock cap per review |
| `flow_timeout_s` | `1800` | Wall-clock cap per flow run |
| `argus_version_pin` | `"0.2.0"` | npm spec vendored at install; empty = latest |
| `flow_budget_usd` | `""` | USD cap injected as `ARGUS_BUDGET_USD` for `argus_flow` only |
| `default_checkout` | `""` | Fallback checkout path for both tools |

## Usage

Ask your agent in natural language, or call the tools directly.

**Review a PR (narrate only — the default):**

```json
{
  "tool_name": "argus_review",
  "tool_args": { "pr": "duketopceo/Argus#71" }
}
```

`pr` also accepts a full URL (`https://github.com/owner/repo/pull/123`),
`owner/repo/pull/123`, or a bare number when the checkout's `origin` is a
github.com remote. github.com only.

**Review and post a sticky comment (explicit opt-in):**

```json
{
  "tool_name": "argus_review",
  "tool_args": {
    "pr": "https://github.com/duketopceo/Argus/pull/71",
    "post": "true"
  }
}
```

Posting upserts one comment per PR — run it twice, the same comment updates.
The comment shares the `<!-- argus-reviewer -->` sentinel with the Argus GitHub
Action, so plugin-posted and CI-posted reviews never duplicate each other.

**Replay flows (requires `trust_checkout: true` + explicit url):**

```json
{
  "tool_name": "argus_flow",
  "tool_args": {
    "checkout": "/home/you/projects/myapp",
    "url": "https://staging.example.com",
    "pattern": "login"
  }
}
```

## Security model — read this before trusting a checkout

The reviewer's working directory determines how much of a PR's code can execute
next to your `GITHUB_TOKEN` and `OPENROUTER_API_KEY`.

**`argus_review` — safe by default.**
- No `checkout`: runs in an empty plugin-managed scratch dir. The PR diff comes
  from the GitHub API; nothing checkout-controlled can execute. Evidence linkage
  reports *inconclusive* (no source context).
- `checkout` passed, `trust_checkout: false`: the plugin runs
  `git archive HEAD` into a scratch dir and **deletes executable config files**
  (`argus-reviewer.config.ts`, `vision-e2e.config.ts`) before invoking the CLI.
  The reviewer sees your real tracked source (evidence works) but cannot run
  checkout-controlled code beside your keys. Untracked/uncommitted changes are
  not included.
- `checkout` + `trust_checkout: true`: runs directly in the real checkout —
  full fidelity, config code executes, checkout-local binaries are used.

**`argus_flow` — gated hard.** `run` executes the checkout's test files and
page-setup modules by design; there is no safe-copy mode. The tool **refuses
to run unless `trust_checkout: true`**. Enable it only for a repo you would
hand a terminal to.

**Subprocess hygiene.** Every argus invocation spawns a new process group,
reads from `/dev/null`, and is killed group-wide on timeout, cancellation, or
your interrupt — no orphaned `npx`/Node children. Secrets travel in an
allowlisted environment only (never argv, logs, or tool arguments), and
credential-bearing URLs are scrubbed from progress output. Git runs with
`GIT_CONFIG_NOSYSTEM=1` and `GIT_CONFIG_GLOBAL=/dev/null`.

**Public comments.** Sticky bodies carry a fixed *"automated review — verify
findings before acting"* disclaimer, and model-controlled text is sanitized
(pipes escaped, newlines flattened, cells capped) so a hostile PR can't write
arbitrary instructions into a public comment.

## Troubleshooting

- **`argus-reviewer binary not found`** — vendoring failed at install. Check
  `helpers/probe-cache.json` for what the probe saw, reinstall the plugin, or
  enable `trust_checkout` and point `checkout` at a repo with
  `argus-reviewer-e2e` in its dependencies.
- **`Node.js is not available`** — A0's tool env has no node/npm (some hosted
  deployments). `argus_review` can't run there; this is an environment limit,
  not a plugin bug.
- **403 when posting** — the comment token lacks `Issues: Read and Write`
  (fine-grained) or `repo`/`public_repo` (classic).
- **`no test files matched`** — `argus_flow` ran but the pattern/dir matched
  nothing; check `pattern` and the checkout's tests directory.
- **Flow refuses to run** — `trust_checkout` is off (correct default) or
  Playwright browsers aren't installed on the host.

## v0.1 limits

- Sticky-comment posting only (no inline review comments, no commit status).
- `argus_flow` is replay-only — recording is interactive/browser-heavy and
  deferred.
- No local-diff review mode; review needs a real github.com PR.
- GitHub.com only (no GHES/Gitea).
- Review spend is **uncapped** — cost is narrated after the fact. Only
  `argus_flow` honors `flow_budget_usd`.

## License

MIT — see [LICENSE](LICENSE).
