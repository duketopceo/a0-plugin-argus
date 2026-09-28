import json

import pytest

import usr.plugins.argus.helpers.argus as argus


class FakeGH:
    """Scripted _gh responses: maps (method, url-prefix) → list of (status, payload).
    Longest matching prefix wins, so `/pulls/4` doesn't swallow
    `/pulls/4/comments`-style routes."""

    def __init__(self):
        self.routes = {}
        self.calls = []

    def add(self, method, prefix, *responses):
        self.routes.setdefault((method, prefix), []).extend(responses)

    def __call__(self, method, url, token, body=None, timeout=30):
        self.calls.append((method, url, body))
        best = None
        for (m, prefix), responses in self.routes.items():
            if m == method and url.startswith(prefix):
                if best is None or len(prefix) > len(best[0]):
                    best = (prefix, responses)
        if best is None:
            return 404, None, {}
        responses = best[1]
        if not responses:
            return 404, None, {}
        if len(responses) == 1:
            return (*responses[0], {})
        return (*responses.pop(0), {})


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
    with pytest.raises(argus.ArgusError, match="Issues \\+ Pull requests read\\+write"):
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


# ---------------------------------------------------------------- review ----
# post_review consumes the serialized reviewComments[] verbatim — these tests
# script the PR/diff/review state through FakeGH and assert the thin-poster
# contract (freshness, dedup, diff validation, dismissal, retry ladder).

PULLS = "https://api.github.com/repos/o/r/pulls/4"
PULL_COMMENTS = PULLS + "/comments"
PULL_FILES = PULLS + "/files"
PULL_REVIEWS = PULLS + "/reviews"
USER = "https://api.github.com/user"

# RIGHT-side anchors: lines 1-4 (first hunk) and 21-23 (second hunk).
PATCH = (
    "@@ -1,3 +1,4 @@\n context\n-old\n+new\n+more\n"
    "@@ -20,2 +21,3 @@\n context\n+added\n+added2"
)
DIFF = [{"filename": "a.ts", "patch": PATCH}]

PR = {
    "number": 4,
    "head": {"sha": "headsha"},
    "base": {"repo": {"name": "r", "owner": {"login": "o"}}},
}


def _comment(path="a.ts", line=3, message="something is wrong",
             suggestion=None, start_line=None, severity="bug"):
    body = f"**argus-reviewer {severity}:** {message}"
    if suggestion is not None:
        body += (
            f"\n\n````suggestion\n{suggestion}\n````"
            "\n\n*Suggested change — review before committing.*"
        )
    c = {
        "path": path,
        "line": line,
        "side": "RIGHT",
        "body": body,
        "dedupKey": f"{path}:{line}:{body.split(chr(10))[0]}:"
                    f"{argus._short_hash(suggestion or '')}",
    }
    if start_line is not None:
        c["start_line"] = start_line
        c["start_side"] = "RIGHT"
    return c


def _report(**kw):
    report = {
        "verdict": "needs_changes",
        "reviewEvent": "comment",
        "provenBlockers": 0,
        "highConfidenceBlockers": 0,
        "reviewComments": [],
        "commentsOverflow": 0,
        "headBinding": {"intendedSha": "headsha"},
    }
    report.update(kw)
    return report


def _review_gh(gh, existing=None, files=None, reviews=None,
               login="argus-bot", pr=None):
    """Script every GET post_review makes; caller adds POST/PUT routes."""
    gh.add("GET", PULL_COMMENTS, (200, existing or []))
    gh.add("GET", PULL_FILES, (200, DIFF if files is None else files))
    gh.add("GET", PULL_REVIEWS, (200, reviews or []))
    gh.add("GET", USER, (200, {"login": login}))
    gh.add("GET", PULLS, (200, PR if pr is None else pr))


def _posts(gh):
    return [c for c in gh.calls
            if c[0] == "POST" and c[1].startswith(PULL_REVIEWS)]


