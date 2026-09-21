# Handoff — current state

Updated 2026-09-21 on omarchy-max. Everything below is recoverable from
git history + GitHub; this doc is the map.

## Where things stand — all work is pushed

- **This repo** (`duketopceo/a0-plugin-argus`): v0.1 complete, `main` is the
  latest. U1–U6 built, U7 verified live on the hosted Agent Zero instance.
  49/49 tests green.
- **Plan doc**: merged to Argus main via PR #71
  (`docs/plans/2026-09-18-2143-feat-a0-plugin-v01-plan.md`).
- **Index submission**:
  [agent0ai/a0-plugins PR #572](https://github.com/agent0ai/a0-plugins/pull/572) —
  CI `validate` green, awaiting maintainer review. Adds `plugins/argus/index.yaml`
  pointing here. Repo fork lives at `duketopceo/a0-plugins` (re-clone if needed).

## Argus repo state (duketopceo/Argus)

- `main` carries the full Jev stack: trust gate (#64), Jev-everywhere (#70),
  plugin plan (#71), oss-readiness + Node 22/24 CI matrix (#63).
- Superseded stack PRs #67/#68 closed; stale branches pruned local + remote.
- Issues closed: #58 (config trust — fixed), #52 (sandbox lane — shipped 0.1.3).
- Open: #62 (OpenCodeReview patterns), #53 (Argus→A0 delegate lane — inverse
  of the plugin, partially verified), #23 (GHES; GitLab excluded), #22 (spend
  alerts), #21 (health dashboard). #69 is an intentional draft demo PR.
- **v0.2.0 release PR #79 merged; tag `v0.2.0` pushed.** The release workflow's
  `publish` job is waiting on the `npm` environment's required-reviewer
  approval — that's Luke. Once it publishes, this plugin's
  `argus_version_pin` should bump `0.1.3` → `0.2.0` to pick up Jev fields
  (`triage`, `secretsScan`, adjudication records) in narration — the
  if-present code path is already in place.

## Kurultai (duketopceo/kurultai)

- `OPENROUTER_API_KEY` repo secret is now set (2026-09-21) — #353's blocker
  resolved; `argus-reviewer` check passes on the PR.
- PR #353 merge conflict resolved (INDEX.md union merge) and pushed.
  **Blocked on required code-owner review — Luke must approve.**
- Post-merge follow-up: `package-lock.json` pins `github:duketopceo/Argus#ca2e0ae`
  (mid-Jev-stack). Once v0.2.0 publishes to npm, re-pin the shim to the npm
  release instead of a git SHA.

## Verified state on the hosted A0 (server-001)

- Plugin installed at `/home/khan/agent-zero/usr/plugins/argus` on `server-001`
  (bind-mounted to `/a0/usr/plugins/argus` in the `agent-zero` container).
- `config.json` there (untracked, intentional):
  `{"github_token_env": "PERSONAL_GITHUB_PAT", "openrouter_key_env": "API_KEY_OPENROUTER"}`
  — both resolve via A0's secrets store, not container env.
- Live-verified: real review on Argus#71 narrated + `post:true` upserts the
  Argus Action's own sticky comment (sentinel sharing works).
- Playwright absent in that container → `argus_flow` ships behind its trust +
  runtime gates (README documents `npx playwright install chromium`).
- After v0.2.0 publishes + pin bump: `git -C /home/khan/agent-zero/usr/plugins/argus pull`
  then re-run `hooks.py::install` with `/opt/venv-a0/bin/python` to re-vendor.

## To pick up

```bash
cd ~/Documents/github/personal/a0-plugin-argus && git pull
uv run --with pytest python -m pytest tests/ -q   # 49 green
```
