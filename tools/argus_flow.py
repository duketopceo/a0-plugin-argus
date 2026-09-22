import re
from urllib.parse import urlparse

from helpers.tool import Tool, Response

from usr.plugins.argus.helpers import argus as A
from usr.plugins.argus.helpers import runtime

_URL_USERINFO = re.compile(r"^https?://[^/@\s]+@")


class ArgusFlow(Tool):
    """Replay a recorded Argus flow against an explicit URL.

    Checkout code (test files + page setup) executes inside the A0 host, so the
    tool refuses to run unless the plugin's trust_checkout setting is enabled.
    """

    async def execute(self, checkout="", url="", pattern="", **_kwargs):
        try:
            settings = A.resolve_settings(self.agent)
            if not A._truthy(settings.get("trust_checkout")):
                return self._fail(
                    "refusing to run — flow replay executes checkout-controlled test "
                    "files inside this A0 host. Enable `trust_checkout` in the plugin "
                    "settings only for a checkout you control."
                )
            checkout = (checkout or settings.get("default_checkout") or "").strip()
            if not checkout:
                return self._fail(
                    "a `checkout` path is required (or set `default_checkout` in the "
                    "plugin settings) — flow replay runs the checkout's test files."
                )
            if not url or not str(url).strip():
                return self._fail(
                    "an explicit `url` is required — the tool never infers one."
                )
            url = str(url).strip()
            if _URL_USERINFO.match(url) or urlparse(url).password:
                return self._fail(
                    "the url contains embedded credentials (userinfo) — pass a clean "
                    "url and configure auth inside the test setup instead."
                )

            or_key, or_name = A.env_value(
                settings, "openrouter_key_env", "OPENROUTER_API_KEY"
            )
            if not or_key:
                return self._fail(
                    f"`{or_name}` is not set in the A0 host environment."
                )

            probe = runtime.read_probe_cache()
            if probe and not probe.get("playwright"):
                return self._fail(
                    "Playwright browsers are not installed in this A0 environment. "
                    "Run `npx playwright install chromium` on the A0 host."
                )

            exe = A.resolve_cli(checkout, trusted=True)
            if exe is None:
                return self._fail(
                    "argus-reviewer binary not found (checkout node_modules/.bin or "
                    "the vendored copy). Reinstall the plugin or the checkout's deps."
                )

            report_dir = A.fresh_report_dir()
            argv = [exe, "run"]
            if str(pattern).strip():
                argv.append(str(pattern).strip())
            argv += ["--report-dir", report_dir, "--url", url]
            overlays = {
                "OPENROUTER_API_KEY": or_key,
                "ARGUS_UNTRUSTED": "1",
            }
            if str(settings.get("flow_budget_usd") or "").strip():
                overlays["ARGUS_BUDGET_USD"] = str(
                    A._bounded(settings.get("flow_budget_usd"), 1, 1000, 5)
                )
            env = A.build_child_env(overlays)
            res = await A.run_argus(
                argv,
                checkout,
                env,
                timeout_s=A._bounded(settings.get("flow_timeout_s"), 1, 7200, 900),
                on_line=lambda line: self.add_progress(line + "\n"),
                abort_check=lambda: self.agent.handle_intervention(),
            )
            if res["spawn_error"]:
                return self._fail(f"could not start argus: {res['spawn_error']}")
            if res["timed_out"]:
                return self._fail(
                    "argus run exceeded the timeout and was killed.\n\n"
                    f"output tail:\n{res['tail'][-1500:]}"
                )
            report = A.parse_run_report(report_dir)
            msg = A.narrate_run(report, report_dir)
            if report is None and res["tail"].strip():
                msg += f"\n\nrun output tail:\n{res['tail'][-1500:]}"
            return Response(message=msg, break_loop=False)
        except A.ArgusError as e:
            return self._fail(str(e))

    def _fail(self, message):
        return Response(message=f"argus_flow: {message}", break_loop=False)