def test_review_posts_serialized_comments_verbatim(monkeypatch):
    gh = FakeGH()
    _review_gh(gh)
    gh.add("POST", PULL_REVIEWS, (200, {"html_url": "https://x/review-1"}))
    monkeypatch.setattr(argus, "_gh", gh)
    comments = [
        _comment(line=3),
        _comment(path="a.ts", line=22, message="tidy", severity="nit",
                 suggestion="const x = 1;", start_line=21),
    ]
    res = argus.post_review(_report(reviewComments=comments), PR, "tok")
    assert res["status"] == "posted" and res["event"] == "COMMENT"
    assert res["comments"] == 2 and res["url"].endswith("review-1")
    posts = _posts(gh)
    assert len(posts) == 1
    payload = posts[0][2]
    assert payload["commit_id"] == "headsha" and payload["event"] == "COMMENT"
    assert payload["body"].startswith(argus.SENTINEL)
    assert "verdict **needs_changes**" in payload["body"]
    assert payload["comments"] == [
        {k: v for k, v in c.items() if k != "dedupKey"} for c in comments
    ]


def test_review_request_changes_passes_through(monkeypatch):
    gh = FakeGH()
    _review_gh(gh)
    gh.add("POST", PULL_REVIEWS, (200, {"html_url": "https://x/r"}))
    monkeypatch.setattr(argus, "_gh", gh)
    res = argus.post_review(
        _report(reviewEvent="request_changes", provenBlockers=1,
                highConfidenceBlockers=2, reviewComments=[_comment()]),
        PR, "tok",
    )
    assert res["status"] == "posted" and res["event"] == "REQUEST_CHANGES"
    payload = _posts(gh)[0][2]
    assert payload["event"] == "REQUEST_CHANGES"
    assert "⛔ 1 reproduced" in payload["body"]
    assert "◎ 2 high-confidence" in payload["body"]


def test_review_dedup_scoped_to_head_sha(monkeypatch):
    posted_body = (
        "**argus-reviewer bug:** something is wrong"
        "\n\n````suggestion\nfix()\n````"
        "\n\n*Suggested change — review before committing.*"
    )
    existing = [
        # same head + same key → suppressed
        {"commit_id": "headsha", "path": "a.ts", "line": 3, "body": posted_body},
        # same key on an older commit → must NOT suppress
        {"commit_id": "oldsha", "path": "a.ts", "line": 22, "body": posted_body},
        # not ours (no argus prefix) → ignored
        {"commit_id": "headsha", "path": "a.ts", "line": 3,
         "body": "human comment"},
    ]
    gh = FakeGH()
    _review_gh(gh, existing=existing)
    gh.add("POST", PULL_REVIEWS, (200, {"html_url": "https://x/r"}))
    monkeypatch.setattr(argus, "_gh", gh)
    dup = _comment(line=3, suggestion="fix()")
    fresh = _comment(line=22, message="new finding")
    res = argus.post_review(_report(reviewComments=[dup, fresh]), PR, "tok")
    assert res["status"] == "posted"
    assert res["deduped"] == 1 and res["comments"] == 1
    assert _posts(gh)[0][2]["comments"][0]["line"] == 22


def test_review_drops_off_diff_anchors(monkeypatch):
    gh = FakeGH()
    _review_gh(gh)
    gh.add("POST", PULL_REVIEWS, (200, {"html_url": "https://x/r"}))
    monkeypatch.setattr(argus, "_gh", gh)
    comments = [
        _comment(line=3),                          # on-diff
        _comment(line=3, start_line=1),            # on-diff range
        _comment(line=500),                        # past every hunk
        _comment(path="missing.ts", line=3),       # file not in diff
        _comment(line=3, start_line=10),           # start off-diff
        _comment(path="big.bin", line=1),          # no patch → no anchors
    ]
    files = DIFF + [{"filename": "big.bin"}]  # binary: patch absent
    gh.routes[("GET", PULL_FILES)] = [(200, files)]
    res = argus.post_review(_report(reviewComments=comments), PR, "tok")
    assert res["status"] == "posted"
    assert res["comments"] == 2 and res["dropped"] == 4
    lines = [c["line"] for c in _posts(gh)[0][2]["comments"]]
    assert lines == [3, 3]


def test_review_freshness_mismatch_skips(monkeypatch):
    gh = FakeGH()
    _review_gh(gh)
    monkeypatch.setattr(argus, "_gh", gh)
    report = _report(reviewComments=[_comment()])
    report["headBinding"]["intendedSha"] = "stale-sha"
    res = argus.post_review(report, PR, "tok")
    assert res["status"] == "skipped" and "head binding" in res["reason"]
    assert _posts(gh) == []


