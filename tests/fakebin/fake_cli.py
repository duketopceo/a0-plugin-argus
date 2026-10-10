#!/usr/bin/env python3
"""Fake argus-reviewer for tests. Behavior driven by ARGUS_FAKE_* env:
  ARGUS_FAKE_SLEEP   — seconds to sleep before acting (timeout tests)
  ARGUS_FAKE_EXIT    — exit code (default 0)
  ARGUS_FAKE_STDERR  — text to print to stderr
  ARGUS_FAKE_CALLS   — file to append each invoked subcommand to
  ARGUS_FAKE_INDEX_FAIL — `index` exits 1 instead of writing argus.index.json
  ARGUS_FAKE_ORPHAN_PIPE — spawn a setsid'd grandchild that inherits our
      stdout and sleeps 60s, then exit. Simulates a leaked pipe-holder:
      killpg can't reach it (it escaped the group) so a naive stdout drain
      would hang waiting for EOF.
Writes tests/fixtures-style JSON into --report-dir.
"""

import json
import os
import subprocess
import sys
import time


def main():
    if os.environ.get("ARGUS_FAKE_ORPHAN_PIPE"):
        subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(60)"],
            stdin=subprocess.DEVNULL,
            stdout=sys.stdout,      # inherit — holds the pipe past our exit
            stderr=subprocess.DEVNULL,
            start_new_session=True, # escape our process group: killpg-proof
        )
    sleep = float(os.environ.get("ARGUS_FAKE_SLEEP", "0") or 0)
    if sleep:
        time.sleep(sleep)
    if os.environ.get("ARGUS_FAKE_STDERR"):
        sys.stderr.write(os.environ["ARGUS_FAKE_STDERR"] + "\n")
    args = sys.argv[1:]
    report_dir = "."
    if "--report-dir" in args:
        report_dir = args[args.index("--report-dir") + 1]
    cmd = args[0] if args else ""
    if os.environ.get("ARGUS_FAKE_CALLS"):
        with open(os.environ["ARGUS_FAKE_CALLS"], "a") as f:
            f.write(cmd + "\n")
    if cmd == "index":
        if os.environ.get("ARGUS_FAKE_INDEX_FAIL"):
            sys.exit(1)
        with open("argus.index.json", "w") as f:
            json.dump({"files": {}}, f)
    elif cmd == "code-review":
        with open(os.path.join(report_dir, "code-review.json"), "w") as f:
            json.dump(json.loads(os.environ.get("ARGUS_FAKE_REVIEW_JSON", "{}")), f)
    elif cmd == "run":
        with open(os.path.join(report_dir, "run.json"), "w") as f:
            json.dump(json.loads(os.environ.get("ARGUS_FAKE_RUN_JSON", "{}")), f)
    sys.exit(int(os.environ.get("ARGUS_FAKE_EXIT", "0")))


if __name__ == "__main__":
    main()
