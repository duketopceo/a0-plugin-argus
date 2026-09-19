# a0-plugin-argus

Agent Zero plugin for [Argus](https://github.com/duketopceo/Argus) — gives any
A0 instance two tools:

- **`argus_review`** — AI pull-request review (verdict, findings, cost) narrated
  into the conversation. Optionally posts a sticky comment when asked.
- **`argus_flow`** — replays recorded Argus flows against a URL in a checkout
  you trust.

Requires `node >= 20.19`, `npm`, `git` in the A0 tool environment, plus
`GITHUB_TOKEN` and `OPENROUTER_API_KEY` set as environment variables on the
A0 host. Full setup, settings, token scopes, and trust model docs land with
the v0.1 release.