def test_review_missing_head_sha_skips(monkeypatch):
    gh = FakeGH()
    _review_gh(gh, pr={"number": 4, "base": {"repo": {"name": "r", "owner": {"login": "o"}}}})
    monkeypatch.setattr(argus, "_gh", gh)
    res = argus.post_review(_report(reviewComments=[_comment()]), PR, "tok")
    assert res["status"] == "skipped"
    assert _posts(gh) == []
    # No PR payload at all — skipped before any API call.
    gh2 = FakeGH()
    monkeypatch.setattr(argus, "_gh", gh2)
    res = argus.post_review(_report(reviewComments=[_comment()]), None, "tok")
    assert res["status"] == "skipped" and gh2.calls == []


def test_review_old_report_posts_nothing(monkeypatch):
    """Pre-0.3.0 reports have no serialized surface — no API calls at all."""
    gh = FakeGH()
    monkeypatch.setattr(argus, "_gh", gh)
    old = _report()
    del old["reviewComments"]
    assert argus.post_review(old, PR, "tok") is None
    assert argus.post_review({"skipped": True}, PR, "tok") is None
    assert gh.calls == []


def test_review_downgrades_request_changes_on_403(monkeypatch):
    gh = FakeGH()
    _review_gh(gh)
    gh.add("POST", PULL_REVIEWS, (403, {"message": "forbidden"}),
           (200, {"html_url": "https://x/r"}))
    monkeypatch.setattr(argus, "_gh", gh)
    res = argus.post_review(
        _report(reviewEvent="request_changes", reviewComments=[_comment()]),
        PR, "tok",
    )
    assert res["status"] == "posted" and res["event"] == "COMMENT"
    posts = _posts(gh)
    assert len(posts) == 2
    assert posts[0][2]["event"] == "REQUEST_CHANGES"
    assert posts[1][2]["event"] == "COMMENT"
    assert "REQUEST_CHANGES downgraded to COMMENT — 403" in posts[1][2]["body"]


def test_review_post_failure_is_reported_not_raised(monkeypatch):
    gh = FakeGH()
    _review_gh(gh)
    gh.add("POST", PULL_REVIEWS, (403, None))
    monkeypatch.setattr(argus, "_gh", gh)
    res = argus.post_review(
        _report(reviewEvent="request_changes", reviewComments=[_comment()]),
        PR, "tok",
    )
    # RC → 403 → COMMENT → 403 → stop: bounded ladder, failure narrated.
    assert res["status"] == "failed" and "403" in res["reason"]
    assert "Pull requests read+write" in res["reason"]
    assert len(_posts(gh)) == 2


def test_review_dismisses_stale_self_reviews_only(monkeypatch):
    sentinel_body = f"{argus.SENTINEL}\n**argus-reviewer** — verdict **needs_changes**"
    reviews = [
        # stale self review → dismissed
        {"id": 11, "state": "CHANGES_REQUESTED",
         "user": {"login": "argus-bot"}, "body": sentinel_body},
        # foreign review by another user → untouched even with sentinel text
        {"id": 12, "state": "CHANGES_REQUESTED",
         "user": {"login": "github-actions[bot]"}, "body": sentinel_body},
        # same login but a real hand-written body → never dismissed
        {"id": 13, "state": "CHANGES_REQUESTED",
         "user": {"login": "argus-bot"}, "body": "blocking: do not merge"},
        # self but not a blocking state → untouched
        {"id": 14, "state": "COMMENTED",
         "user": {"login": "argus-bot"}, "body": sentinel_body},
    ]
    gh = FakeGH()
    _review_gh(gh, reviews=reviews)
    gh.add("PUT", PULL_REVIEWS, (200, {"id": 11}))
    gh.add("POST", PULL_REVIEWS, (200, {"html_url": "https://x/r"}))
    monkeypatch.setattr(argus, "_gh", gh)
    res = argus.post_review(
        _report(reviewEvent="request_changes", reviewComments=[_comment()]),
        PR, "tok",
    )
    assert res["status"] == "posted" and res["dismissed"] == 1
    dismissals = [c for c in gh.calls if c[0] == "PUT"]
    assert len(dismissals) == 1 and "/reviews/11/dismissals" in dismissals[0][1]
    # dismissal happens before the new review posts
    kinds = [(c[0], "dismiss" if c[0] == "PUT" else "post")
             for c in gh.calls if c[0] in ("PUT", "POST")]
    assert kinds[0][1] == "dismiss"


