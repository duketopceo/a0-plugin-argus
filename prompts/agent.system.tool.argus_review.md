### argus_review
Run an Argus code review on a GitHub pull request and narrate the result back into this conversation.

args: `pr`, optional `checkout`, `post` — all strings.
- `pr`: the pull request to review. Accepts a full GitHub PR URL (`https://github.com/owner/repo/pull/123`), `owner/repo#123`, `owner/repo/pull/123`, or a bare PR number (only when `checkout` has a github.com `origin` remote). github.com only.
- `checkout`: absolute path to a local git checkout used as review context. Untrusted checkouts are copied via `git archive` with executable config files stripped before review. Optional — omit to review from the PR diff alone (evidence linkage will be inconclusive). Falls back to the plugin's `default_checkout` setting.
- `post`: `"false"` (default) or `"true"`. When `"true"`, the review result is also written to the PR as a public sticky comment. **This publishes a comment on GitHub — only set `post:"true"` when the user explicitly asks to post.**

The review result is always narrated into the conversation: verdict, findings (with file/line), model, token and cost totals, and the local report path. `needs_changes`, skipped reviews, and budget-exceeded are narrated results, not failures.

requires: `OPENROUTER_API_KEY` and `GITHUB_TOKEN` (or the env names configured in plugin settings) in the host environment. `post:"true"` additionally needs a token with Issues read+write — see `comment_token_env`.

example (narrate only):
~~~json
{
  "thoughts": ["The user asked me to review PR #71 on duketopceo/Argus and narrate the result."],
  "headline": "Running Argus review on PR #71",
  "tool_name": "argus_review",
  "tool_args": {
    "pr": "duketopceo/Argus#71"
  }
}
~~~

example (post a sticky comment):
~~~json
{
  "thoughts": ["The user explicitly asked to post the review back to the PR."],
  "headline": "Reviewing and posting a sticky comment",
  "tool_name": "argus_review",
  "tool_args": {
    "pr": "https://github.com/duketopceo/Argus/pull/71",
    "post": "true"
  }
}
~~~
