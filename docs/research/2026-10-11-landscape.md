# Landscape: a0-plugin-argus (2026-10-11)

Question: who else puts an AI pull-request reviewer behind an agent tool, and what is unique here? Code read first (`helpers/argus.py`, `tools/`, `webui/config.html`, `hooks.py`); web claims are cited and weak sources are flagged.

## What this repo is

An Agent Zero plugin with two tools, `argus_review` and `argus_flow`, that shell out to a vendored `argus-reviewer-e2e` CLI (pinned to 0.3.1 in `default_config.yaml`; npm latest is 0.5.0 as of 2026-10-11, checked with `npm view`). It adds a sticky PR comment, one batched inline review with committable suggestions, a trust gate for running checkout code, and a polling watcher (`contrib/pr-watch`). About 1,900 lines, 87 offline tests.

## Comparable projects

| Project | Model | Better than this plugin | Worse than this plugin |
|---|---|---|---|
| [PR-Agent (Qodo)](https://github.com/qodo-ai/pr-agent) | MIT, CLI / GitHub Action / Docker / webhook; BYO keys via LiteLLM; `/review`, `/improve`, `/describe`, `/ask` each one LLM call | Mature, many git hosts and models, many commands | No browser flow replay; no evidence-linked or "proven blocker" gating that I could find in the page |
| [CodeRabbit](https://docs.coderabbit.ai/) | Hosted product; docs list context-aware reviews, bug detection, one-click fixes, IDE and CLI | Polished, learns from team feedback, linters bundled (per a [comparison blog](https://aicodereview.cc/blog/qodo-vs-coderabbit); vendor-adjacent, treat as unverified) | Self-hosting is enterprise only per the same blog; not an agent tool |
| [Qodo](https://dev.to/rahulxsingh/qodo-vs-coderabbit-ai-code-review-tools-compared-2026-kdp) | Hosted product on top of PR-Agent; test generation | Test generation; benchmarks (vendor-side, unverified) | Per-seat pricing; single-tenant only on enterprise tiers |
| [crito](https://github.com/nitingupta220/crito) | GitHub Action using an OpenRouter key; never executes PR code | Zero hosting | No agent-tool surface, no replay |
| [Vercel open-review bot](https://pub.towardsai.net/vercel-open-sourced-their-internal-ai-code-review-bot-5a613bc07e8c) | Self-hosted review bot (member-only story, details not read) | Deploys in one click | Not an Agent Zero plugin |
| [Argus](https://github.com/duketopceo/Argus) (the engine) | MIT CLI and Action, BYO OpenRouter key, runs the real app in a browser, evidence linkage, default $1 per-run budget | n/a | n/a |

## What users expect

- Comments appear on the PR, not only in a chat (PR-Agent: "run as a comment on a PR or from the CLI").
- Keys stay under the user's control (PR-Agent, crito, Argus all say so).
- A visible cost per review. Argus reports spend; this plugin narrates it after the fact unless `review_budget_usd` is set.

## What is unique here

1. It is the only one of the above that is an Agent Zero tool: the review lands in the agent's conversation as narrated findings, and the agent can then act on them.
2. It inherits Argus's evidence linkage and "request changes only for proven blockers" posting rule, and adds a stale-review dismissal that only touches its own reviews.
3. A real trust model for a host that holds tokens: sanitised `git archive` copy by default, `trust_checkout` gate, process-group kill on timeout.

## Gaps and risks

- Version drift: pinned 0.3.1 vs 0.5.0 on npm. The budget default ($1) first shipped in 0.4.2 (PR #12 text), so the default pin has no built-in cap.
- `helpers/argus.py` is 1,127 lines holding settings, subprocess, GitHub client, rendering and posting.
- No settings-panel feedback: nothing in the UI says whether node, the CLI or the token are present.
- Plugin Hub listing (`agent0ai/a0-plugins#572`, per `docs/HANDOFF.md`) is still pending.