def test_review_dismissal_skipped_when_login_unresolvable(monkeypatch):
    """/user 403 → we can't tell our reviews from anyone's — dismiss nothing."""
    gh = FakeGH()
    _review_gh(gh, reviews=[
        {"id": 11, "state": "CHANGES_REQUESTED",
         "user": {"login": "argus-bot"}, "body": argus.SENTINEL},
    ], login=None)
    gh.routes[("GET", USER)] = [(403, None)]
    gh.add("POST", PULL_REVIEWS, (200, {"html_url": "https://x/r"}))
    monkeypatch.setattr(argus, "_gh", gh)
    res = argus.post_review(
        _report(reviewEvent="request_changes", reviewComments=[_comment()]),
        PR, "tok",
    )
    assert res["status"] == "posted" and res["dismissed"] == 0
    assert res["warnings"]
    assert [c for c in gh.calls if c[0] == "PUT"] == []


def test_review_comment_event_with_nothing_new_posts_nothing(monkeypatch):
    gh = FakeGH()
    _review_gh(gh)
    monkeypatch.setattr(argus, "_gh", gh)
    res = argus.post_review(_report(reviewComments=[]), PR, "tok")
    assert res["status"] == "skipped" and res["reason"] == "no new inline comments"
    assert _posts(gh) == []


def test_review_request_changes_posts_even_with_zero_comments(monkeypatch):
    gh = FakeGH()
    _review_gh(gh)
    gh.add("POST", PULL_REVIEWS, (200, {"html_url": "https://x/r"}))
    monkeypatch.setattr(argus, "_gh", gh)
    res = argus.post_review(
        _report(reviewEvent="request_changes", provenBlockers=1), PR, "tok")
    assert res["status"] == "posted" and res["event"] == "REQUEST_CHANGES"
    assert _posts(gh)[0][2]["comments"] == []


def test_review_list_failure_skips_post(monkeypatch):
    gh = FakeGH()
    _review_gh(gh)
    gh.routes[("GET", PULL_COMMENTS)] = [(500, None)]
    monkeypatch.setattr(argus, "_gh", gh)
    res = argus.post_review(_report(reviewComments=[_comment()]), PR, "tok")
    assert res["status"] == "skipped" and _posts(gh) == []


def test_narrate_review_surfaces_review_result(tmp_path):
    report = _report()
    msg = argus.narrate_review(
        report, str(tmp_path),
        posted=("https://x/1", "created"),
        review={"status": "posted", "event": "REQUEST_CHANGES", "comments": 2,
                "deduped": 1, "dropped": 0, "overflow": 3, "dismissed": 1},
    )
    assert "review posted (REQUEST_CHANGES): 2 inline comment(s)" in msg
    assert "1 already posted" in msg and "3 over cap" in msg
    assert "1 stale review(s) dismissed" in msg


def test_narrate_review_surfaces_skip_and_failure(tmp_path):
    report = _report()
    msg = argus.narrate_review(
        report, str(tmp_path), review={"status": "skipped", "reason": "stale head"})
    assert "review skipped: stale head" in msg
    msg = argus.narrate_review(
        report, str(tmp_path), review={"status": "failed", "reason": "boom (403)"})
    assert "review post failed: boom (403)" in msg


def test_version_pin_tracks_serialized_review_schema():
    """Pin bumps are deliberate (AGENTS.md): 0.3.0 is the first argus release
    serializing reviewEvent/reviewComments/headBinding.intendedSha into
    code-review.json — the contract post_review consumes. Held at 0.2.0
    until 0.3.0 is published; fresh installs would fail vendoring otherwise.
    Update this assertion with the pin."""
    assert argus.load_default_config()["argus_version_pin"] == "0.2.0"
