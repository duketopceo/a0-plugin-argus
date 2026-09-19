# Handoff — machine migration (omarchy-macbook-m1 → omarchy-max)

Written 2026-09-19 by the session that built v0.1. Everything below is
recoverable from git history + GitHub; this doc is the map.

## Where things stand — all work is pushed

- **This repo** (`duketopceo/a0-plugin-argus`): v0.1 complete, `main` is the
  latest. U1–U6 built, U7 verified live on the hosted Agent Zero instance.
- **Plan doc**: `duketopceo/Argus` branch `feat/a0-plugin-v01` →
  [PR #71](https://github.com/duketopceo/Argus/pull/71)
  (`docs/plans/2026-09-18-2143-feat-a0-plugin-v01-plan.md`). Unmerged.
- **Index submission**:
  [agent0ai/a0-plugins PR #572](https://github.com/agent0ai/a0-plugins/pull/572) —
  CI `validate` green, awaiting maintainer review. Adds `plugins/argus/index.yaml`
  pointing here. Repo fork lives at `duketopceo/a0-plugins` (local clone was
  `/tmp/a0-plugins` on the old machine — re-clone if needed).

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

## Open threads

- **Jev stack is wanted, not lost.** `feat/jev-everywhere` on Argus
  (PR #70) holds `src/trust.ts` + Jev lanes. The plugin sets `ARGUS_UNTRUSTED=1`
  and narrates Jev fields if-present — it picks them up automatically when a
  Jev-bearing argus ships; bump `argus_version_pin` (currently `0.1.3`).
- Still on Luke: `OPENROUTER_API_KEY` repo secret on `duketopceo/kurultai`
  for PR #353.
- Watch: kurultai #353, Argus #68/#70/#71, index PR #572 (maintainer queue).

## To pick up on the new machine

```bash
gh repo clone duketopceo/a0-plugin-argus ~/Documents/github/personal/a0-plugin-argus
cd a0-plugin-argus && uv run --with pytest python -m pytest tests/ -q   # 49 green
```

Server-side state needs nothing from this laptop — the installed copy on
server-001 pulls straight from this repo.
