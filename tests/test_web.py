from datetime import UTC, datetime

from starlette.testclient import TestClient

from containertldr.__main__ import write_heartbeat, write_last_run
from containertldr.config import Config
from containertldr.pipeline import RunResult
from containertldr.state import Entry, State, state_key
from containertldr.web import create_app

RADARR = "lscr.io/linuxserver/radarr:latest"
FOOTER = "Summarised by qwen3:8b from 1 release note"


def client(tmp_path, **config):
    return TestClient(create_app(Config(notify_urls=["discord://1/a"], data_dir=tmp_path, **config)))


def sent(title, summary="**Verdict:** Safe to update", link=None):
    return {"title": title, "summary": summary, "link": link, "footer": FOOTER}


def write_state(tmp_path, *entries):
    """entries are (ref, at, message) for images reported or baselined while running sha256:old."""
    images = {state_key(ref, "sha256:old"): Entry("sha256:new", None, at, message) for ref, at, message in entries}
    State(tmp_path / "state.json", images, first_run=False).save()


def test_status_before_the_first_check(tmp_path):
    page = client(tmp_path).get("/")
    assert page.status_code == 200
    assert "None has finished yet" in page.text
    assert "Not scheduled yet" in page.text
    assert "Nothing. A reported update shows here" in page.text


def test_status_after_a_check(tmp_path):
    result = RunResult(updates=1, baselined=2, errors=["redis: docker.io rate limit reached"])
    write_last_run(tmp_path, datetime.now(UTC), result)
    write_heartbeat(tmp_path, datetime(2026, 10, 21, 3, 30, tzinfo=UTC))
    page = client(tmp_path, tz="Europe/London").get("/")
    assert "<dt>Updates</dt><dd>1</dd>" in page.text
    assert "<dt>Baselined</dt><dd>2</dd>" in page.text
    assert "<li>redis: docker.io rate limit reached</li>" in page.text
    assert "<dd>2026-10-21 04:30</dd>" in page.text  # in the configured timezone
    assert "<code>30 4 * * *</code>, Europe/London" in page.text


def test_pending_updates_newest_first(tmp_path):
    release = "https://github.com/Radarr/Radarr/releases/tag/v6.4.5.10720"
    write_state(
        tmp_path,
        (RADARR, "2026-10-18T04:31:00+00:00", sent("⬆️ radarr: 6.4.4 → 6.4.5", link=release)),
        ("jellyfin/jellyfin:latest", "2026-10-20T04:31:00+00:00", sent("⬆️ jellyfin: 10.11.1 → 10.11.2")),
        ("redis:8", "2026-10-19T04:31:00+00:00", None),  # baselined, so nothing was sent
    )
    page = client(tmp_path).get("/").text
    assert page.index("jellyfin: 10.11.1") < page.index("radarr: 6.4.4")
    assert f'<a href="{release}">⬆️ radarr: 6.4.4 → 6.4.5</a>' in page
    assert "redis" not in page
    assert page.count(FOOTER) == 2 and page.count("**Verdict:** Safe to update") == 2


def test_summaries_are_shown_as_text(tmp_path):
    summary = '<script>alert("from the release notes")</script>'
    write_state(tmp_path, ("ghcr.io/someone/app:latest", "2026-10-20T04:31:00+00:00", sent("⬆️ app: 1 → 2", summary)))
    page = client(tmp_path).get("/").text
    assert "<script>" not in page
    assert "&lt;script&gt;" in page


def test_stylesheet(tmp_path):
    response = client(tmp_path).get("/static/style.css")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/css")
