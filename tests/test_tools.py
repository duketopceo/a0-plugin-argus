import asyncio
import json
import sys
from pathlib import Path

import pytest

import usr.plugins.argus.helpers.argus as A
import usr.plugins.argus.helpers.runtime as runtime
from usr.plugins.argus.tools.argus_review import ArgusReview
from usr.plugins.argus.tools.argus_flow import ArgusFlow
from conftest import FakeAgent

ROOT = Path(__file__).resolve().parents[1]
FAKE_CLI = str(ROOT / "tests" / "fakebin" / "fake_cli.py")
FIXTURES = ROOT / "tests" / "fixtures"


def run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


def _tool(cls, agent=None):
    return cls(agent=agent or FakeAgent(), name="x", method=None, args={}, message="")


def _tokens(monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "gh-tok")
    monkeypatch.setenv("OPENROUTER_API_KEY", "or-key")


def _patch_run(monkeypatch, env_overrides=None, cli=FAKE_CLI):
    """Real subprocess + fake CLI; inject ARGUS_FAKE_* into the child env."""
    monkeypatch.setattr(A, "resolve_cli", lambda checkout=None, trusted=False: cli)
    monkeypatch.setattr(A, "preflight_pr", lambda *a: None)
    real_env = A.build_child_env
    monkeypatch.setattr(
        A, "build_child_env", lambda ov: {**real_env(ov), **(env_overrides or {})}
    )


# ---------------------------------------------------------------- review ----

def test_review_narrates(monkeypatch):
    _tokens(monkeypatch)
    _patch_run(monkeypatch, {
        "ARGUS_FAKE_REVIEW_JSON": (FIXTURES / "code-review.json").read_text()
    })
    res = run(_tool(ArgusReview).execute(pr="owner/repo#7"))
    assert "needs_changes" in res.message or "ok" in res.message
    assert "report:" in res.message


def test_review_missing_openrouter_key(monkeypatch):
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    monkeypatch.setenv("GITHUB_TOKEN", "x")
    called = []
    monkeypatch.setattr(A, "resolve_cli", lambda *a, **k: called.append(1) or FAKE_CLI)
    res = run(_tool(ArgusReview).execute(pr="owner/repo#7"))
    assert "OPENROUTER_API_KEY" in res.message and not called


def test_review_missing_github_token(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "x")
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    res = run(_tool(ArgusReview).execute(pr="owner/repo#7"))
    assert "GITHUB_TOKEN" in res.message


def test_review_post_true_posts(monkeypatch):
    _tokens(monkeypatch)
    _patch_run(monkeypatch, {
        "ARGUS_FAKE_REVIEW_JSON": (FIXTURES / "code-review.json").read_text()
    })
    posts = []
    monkeypatch.setattr(
        A, "post_sticky", lambda *a: posts.append(a) or ("https://x/1", "created")
    )
    res = run(_tool(ArgusReview).execute(pr="owner/repo#7", post="true"))
    assert len(posts) == 1 and posts[0][0:3] == ("owner", "repo", 7)
    assert "posted" in res.message


def test_review_post_without_comment_token_fails_early(monkeypatch):
    _tokens(monkeypatch)
    monkeypatch.setenv("MISSING_COMMENT_TOKEN", "")
    monkeypatch.delenv("MISSING_COMMENT_TOKEN", raising=False)
    settings = A.load_default_config()
    settings["comment_token_env"] = "MISSING_COMMENT_TOKEN"
    monkeypatch.setattr(A, "resolve_settings", lambda agent=None: settings)
    called = []
    monkeypatch.setattr(A, "resolve_cli", lambda *a, **k: called.append(1) or FAKE_CLI)
    res = run(_tool(ArgusReview).execute(pr="owner/repo#7", post="true"))
    assert "MISSING_COMMENT_TOKEN" in res.message and not called


def test_review_timeout_message(monkeypatch):
    _tokens(monkeypatch)
    _patch_run(monkeypatch, {"ARGUS_FAKE_SLEEP": "30"})
    settings = A.load_default_config()
    settings["review_timeout_s"] = 1
    monkeypatch.setattr(A, "resolve_settings", lambda agent=None: settings)
    res = run(_tool(ArgusReview).execute(pr="owner/repo#7"))
    assert "timeout" in res.message.lower() or "killed" in res.message.lower()


def test_review_no_report_surfaces_tail(monkeypatch):
    _tokens(monkeypatch)
    _patch_run(monkeypatch, {
        "ARGUS_FAKE_EXIT": "2",
        "ARGUS_FAKE_STDERR": "boom: something broke",
    })
    # fake_cli writes {} on code-review — parse yields a dict, so force a crash:
    monkeypatch.setattr(A, "parse_review_report", lambda d: None)
    res = run(_tool(ArgusReview).execute(pr="owner/repo#7"))
    assert "boom: something broke" in res.message


# ------------------------------------------------------------------ flow ----

def test_flow_refuses_without_trust(monkeypatch):
    _tokens(monkeypatch)
    called = []
    monkeypatch.setattr(A, "resolve_cli", lambda *a, **k: called.append(1) or FAKE_CLI)
    res = run(_tool(ArgusFlow).execute(checkout="/tmp/x", url="https://app.example"))
    assert "trust_checkout" in res.message and not called


def _trusted(monkeypatch):
    settings = A.load_default_config()
    settings["trust_checkout"] = True
    monkeypatch.setattr(A, "resolve_settings", lambda agent=None: settings)


