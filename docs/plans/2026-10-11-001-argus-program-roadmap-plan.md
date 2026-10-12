---
title: "a0-plugin-argus program roadmap"
type: program-roadmap
status: proposed
date: 2026-10-11
origin: docs/research/2026-10-11-landscape.md
---

# a0-plugin-argus program roadmap

> Written by hand in the shape of the sibling program plans (a0-plugin-notion, a0-plugin-jev-compact). `compound-engineering:ce-plan` was not run: the scope is docs plus a small settings-panel restyle, and the multi-phase flow is too heavy for a repo this size. Claims were checked against the code on 2026-10-11.

## Summary

Keep the plugin standalone and security-first. Work ahead: catch up with the engine (pin), tell the user in the panel whether it can run, and shrink the one very large helper. No new surface area until those land.

## Now / Next / Later

| ID | Unit | Horizon | Difficulty |
|---|---|---|---|
| U1 | Re-verify fixtures and bump `argus_version_pin` to 0.5.x | Now | medium |
| U2 | Land PR #12 (docs corrections) and rebase the README of this PR on it | Now | easy |
| U3 | Settings panel in the Patchbay look (done in this PR) | Now | easy |
| U4 | `argus_status` token-free API plus a status chip in the panel | Next | medium |
| U5 | Split `helpers/argus.py` | Next | medium |
| U6 | Plugin Hub listing (`a0-plugins#572`) | Next | easy |
| U7 | Local-diff review mode (no PR needed) | Later | hard |
| U8 | Replace `contrib/pr-watch` polling with the Argus GitHub Action | Later | medium |

## Units

### U1. Re-verify fixtures and bump the pin
- Status: Now, not started. Evidence: `argus_version_pin: "0.3.1"`; `npm view argus-reviewer-e2e version` returns 0.5.0.
- Dependencies: none. Verification: regenerate `tests/fixtures/code-review*.json` from a real 0.5.0 run (billed through the `orch` wrapper and the `orchestral-eval-v3` key), then `pytest` green.
- **Difficulty**: medium. The report schema is asserted in `parse_review_report` and `post_review`; a field change breaks posting silently.
- **Feasibility**: needs one paid review run (cents). Could be blocked if 0.4 or 0.5 changed `reviewComments[]`.
- **Simpler alternative**: leave the pin and document that the default has no built-in cap (PR #12 already does). Costs nothing, keeps users on old behaviour.

### U2. Land PR #12 and rebase
- Status: Now. Evidence: PR #12 is open, touches `README.md` and `docs/HANDOFF.md`. This PR copies its README hunks verbatim so git sees identical changes.
- Dependencies: owner review. Verification: `git merge` of both in either order is clean.
- **Difficulty**: easy. **Feasibility**: none blocking. **Simpler alternative**: none needed.

### U3. Settings panel restyle
- Status: Now, done here. Evidence: `webui/config.html` commits `ui: ...` in this PR.
- Verification: before/after screenshots in the PR; tests unchanged.
- **Difficulty**: easy. **Feasibility**: the panel renders inside Agent Zero and uses its CSS variables; tokens fall back to Patchbay values when a variable is missing. Not verified inside a live a0.
- **Simpler alternative**: none; it is a style block.

### U4. `argus_status` API and status chip
- Status: Next. Evidence: `hooks.py` already writes a probe cache (`helpers/runtime.py`), but nothing reads it for the UI.
- Dependencies: U3. Verification: new test in `tests/test_helpers.py`; chip shows LIVE / IDLE / FAILED per `DESIGN.md`.
- **Difficulty**: medium. A new `api/` route plus Alpine fetch code in the panel.
- **Feasibility**: must never return token values; return booleans only (same rule as the notion plugin's `notion_status`).
- **Simpler alternative**: print the probe-cache summary as static text in the panel; no API.

### U5. Split `helpers/argus.py`
- Status: Next. Evidence: 1,127 lines; `post_review` alone is lines 796-977.
- Dependencies: none, but do it after U1 so fixtures settle. Verification: all 87 tests pass unchanged except import paths.
- **Difficulty**: medium (imports use the a0-qualified path; `tests/conftest.py` synthesises the package).
- **Feasibility**: AGENTS.md says `post_review` ports `action/sticky-comment.cjs` and must stay in lockstep; splitting must not reorder logic.
- **Simpler alternative**: extract only `post_review` and its three helpers into `helpers/posting.py` (about 250 lines) and leave the rest.

### U6. Plugin Hub listing
- Status: Next. Evidence: `docs/HANDOFF.md` records `agent0ai/a0-plugins#572` as open; the staged card is in `a0-openrouter-spend/index/argus`. Add a Patchbay thumbnail (square, 20 KB or less) from `assets/brand/logo.png` when resubmitting.
- **Difficulty**: easy. **Feasibility**: maintainer review time; PRs failing CI for 7+ days may be closed ([a0-plugins](https://github.com/agent0ai/a0-plugins)).
- **Simpler alternative**: document the git-clone install only (already in `docs/ONBOARDING.md`).

### U7. Local-diff review mode
- Status: Later. Evidence: README "v0.1 limits": review needs a real github.com PR.
- **Difficulty**: hard. Needs engine support and a new trust path for uncommitted files.
- **Feasibility**: depends on Argus; the plugin cannot do it alone.
- **Simpler alternative**: push a draft PR and review that.

### U8. Replace pr-watch polling
- Status: Later. Evidence: `contrib/pr-watch/a0_pr_watch.py` (256 lines) polls every 2 minutes because the a0 host is tailnet-only.
- **Difficulty**: medium. **Feasibility**: a hosted Action cannot reach the tailnet; only valid if the review runs in the Action itself, which makes the plugin redundant for that path.
- **Simpler alternative**: delete `contrib/pr-watch` and tell users to use the Argus Action directly; keep the plugin for interactive review.

## Verdict

Standalone, no merge, no retirement. The largest cut available is U8 (about 260 lines plus a second install story).
