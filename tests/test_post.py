import json

import pytest

import usr.plugins.argus.helpers.argus as argus


class FakeGH:
    """Scripted _gh responses: maps (method, url-prefix) → list of (status, payload)."""

    def __init__(self):
        self.routes = {}
        self.calls = []

    def add(self, method, prefix, *responses):
        self.routes.setdefault((method, prefix), []).extend(responses)

    def __call__(self, method, url, token, body=None, timeout=30):
        self.calls.append((method, url, body))
        for (m, prefix), responses in self.routes.items():
            if m == method and url.startswith(prefix):
                if not responses:
                    return 404, None, {}
                if len(responses) == 1:
                    return (*responses[0], {})
                return (*responses.pop(0), {})
        return 404, None, {}


ISSUES = "https://api.github.com/repos/o/r/issues/4/comments"
COMMENTS = "https://api.github.com/repos/o/r/issues/comments"


def test_post_creates_when_no_sentinel(monkeypatch):
    gh = FakeGH()
    gh.add("GET", ISSUES, (200, []))
    gh.add("POST", ISSUES, (201, {"html_url": "https://github.com/o/r/issues/4#issuecomment-1"}))
    monkeypatch.setattr(argus, "_gh", gh)
    url, how = argus.post_sticky("o", "r", 4, "body", "tok")
    assert how == "created"
    assert url.endswith("issuecomment-1")


def test_post_updates_existing_sentinel_on_later_page(monkeypatch):
    gh = FakeGH()
    page1 = [{"id": i, "body": "other"} for i in range(100)]
    page2 = [{"id": 555, "body": f"{argus.SENTINEL}\nold"}]
    gh.add("GET", ISSUES, (200, page1), (200, page2))
    gh.add("PATCH", COMMENTS, (200, {"html_url": "https://github.com/o/r/issues/4#issuecomment-555"}))
    monkeypatch.setattr(argus, "_gh", gh)
    url, how = argus.post_sticky("o", "r", 4, "new body", "tok")
    assert how == "updated"
    method, url_called, body = gh.calls[-1]
    assert method == "PATCH" and "/comments/555" in url_called and body == {"body": "new body"}


def test_post_patch_404_falls_back_to_post(monkeypatch):
    gh = FakeGH()
    gh.add("GET", ISSUES, (200, [{"id": 9, "body": argus.SENTINEL}]))
    gh.add("PATCH", COMMENTS, (404, None))
    gh.add("POST", ISSUES, (201, {"html_url": "https://x/1"}))
    monkeypatch.setattr(argus, "_gh", gh)
    url, how = argus.post_sticky("o", "r", 4, "b", "tok")
    assert how == "created"


def test_post_403_gives_scope_guidance(monkeypatch):
    gh = FakeGH()
    gh.add("GET", ISSUES, (200, []))
    gh.add("POST", ISSUES, (403, None))
    monkeypatch.setattr(argus, "_gh", gh)
    with pytest.raises(argus.ArgusError, match="Issues read\\+write"):
        argus.post_sticky("o", "r", 4, "b", "tok")


def test_post_422_truncates_and_retries(monkeypatch):
    gh = FakeGH()
    gh.add("GET", ISSUES, (200, []))
    gh.add("POST", ISSUES, (422, None), (201, {"html_url": "https://x/2"}))
    monkeypatch.setattr(argus, "_gh", gh)
    url, how = argus.post_sticky("o", "r", 4, "x" * 20000, "tok")
    assert how == "created"
    assert len(gh.calls[-1][2]["body"]) < 10000


def test_render_body_shape():
    report = {
        "verdict": "needs_changes",
        "summary": "a | b\nsecond line",
        "visionCostUsd": 0.05,
        "tokens": 1000,
        "model": "m/x",
        "budgetExceeded": True,
        "findings": [{"file": "a.ts", "line": 1, "severity": "bug", "message": "m"}],
    }
    body = argus.render_sticky_body(report)
    assert body.startswith(argus.SENTINEL)
    assert "needs_changes" in body and "budget exceeded" in body
    assert "a \\| b second line" in body  # cell escaping
    assert "| `a.ts:1` | bug | m |" in body
    assert "verify findings before acting" in body


def test_render_body_caps_findings():
    report = {
        "verdict": "needs_changes",
        "summary": "s",
        "findings": [{"file": f"f{i}.ts", "severity": "nit", "message": "m"} for i in range(30)],
    }
    body = argus.render_sticky_body(report)
    assert "5 more findings" in body
    assert body.count("| `f") == 25


def test_render_skipped_body():
    body = argus.render_sticky_body({"skipped": True, "summary": "no token"})
    assert "skipped" in body and "no token" in body
