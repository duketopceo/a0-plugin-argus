# a0-plugin-argus — agent notes

An [Agent Zero](https://github.com/agent0ai/agent-zero) plugin exposing two
tools backed by the `argus-reviewer-e2e` CLI: `argus_review` and `argus_flow`.
`README.md` is the user-facing doc. This file is what an agent needs and the
README does not say.

## Commands

```bash
# Tests — 52 tests, no third-party runtime dependencies
python3.12 -m pytest tests/ -q

# Exactly what CI runs (.github/workflows/ci.yml, push to main + PR):
#   Python 3.12, `python -m pip install pytest`, `python -m pytest tests/ -q`
```

**Use Python 3.12.** On a system Python 3.14 the suite dies before collection
with `SystemError: The installed pydantic-core version ... is incompatible` —
that is the local pytest's pydantic, not this repo. A 3.12 venv is clean.

There is no linter, formatter, or type-checker configured. Do not add one as
part of an unrelated change.

## Layout

| Path | What it is |
|---|---|
| `plugin.yaml` | Plugin manifest — `name: argus`, `settings_sections: [external]`. Renaming the plugin id breaks A0's `usr/plugins/<name>` install path. |
| `hooks.py` | A0 lifecycle: `install()` probes the env and vendors the CLI, `uninstall()` removes it. Never raises — a host without node records the gap in the probe cache instead. |
| `tools/argus_review.py`, `tools/argus_flow.py` | The two A0 tools. They `from helpers.tool import ...`; the framework supplies that. |
| `helpers/argus.py` | CLI invocation, scratch-dir and `git archive` copy handling, comment sanitising. |
| `helpers/runtime.py` | Install-time probe cache and npm vendoring. |
| `prompts/agent.system.tool.argus_*.md` | Tool prompts. |
| `contrib/pr-watch/` | Optional watcher that triggers reviews on PR push. Separate entry point, not loaded by the plugin. |
| `tests/fakebin/fake_cli.py` | Stands in for the vendored CLI so tests need no node, no network, no keys. |

## Conventions that will bite you

- **Imports use the A0-qualified path.** Code and tests import
  `usr.plugins.argus.helpers...`, not `helpers.argus`.
  `tests/conftest.py` synthesises the `usr.plugins.argus` package pointing at
  the repo root and stubs the framework's `helpers.tool`. Keep that shape: a
  plain `from helpers import argus` resolves differently at runtime than it
  does under the test harness.
- **`default_config.yaml` holds env-var *names* and policy, never values.**
  Adding a real key there is a credential leak.
- **`argus_version_pin: "0.2.0"` is deliberate.** Report-schema parsing is
  asserted against that version. Bump it in its own commit with the test
  change, not incidentally.
- **Tests must stay offline.** No network, no API key, no node. Extend
  `tests/fakebin/fake_cli.py` instead of invoking the real CLI.

## Security invariants — do not relax

- `argus_flow` executes the target checkout's test files. It **must** refuse to
  run unless `trust_checkout: true`. There is no safe-copy mode.
- `argus_review` without `trust_checkout` runs `git archive HEAD` into a
  scratch dir and **deletes** executable config (`argus-reviewer.config.ts`,
  `vision-e2e.config.ts`) before invoking the CLI.
- Secrets move through an allowlisted environment only, never argv, logs, or
  tool arguments. `GIT_CONFIG_NOSYSTEM=1` and `GIT_CONFIG_GLOBAL=/dev/null`
  are set for every git call.
- Public sticky comments must keep the "automated review — verify findings
  before acting" disclaimer and the text sanitising. Model-controlled text
  reaching a public comment unescaped is an injection vector.

See the "Security model" section of `README.md` and `docs/ONBOARDING.md`.
