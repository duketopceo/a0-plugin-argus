import asyncio
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

import usr.plugins.argus.helpers.argus as argus

FIXTURES = Path(__file__).parent / "fixtures"
FAKE_CLI = Path(__file__).parent / "fakebin" / "fake_cli.py"


def run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


def fake_argv(**env):
    return [sys.executable, str(FAKE_CLI)]


# --- normalize_pr -----------------------------------------------------------

def test_normalize_url():
    assert argus.normalize_pr("https://github.com/o/r/pull/7") == ("o", "r", 7)


def test_normalize_slug_forms():
    assert argus.normalize_pr("o/r/pull/9") == ("o", "r", 9)
    assert argus.normalize_pr("o/r#9") == ("o", "r", 9)


def test_normalize_rejects_ghe(tmp_path):
    with pytest.raises(argus.ArgusError, match="Enterprise"):
        argus.normalize_pr("https://gitlab.example.com/o/r/-/merge_requests/3")


def test_normalize_malformed():
    with pytest.raises(argus.ArgusError, match="couldn't parse"):
        argus.normalize_pr("not-a-pr")


def test_normalize_bare_number_no_origin(tmp_path):
    with pytest.raises(argus.ArgusError, match="bare PR number"):
        argus.normalize_pr("42", checkout=tmp_path)


def test_normalize_bare_number_from_origin(tmp_path):
    _init_git(tmp_path, remote="git@github.com:acme/widgets.git")
    assert argus.normalize_pr("42", checkout=tmp_path) == ("acme", "widgets", 42)


# --- build_child_env ----------------------------------------------------------

def test_env_allowlist_scrubs_ambient(monkeypatch):
    monkeypatch.setenv("ARGUS_SANDBOX", "1")
    monkeypatch.setenv("GH_TOKEN", "leakme")
    monkeypatch.setenv("GITHUB_EVENT_NAME", "pull_request")
    monkeypatch.setenv("NODE_OPTIONS", "--inspect")
    monkeypatch.setenv("NPM_CONFIG_FOO", "x")
    monkeypatch.setenv("GIT_DIR", "/tmp/evil")
    env = argus.build_child_env({"GITHUB_TOKEN": "tok", "ARGUS_UNTRUSTED": "1"})
    for bad in ("ARGUS_SANDBOX", "GH_TOKEN", "GITHUB_EVENT_NAME", "NODE_OPTIONS", "NPM_CONFIG_FOO", "GIT_DIR"):
        assert bad not in env
    assert env["GIT_CONFIG_NOSYSTEM"] == "1"
    assert env["GIT_CONFIG_GLOBAL"] == os.devnull
    assert env["GITHUB_TOKEN"] == "tok"
    assert env["ARGUS_UNTRUSTED"] == "1"


def test_env_overlay_none_removes(monkeypatch):
    monkeypatch.setenv("HOME", "/h")
    env = argus.build_child_env({"HOME": None})
    assert "HOME" not in env


# --- scrub ------------------------------------------------------------------

def test_scrub_masks_userinfo():
    assert argus.scrub_line("fatal: https://user:secret@github.com/o/r failed") == (
        "fatal: https://***@github.com/o/r failed"
    )
    assert argus.scrub_line("plain line") == "plain line"


# --- run_argus ----------------------------------------------------------------

def test_run_argus_captures_tail_and_code(tmp_path):
    env = argus.build_child_env({"ARGUS_FAKE_REVIEW_JSON": json.dumps({"ok": True})})
    res = run(
        argus.run_argus(
            fake_argv() + ["code-review", "--report-dir", str(tmp_path)],
            tmp_path,
            env,
            30,
        )
    )
    assert res["code"] == 0
    assert not res["timed_out"]
    assert (tmp_path / "code-review.json").exists()


def test_run_argus_timeout_kills_group(tmp_path):
    env = argus.build_child_env({"ARGUS_FAKE_SLEEP": "30"})
    t0 = time.monotonic()
    res = run(argus.run_argus(fake_argv(), tmp_path, env, 1))
    assert res["timed_out"]
    assert time.monotonic() - t0 < 10  # group was killed, not waited out


def test_run_argus_spawn_error_is_readable(tmp_path):
    res = run(argus.run_argus(["/nonexistent/binary-xyz"], tmp_path, {}, 5))
    assert res["spawn_error"] is not None
    assert "couldn't start" in res["spawn_error"]


def test_run_argus_abort_check(tmp_path):
    env = argus.build_child_env({"ARGUS_FAKE_SLEEP": "30"})
    res = run(argus.run_argus(fake_argv(), tmp_path, env, 30, abort_check=lambda: True))
    assert res["aborted"]


def test_run_argus_scrubs_streamed_credentials(tmp_path):
    env = argus.build_child_env({"ARGUS_FAKE_STDERR": "fail https://u:p@github.com/x"})
    lines = []
    res = run(
        argus.run_argus(fake_argv(), tmp_path, env, 10, on_line=lines.append)
    )
    assert any("***@" in l for l in lines)
    assert not any("u:p@" in l for l in lines)


# --- prepare_review_cwd -------------------------------------------------------

