import time
from datetime import UTC, datetime, timedelta

import httpx

from .releases import Release

# Releases are fetched back to the running image's build date, minus this margin for images that were
# built a while after the release they contain
LOOKBACK = timedelta(days=30)
MAX_PAGES = 10
MAX_COMMITS = 100


class GitHubError(Exception):
    pass


class GitHub:
    def __init__(
        self,
        token: str | None = None,
        url: str = "https://api.github.com",
        transport: httpx.BaseTransport | None = None,
    ):
        headers = {
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "containertldr",
        }
        if token:
            headers["Authorization"] = f"Bearer {token}"
        self.http = httpx.Client(base_url=url, headers=headers, timeout=30, transport=transport)
        self.limited_until: datetime | None = None
        self._releases: dict[tuple[str, datetime], list[Release]] = {}

    def releases(self, repo: str, since: datetime) -> list[Release]:
        """Published releases of repo, newest first, going back far enough to cover since. Cached for the run.

        Paging stops on dates rather than versions: repos with parallel branches (redis 8.8 and 8.10) mix
        old versions in among new releases.
        """
        key = (repo, since)
        if key not in self._releases:
            found = []
            url = f"/repos/{repo}/releases?per_page=100"
            for _ in range(MAX_PAGES):
                r = self._get(url)
                page = [_release(d) for d in r.json() if not d["draft"]]
                found += page
                url = r.links.get("next", {}).get("url")
                if not url or (page and min(p.published for p in page) < since - LOOKBACK):
                    break
            self._releases[key] = found
        return self._releases[key]

    def compare(self, repo: str, base: str, head: str) -> Release:
        """Commits between two revisions as one release, for images built from a branch."""
        data = self._get(f"/repos/{repo}/compare/{base}...{head}").json()
        commits = data["commits"][-MAX_COMMITS:]  # oldest first, so this keeps the newest
        body = "\n".join("- " + c["commit"]["message"].partition("\n")[0] for c in commits)
        published = datetime.fromisoformat(commits[-1]["commit"]["committer"]["date"]) if commits else datetime.now(UTC)
        return Release(f"{base[:7]}...{head[:7]}", published, body, data["html_url"])

    def _get(self, url: str) -> httpx.Response:
        if self.limited_until and datetime.now(UTC) < self.limited_until:
            raise GitHubError(_limit_message(self.limited_until))
        r = self.http.get(url)
        retry_after = r.headers.get("Retry-After", "")
        if r.status_code in (403, 429) and retry_after.isdigit() and int(retry_after) <= 60:  # secondary limit
            time.sleep(int(retry_after))
            r = self.http.get(url)

        if r.headers.get("X-RateLimit-Remaining") == "0":
            self.limited_until = datetime.fromtimestamp(int(r.headers["X-RateLimit-Reset"]), UTC)
            if r.status_code in (403, 429):
                raise GitHubError(_limit_message(self.limited_until))
        if r.status_code in (403, 429):
            raise GitHubError(f"GitHub refused {url}: {r.text[:200]}")
        if r.status_code == 404:
            raise GitHubError(f"not found on GitHub: {url}")
        r.raise_for_status()
        return r


def _release(d: dict) -> Release:
    published = datetime.fromisoformat(d["published_at"])
    return Release(d["tag_name"], published, d["body"] or "", d["html_url"], d["prerelease"])


def _limit_message(until: datetime) -> str:
    return f"GitHub rate limit reached until {until:%H:%M} UTC, set GITHUB_TOKEN for a higher limit"
