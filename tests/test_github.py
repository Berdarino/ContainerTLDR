import json
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest

from containertldr.github import LOOKBACK, GitHub, GitHubError

FIXTURES = Path(__file__).parent / "fixtures" / "github"
PAPRA = json.loads((FIXTURES / "papra_releases.json").read_text())  # recorded 2026-10-19, 30 releases
SINCE = datetime(2025, 1, 1, tzinfo=UTC)


def paged(releases, requests, per_page=10):
    """Serve a release list in pages with Link headers, the way GitHub does."""

    def handler(request):
        requests.append(request)
        page = int(request.url.params.get("page", "1"))
        headers = {}
        if page * per_page < len(releases):
            headers["Link"] = f'<{request.url.copy_set_param("page", page + 1)}>; rel="next"'
        return httpx.Response(200, json=releases[(page - 1) * per_page : page * per_page], headers=headers)

    return httpx.MockTransport(handler)


def responding(*responses, requests=None):
    """Answer with the given responses in order."""
    queue = list(responses)

    def handler(request):
        if requests is not None:
            requests.append(request)
        return queue.pop(0)

    return httpx.MockTransport(handler)


def published(d):
    return datetime.fromisoformat(d["published_at"])


def test_releases_from_recorded_response():
    requests = []
    releases = GitHub(transport=paged(PAPRA, requests)).releases("papra-hq/papra", SINCE)

    assert (len(releases), len(requests)) == (30, 3)
    assert requests[0].url.path == "/repos/papra-hq/papra/releases"
    app = next(r for r in releases if r.tag == "@papra/app@26.7.0")
    assert app.published == datetime(2026, 10, 1, 21, 1, 22, tzinfo=UTC)
    assert app.url == "https://github.com/papra-hq/papra/releases/tag/%40papra/app%4026.7.0"
    assert "API breaking change" in app.body
    assert not app.prerelease


@pytest.mark.parametrize("days, pages", [(1, 1), (-1, 2)])
def test_stops_paging_once_past_since(days, pages):
    # since - LOOKBACK falls a day after (or before) the oldest release on the first page
    since = min(published(d) for d in PAPRA[:10]) + LOOKBACK + timedelta(days)
    requests = []
    GitHub(transport=paged(PAPRA, requests)).releases("papra-hq/papra", since)
    assert len(requests) == pages


def test_skips_drafts():
    draft = {**PAPRA[0], "tag_name": "@papra/app@26.8.0", "draft": True, "published_at": None}
    releases = GitHub(transport=paged([draft, *PAPRA[:5]], [])).releases("papra-hq/papra", SINCE)
    assert [r.tag for r in releases] == [d["tag_name"] for d in PAPRA[:5]]


def test_caches_release_lists_for_the_run():
    requests = []
    gh = GitHub(transport=paged(PAPRA, requests))
    assert gh.releases("papra-hq/papra", SINCE) == gh.releases("papra-hq/papra", SINCE)
    assert len(requests) == 3


def test_sends_token_only_when_set():
    requests = []
    GitHub("ghp_test", transport=paged(PAPRA[:1], requests)).releases("papra-hq/papra", SINCE)
    GitHub(transport=paged(PAPRA[:1], requests)).releases("papra-hq/papra", SINCE)
    assert requests[0].headers["Authorization"] == "Bearer ghp_test"
    assert "Authorization" not in requests[1].headers


def limit_headers():
    return {"X-RateLimit-Remaining": "0", "X-RateLimit-Reset": str(int(time.time()) + 600)}


def test_rate_limit_fails_fast_until_reset():
    requests = []
    limited = httpx.Response(403, json={"message": "API rate limit exceeded"}, headers=limit_headers())
    gh = GitHub(transport=responding(limited, requests=requests))
    with pytest.raises(GitHubError, match="rate limit"):
        gh.releases("papra-hq/papra", SINCE)
    with pytest.raises(GitHubError, match="rate limit"):
        gh.releases("redis/redis", SINCE)
    assert len(requests) == 1


def test_using_the_last_request_marks_the_limit():
    requests = []
    gh = GitHub(transport=responding(httpx.Response(200, json=PAPRA[:1], headers=limit_headers()), requests=requests))
    assert len(gh.releases("papra-hq/papra", SINCE)) == 1
    with pytest.raises(GitHubError, match="rate limit"):
        gh.releases("redis/redis", SINCE)
    assert len(requests) == 1


def test_waits_out_a_short_retry_after(monkeypatch):
    slept = []
    monkeypatch.setattr("containertldr.github.time.sleep", slept.append)
    gh = GitHub(transport=responding(httpx.Response(429, headers={"Retry-After": "2"}), httpx.Response(200, json=[])))
    assert gh.releases("papra-hq/papra", SINCE) == []
    assert slept == [2]


def test_unknown_repo():
    gh = GitHub(transport=responding(httpx.Response(404, json={"message": "Not Found"})))
    with pytest.raises(GitHubError, match="not found"):
        gh.releases("nobody/nothing", SINCE)


def test_compare_as_one_release():
    data = json.loads((FIXTURES / "caddy_tailscale_compare.json").read_text())
    base, head = "6911d07a13eca7605ea1c0ec985d858cf2408393", "de41b249af4fd2083612c3b2303197e92787d1c5"
    requests = []
    release = GitHub(transport=responding(httpx.Response(200, json=data), requests=requests)).compare(
        "tailscale/caddy-tailscale", base, head
    )

    assert requests[0].url.path == f"/repos/tailscale/caddy-tailscale/compare/{base}...{head}"
    assert (release.tag, release.url) == ("6911d07...de41b24", data["html_url"])
    assert release.published == datetime(2026, 8, 26, 18, 3, 4, tzinfo=UTC)
    lines = release.body.splitlines()
    assert len(lines) == 7
    assert lines[0] == "- Allow AdvertiseTags to be passed to tsnet.Server"
    assert lines[-1] == "- Update Tailscale 1.90.9 -> 1.102.2 (#138)"


def test_compare_keeps_the_newest_commits():
    commits = [
        {"commit": {"message": f"commit {i}\n\ndetails", "committer": {"date": "2026-10-01T00:00Z"}}}
        for i in range(150)
    ]
    gh = GitHub(transport=responding(httpx.Response(200, json={"html_url": "", "commits": commits})))
    lines = gh.compare("owner/repo", "a" * 40, "b" * 40).body.splitlines()
    assert (len(lines), lines[0], lines[-1]) == (100, "- commit 50", "- commit 149")