def test_flow_requires_url(monkeypatch):
    _tokens(monkeypatch)
    _trusted(monkeypatch)
    res = run(_tool(ArgusFlow).execute(checkout="/tmp/x"))
    assert "url" in res.message.lower()


def test_flow_rejects_userinfo_url(monkeypatch):
    _tokens(monkeypatch)
    _trusted(monkeypatch)
    res = run(_tool(ArgusFlow).execute(
        checkout="/tmp/x", url="https://user:pass@app.example"))
    assert "credentials" in res.message


def test_flow_narrates_results(monkeypatch, tmp_path):
    _tokens(monkeypatch)
    _trusted(monkeypatch)
    _patch_run(monkeypatch, {
        "ARGUS_FAKE_RUN_JSON": (FIXTURES / "run.json").read_text()
    })
    res = run(_tool(ArgusFlow).execute(
        checkout=str(tmp_path), url="https://app.example"))
    assert "2/3 passed" in res.message
    assert "profile flow" in res.message
    assert "assert 'name visible' failed" in res.message


def test_flow_zero_tests(monkeypatch, tmp_path):
    _tokens(monkeypatch)
    _trusted(monkeypatch)
    _patch_run(monkeypatch, {
        "ARGUS_FAKE_RUN_JSON": json.dumps({"ok": True, "totals": {"tests": 0}, "tests": []})
    })
    res = run(_tool(ArgusFlow).execute(
        checkout=str(tmp_path), url="https://app.example"))
    assert "no test files" in res.message


def test_flow_missing_playwright(monkeypatch, tmp_path):
    _tokens(monkeypatch)
    _trusted(monkeypatch)
    monkeypatch.setattr(
        runtime, "read_probe_cache",
        lambda: {"node_ok": True, "playwright_ok": False},
    )
    called = []
    monkeypatch.setattr(A, "resolve_cli", lambda *a, **k: called.append(1) or FAKE_CLI)
    res = run(_tool(ArgusFlow).execute(
        checkout=str(tmp_path), url="https://app.example"))
    assert "Playwright" in res.message and not called


def _fake_home_with_browser(monkeypatch, tmp_path, present=True):
    """Fake $HOME for the real probe: optionally plant a Playwright cache."""
    home = tmp_path / "home"
    home.mkdir()
    if present:
        cache = home / ".cache" / "ms-playwright" / "chromium-1194"
        cache.mkdir(parents=True)
        (cache / "chrome-linux").write_text("fake-browser")
    monkeypatch.setenv("HOME", str(home))
    return home


def _write_real_probe(monkeypatch, tmp_path, browser_present):
    """Run the REAL runtime.probe(), write it to a tmp probe cache, and point
    read_probe_cache() at it. Only the environment (HOME) and the network
    side-effect (_vendor) are faked."""
    _fake_home_with_browser(monkeypatch, tmp_path, present=browser_present)
    monkeypatch.setattr(runtime, "_vendor", lambda pin="": (True, None))
    cache_file = tmp_path / "probe-cache.json"
    monkeypatch.setattr(runtime, "PROBE_CACHE", cache_file)
    result = runtime.probe()
    runtime.write_probe_cache(result)
    return result


def test_flow_real_probe_playwright_present_passes_preflight(monkeypatch, tmp_path):
    """End-to-end schema contract: real probe sees a browser cache, writes the
    cache, and the flow preflight lets the run through."""
    _tokens(monkeypatch)
    _trusted(monkeypatch)
    result = _write_real_probe(monkeypatch, tmp_path, browser_present=True)
    assert result["playwright_ok"] is True
    assert "browser cache present at" in (result["playwright_note"] or "")
    # writer and reader agree on the shared schema keys
    assert set(result) == set(runtime.ProbeResult.__annotations__)

    _patch_run(monkeypatch, {
        "ARGUS_FAKE_RUN_JSON": (FIXTURES / "run.json").read_text()
    })
    res = run(_tool(ArgusFlow).execute(
        checkout=str(tmp_path), url="https://app.example"))
    assert "Playwright browsers are not installed" not in res.message
    assert "2/3 passed" in res.message


def test_flow_real_probe_playwright_missing_message(monkeypatch, tmp_path):
    """Explicit missing-browser path: real probe with no cache on disk fails
    the preflight with an actionable message before touching the CLI."""
    _tokens(monkeypatch)
    _trusted(monkeypatch)
    result = _write_real_probe(monkeypatch, tmp_path, browser_present=False)
    assert result["playwright_ok"] is False

    called = []
    monkeypatch.setattr(A, "resolve_cli", lambda *a, **k: called.append(1) or FAKE_CLI)
    res = run(_tool(ArgusFlow).execute(
        checkout=str(tmp_path), url="https://app.example"))
    assert "Playwright browsers are not installed" in res.message
    assert "npx playwright install chromium" in res.message
    # the probe note explains what was checked, not just "missing"
    assert "no browser cache under" in res.message
    assert not called


def test_read_probe_cache_normalizes_legacy_schema(monkeypatch, tmp_path):
    """Caches written before the shared schema used a bare "playwright" bool;
    they still validate as playwright_ok."""
    cache_file = tmp_path / "probe-cache.json"
    cache_file.write_text(json.dumps({"node_ok": True, "playwright": True}))
    monkeypatch.setattr(runtime, "PROBE_CACHE", cache_file)
    probe = runtime.read_probe_cache()
    assert probe is not None
    assert probe["playwright_ok"] is True
    assert set(probe) == set(runtime.ProbeResult.__annotations__)