def _init_git(path, remote=None):
    path.mkdir(parents=True, exist_ok=True)
    env = {**os.environ, "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull}
    def g(*a):
        subprocess.run(["git", "-C", str(path), *a], check=True, env=env,
                       capture_output=True, stdin=subprocess.DEVNULL)
    g("init", "-q")
    if remote:
        g("remote", "add", "origin", remote)
    g("-c", "user.email=t@t", "-c", "user.name=t", "commit", "--allow-empty", "-qm", "x")


def test_prepare_no_checkout_is_scratch():
    cwd, note = argus.prepare_review_cwd(None, trusted=False)
    assert Path(cwd).is_dir() and not any(Path(cwd).iterdir())
    assert "scratch" in note


def test_prepare_untrusted_archives_without_exec_config(tmp_path):
    src = tmp_path / "checkout"
    _init_git(src)
    (src / "argus-reviewer.config.ts").write_text("export default {}\n")
    (src / "argus-reviewer.config.json").write_text('{"model":"m"}\n')
    (src / "src").mkdir()
    (src / "src" / "app.ts").write_text("export {}\n")
    subprocess.run(
        ["git", "-C", str(src), "add", "-A"], check=True, capture_output=True
    )
    subprocess.run(
        ["git", "-C", str(src), "-c", "user.email=t@t", "-c", "user.name=t",
         "commit", "-qm", "add"], check=True, capture_output=True,
    )
    cwd, note = argus.prepare_review_cwd(str(src), trusted=False)
    assert Path(cwd) != src
    assert (Path(cwd) / "src" / "app.ts").exists()
    assert not (Path(cwd) / "argus-reviewer.config.ts").exists()
    assert (Path(cwd) / "argus-reviewer.config.json").exists()  # data config survives
    assert "archive" in note


def test_prepare_untrusted_non_git_errors(tmp_path):
    with pytest.raises(argus.ArgusError, match="isn't a git repository"):
        argus.prepare_review_cwd(str(tmp_path), trusted=False)


def test_prepare_trusted_returns_real(tmp_path):
    cwd, note = argus.prepare_review_cwd(str(tmp_path), trusted=True)
    assert Path(cwd) == tmp_path


# --- resolve_cli ----------------------------------------------------------------

def test_resolve_cli_untrusted_ignores_checkout_bin(tmp_path, monkeypatch):
    local = tmp_path / "node_modules" / ".bin" / "argus-reviewer"
    local.parent.mkdir(parents=True)
    local.write_text("#!/bin/sh\n")
    vendored = tmp_path / "vendored" / "argus-reviewer"
    vendored.parent.mkdir(parents=True)
    vendored.write_text("#!/bin/sh\n")
    monkeypatch.setattr(argus, "VENDORED_BIN", vendored)
    assert argus.resolve_cli(tmp_path, trusted=False) == str(vendored)
    assert argus.resolve_cli(tmp_path, trusted=True) == str(local)


def test_resolve_cli_none_when_missing(tmp_path, monkeypatch):
    monkeypatch.setattr(argus, "VENDORED_BIN", tmp_path / "nope")
    assert argus.resolve_cli(None, trusted=False) is None


# --- report parsing / narration -------------------------------------------------

def _write(d, name, fixture):
    (d / name).write_text((FIXTURES / fixture).read_text())


def test_narrate_review_needs_changes(tmp_path):
    _write(tmp_path, "code-review.json", "code-review.json")
    msg = argus.narrate_review(argus.parse_review_report(tmp_path), str(tmp_path))
    assert "needs_changes" in msg
    assert "budget exceeded" in msg
    assert "bug: 1" in msg
    assert "src/discount.ts:42" in msg
    assert "not_exercised" in msg
    assert "probe lane skipped" in msg
    assert "$0.0312" in msg


def test_narrate_review_skipped(tmp_path):
    (tmp_path / "code-review.json").write_text(
        json.dumps({"skipped": True, "summary": "no GITHUB_TOKEN"})
    )
    msg = argus.narrate_review(argus.parse_review_report(tmp_path), str(tmp_path))
    assert "skipped" in msg and "no GITHUB_TOKEN" in msg


def test_narrate_review_missing_report(tmp_path):
    assert argus.parse_review_report(tmp_path) is None
    assert "no report" in argus.narrate_review(None, str(tmp_path))


def test_narrate_review_jev_fields_if_present(tmp_path):
    _write(tmp_path, "code-review.json", "code-review-jev.json")
    msg = argus.narrate_review(argus.parse_review_report(tmp_path), str(tmp_path))
    assert "approve" in msg
    assert "p=0.4" in msg
    assert "[convention]" in msg
    assert "triage" in msg and "annotate" in msg
    assert "secrets scan skipped" in msg


def test_narrate_run_pass_fail(tmp_path):
    _write(tmp_path, "run.json", "run.json")
    msg = argus.narrate_run(argus.parse_run_report(tmp_path), str(tmp_path))
    assert "2/3 passed" in msg
    assert "profile flow" in msg
    assert "assert 'name visible' failed" in msg


def test_narrate_run_zero_tests(tmp_path):
    (tmp_path / "run.json").write_text(json.dumps({"totals": {"tests": 0}}))
    msg = argus.narrate_run(argus.parse_run_report(tmp_path), str(tmp_path))
    assert "no test files" in msg


def test_narrate_run_missing(tmp_path):
    assert "no report" in argus.narrate_run(None, str(tmp_path))
