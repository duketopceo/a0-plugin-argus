import json

from helpers.tool import Tool, Response

from usr.plugins.argus.helpers import argus as A
from usr.plugins.argus.helpers import runtime


class ArgusReview(Tool):
    """Code-review a GitHub pull request with Argus and narrate the result.

    Posts a sticky PR comment only when called with post=true.
    """

    async def execute(self, checkout="", pr="", post="false", **_kwargs):
        try:
            settings = A.resolve_settings(self.agent)
            trusted = A._truthy(settings.get("trust_checkout"))
            post_flag = A._truthy(post)

            gh_token, gh_name = A.env_value(settings, "github_token_env", "GITHUB_TOKEN")
            or_key, or_name = A.env_value(settings, "openrouter_key_env", "OPENROUTER_API_KEY")
            if not or_key:
                return self._fail(f"`{or_name}` is not set in the A0 host environment.")
            if not gh_token:
                return self._fail(f"`{gh_name}` is not set in the A0 host environment.")

            # Fail before spending on the review if posting can't work.
            comment_token = gh_token
            if post_flag:
                ctok, ctok_name = A.env_value(settings, "comment_token_env")
                if ctok_name and not ctok:
                    return self._fail(
                        f"post=true requested but `{ctok_name}` is not set in the A0 host environment."
                    )
                comment_token = ctok or gh_token

            checkout = (checkout or settings.get("default_checkout") or "").strip() or None
            owner, repo, number = A.normalize_pr(pr, checkout=checkout)

            probe = runtime.read_probe_cache()
            if probe and not probe.get("node_ok"):
                return self._fail(
                    "Node.js is not available in this A0 environment "
                    f"(install probe: {json.dumps(probe)}). argus_review cannot run here."
                )
            exe = A.resolve_cli(checkout, trusted)
            if exe is None:
                return self._fail(
                    "Vendored argus-reviewer binary not found. Reinstall the plugin, "
                    "or enable trust_checkout and pass a checkout whose dependencies "
                    "include argus-reviewer-e2e."
                )

            A.preflight_pr(owner, repo, number, gh_token)

            cwd, cwd_note = A.prepare_review_cwd(checkout, trusted)
            report_dir = A.fresh_report_dir()
            env = A.build_child_env(
                {
                    "GITHUB_TOKEN": gh_token,
                    "GH_TOKEN": gh_token,
                    "OPENROUTER_API_KEY": or_key,
                    "ARGUS_REVIEWER_TRACE": json.dumps(
                        {"repo": f"{owner}/{repo}", "pr": str(number)}
                    ),
                    # Forward-compatible: newer argus releases honor this to keep
                    # checkout-controlled config code out of the review process.
                    "ARGUS_UNTRUSTED": "1",
                }
            )
            res = await A.run_argus(
                [exe, "code-review", "--report-dir", report_dir],
                cwd,
                env,
                timeout_s=A._bounded(settings.get("review_timeout_s"), 1, 14400, 1200),
                on_line=lambda line: self.add_progress(line + "\n"),
                abort_check=lambda: self.agent.handle_intervention(),
            )
            if res["spawn_error"]:
                return self._fail(f"could not start argus: {res['spawn_error']}")
            report = A.parse_review_report(report_dir)
            if res["timed_out"]:
                return self._fail(
                    "argus code-review exceeded the timeout and was killed.\n\n"
                    f"output tail:\n{res['tail'][-1500:]}"
                )

            posted = None
            if post_flag and report is not None:
                body = A.render_sticky_body(report)
                posted = A.post_sticky(owner, repo, number, body, comment_token)

            msg = A.narrate_review(report, report_dir, posted=posted, cwd_note=cwd_note)
            if report is None and res["tail"].strip():
                msg += f"\n\nrun output tail:\n{res['tail'][-1500:]}"
            return Response(message=msg, break_loop=False)
        except A.ArgusError as e:
            return self._fail(str(e))

    def _fail(self, message):
        return Response(message=f"argus_review: {message}", break_loop=False)
