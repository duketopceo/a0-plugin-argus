#!/usr/bin/env python3
"""Fake argus-reviewer for tests. Behavior driven by ARGUS_FAKE_* env:
  ARGUS_FAKE_SLEEP   — seconds to sleep before acting (timeout tests)
  ARGUS_FAKE_EXIT    — exit code (default 0)
  ARGUS_FAKE_STDERR  — text to print to stderr
Writes tests/fixtures-style JSON into --report-dir.
"""

import json
import os
import sys
import time


def main():
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
    if cmd == "code-review":
        with open(os.path.join(report_dir, "code-review.json"), "w") as f:
            json.dump(json.loads(os.environ.get("ARGUS_FAKE_REVIEW_JSON", "{}")), f)
    elif cmd == "run":
        with open(os.path.join(report_dir, "run.json"), "w") as f:
            json.dump(json.loads(os.environ.get("ARGUS_FAKE_RUN_JSON", "{}")), f)
    sys.exit(int(os.environ.get("ARGUS_FAKE_EXIT", "0")))


if __name__ == "__main__":
    main()
