### argus_flow
Replay recorded Argus flow tests against a target URL inside a trusted checkout, and narrate pass/fail back into this conversation.

args: `url`, `checkout`, optional `pattern` — all strings.
- `url`: **required.** The explicit target URL the tests run against. Never inferred. Must not contain embedded credentials (`https://user:pass@host` is rejected).
- `checkout`: absolute path to a trusted git checkout containing the recorded tests. Falls back to the plugin's `default_checkout` setting.
- `pattern`: optional substring filter — only test files whose path contains it are run.

**WARNING — code execution:** flow replay executes the checkout's test files and page-setup modules inside this A0 host. The tool refuses to run at all unless the plugin's `trust_checkout` setting is enabled. Only enable it for checkouts you control. Requires Playwright browsers on the host.

Replay is cheap: recorded selectors replay without model calls; fingerprint-cached flows cost near zero when the UI is unchanged. Results narrate passed/failed tests, failure messages, budget status, and the local report path. Zero matched tests is narrated as "no test files matched", not success.

requires: `OPENROUTER_API_KEY` (or the configured env name) in the host environment, `trust_checkout: true` in plugin settings, and Playwright browsers installed on the host.

example:
~~~json
{
  "thoughts": ["The user asked me to replay the login flow tests against the staging URL."],
  "headline": "Replaying Argus flow tests against staging",
  "tool_name": "argus_flow",
  "tool_args": {
    "checkout": "/home/user/projects/myapp",
    "url": "https://staging.example.com",
    "pattern": "login"
  }
}
~~~
