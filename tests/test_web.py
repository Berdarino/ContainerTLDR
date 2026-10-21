import html
import json
import re
import threading
import time
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest
from starlette.testclient import TestClient

from containertldr import web
from containertldr.__main__ import write_heartbeat, write_last_run
from containertldr.config import Config, load, read_fixes, read_settings
from containertldr.discovery import ImageGroup, LocalImage
from containertldr.pipeline import RunResult
from containertldr.registry import parse_ref
from containertldr.state import Entry, State, state_key
from containertldr.web import create_app
from containertldr.web.hooks import Hooks
from containertldr.web.jobs import Failed, Jobs, try_github, try_llm, try_notify, try_summary
from containertldr.web.markdown import markdown

HOOKS = Path(__file__).parent / "fixtures" / "hooks"  # shaped like the senders' documented examples
RADARR = "lscr.io/linuxserver/radarr:latest"
VERSION = "org.opencontainers.image.version"
FOOTER = "Summarised by qwen3:8b from 1 release note"
PASSWORD = "correct horse battery staple"


class FakeDocker:
    def __init__(self, groups):
        self.groups, self.filters = groups, []

    def running(self, include=None, exclude=None):
        self.filters.append((include, exclude))
        if isinstance(self.groups, Exception):
            raise self.groups
        return self.groups


def group(ref, image_labels=None, container_labels=None, env=None):
    """A container named after its image, running image sha256:old."""
    image = parse_ref(ref)
    config = {"Labels": image_labels or {}, "Env": env or []}
    local = LocalImage("sha256:old", frozenset({"sha256:d"}), config, datetime(2026, 9, 20, tzinfo=UTC), "linux/amd64")
    return ImageGroup(ref, image, local, [image.name.rsplit("/", 1)[-1]], ["media"], container_labels or {})


def client(tmp_path, docker=None, env=None, **config):
    config = Config(notify_urls=["discord://1/a"], data_dir=tmp_path, **config)
    docker = docker or FakeDocker([])
    return TestClient(create_app(config, lambda host: docker, env or {}))


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
    assert page.count(FOOTER) == 2 and page.count("<strong>Verdict:</strong> Safe to update") == 2


def test_summaries_are_shown_as_text(tmp_path):
    summary = '**Highlights:** <script>alert("from the release notes")</script>\n- <img src=x onerror=alert(1)>'
    write_state(tmp_path, ("ghcr.io/someone/app:latest", "2026-10-20T04:31:00+00:00", sent("⬆️ app: 1 → 2", summary)))
    page = client(tmp_path).get("/").text
    assert "<script>" not in page and "<img" not in page
    assert "<strong>Highlights:</strong> &lt;script&gt;" in page


def test_markdown_in_summaries():
    summary = "**Breaking:** None mentioned  \n**Highlights:**\n- Added a dark theme\n* Fixed a crash\n\n…"
    assert markdown(summary) == (
        "<p><strong>Breaking:</strong> None mentioned</p>\n"
        "<p><strong>Highlights:</strong></p>\n"
        "<ul>\n<li>Added a dark theme</li>\n<li>Fixed a crash</li>\n</ul>\n"
        "<p>…</p>"
    )


def test_containers(tmp_path):
    docker = FakeDocker(
        [
            group(RADARR, {VERSION: "6.4.4.10685-ls318"}),
            group("redis:8", env=["REDIS_VERSION=8.2.2"]),
            group("postgres:18", env=["PG_VERSION=18.0"]),
            group("ghcr.io/someone/app:latest", {VERSION: "2.1.0"}, {"containertldr.repo": "someone/app-server"}),
        ]
    )
    page = client(tmp_path, docker).get("/containers").text
    assert '<a href="https://github.com/Radarr/Radarr">Radarr/Radarr</a>' in page
    assert "6.4.4.10685-ls318" in page and "8.2.2" in page and "env:REDIS_VERSION" in page
    assert '<a href="https://www.postgresql.org/docs/release/">' in page  # no GitHub repo, but a changelog
    assert "<code>containertldr.repo=someone/app-server</code>" in page
    assert 'class="fix"' not in page
    assert page.index("someone/app:latest") < page.index("postgres:18") < page.index(RADARR)  # by container


def test_containers_with_a_fix(tmp_path):
    fixes = {"ghcr.io/someone/app": {"repo": "someone/app-server"}}
    (tmp_path / "settings.json").write_text(json.dumps({"version": 1, "images": fixes}))
    docker = FakeDocker([group("ghcr.io/someone/app:latest", {VERSION: "2.1.0"})])
    page = client(tmp_path, docker).get("/containers").text
    assert '<a href="https://github.com/someone/app-server">someone/app-server</a>' in page
    assert "fixed in the web UI" in page and 'class="fix"' not in page  # no longer a guess


def test_rows_that_probably_need_a_label(tmp_path):
    docker = FakeDocker(
        [
            group("ghcr.io/someone/app:latest", {VERSION: "2.1.0"}),
            group("someone/tool:latest", {"org.opencontainers.image.source": "https://github.com/someone/tool"}),
            group("someone/thing:latest", {VERSION: "1.0"}),
            group(RADARR, {VERSION: "6.4.4.10685-ls318"}, {"containertldr.rep": "Radarr/Radarr"}),
        ]
    )
    page = client(tmp_path, docker).get("/containers").text
    assert page.count('<tr class="fix">') == 4
    assert "guessed from the ghcr.io path" in page and "guessed from the image" in page
    assert page.count("None found") == 2  # tool's version, thing's repo
    assert "unknown label containertldr.rep," in page


def test_newer_images_come_from_the_state(tmp_path):
    write_state(
        tmp_path,
        (RADARR, "2026-10-20T04:31:00+00:00", sent("⬆️ radarr: 6.4.4 → 6.4.5")),
        ("redis:8", "2026-10-20T04:31:00+00:00", None),
    )
    groups = [group(RADARR), group("redis:8"), group("postgres:18")]
    page = client(tmp_path, FakeDocker(groups)).get("/containers").text
    assert '<a href="/">Update waiting</a>' in page
    assert page.count("Yes") == 1


def test_no_containers(tmp_path):
    assert "No running containers to check." in client(tmp_path).get("/containers").text


def test_docker_unreachable(tmp_path):
    docker = FakeDocker(httpx.ConnectError("[Errno 2] No such file or directory"))
    page = client(tmp_path, docker).get("/containers")
    assert page.status_code == 200
    assert "reach Docker at unix:///var/run/docker.sock: [Errno 2] No such file or directory" in page.text


def test_stylesheet(tmp_path):
    response = client(tmp_path).get("/static/style.css")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/css")


def test_health_for_uptime_monitors(tmp_path):
    c = client(tmp_path, web_password=PASSWORD)  # open even with a login
    assert (c.get("/health").status_code, c.get("/health").text) == (503, "no heartbeat yet")
    write_heartbeat(tmp_path, datetime.now(UTC) + timedelta(hours=1))
    assert (c.get("/health").status_code, c.get("/health").text) == (200, "ok")
    write_heartbeat(tmp_path, datetime.now(UTC) - timedelta(hours=3))
    assert c.get("/health").status_code == 503


TOKEN = "hook-token"


def hooked(monkeypatch):
    """The images webhook checks were run for, with a Checker that only writes them down."""
    images = []

    class FakeChecker:
        @classmethod
        def from_config(cls, config, dry_run=False):
            return cls()

        def run(self, image=None):
            images.append(image)
            return RunResult()

    monkeypatch.setattr(web, "Checker", FakeChecker)
    return images


def wait_for(items, count):
    for _ in range(500):
        if len(items) >= count:
            return items
        time.sleep(0.01)
    raise AssertionError(f"expected {count}, got {items}")


def test_webhooks_need_a_token(tmp_path, monkeypatch):
    hooked(monkeypatch)
    assert client(tmp_path).post("/hooks/generic", json={"image": RADARR}).status_code == 404  # no token, no hooks
    c = client(tmp_path, webhook_token=TOKEN, web_password=PASSWORD)  # a token instead of the login
    assert c.post("/hooks/generic", json={"image": RADARR}).status_code == 401
    assert c.post("/hooks/generic?token=a-guess", json={"image": RADARR}).status_code == 401


def test_the_generic_webhook(tmp_path, monkeypatch):
    images = hooked(monkeypatch)
    c = client(tmp_path, webhook_token=TOKEN)
    response = c.post("/hooks/generic", json={"image": RADARR}, headers={"Authorization": f"Bearer {TOKEN}"})
    assert (response.status_code, response.text) == (202, f"Checking {RADARR}.")
    assert c.post(f"/hooks/generic?token={TOKEN}", json={"image": "redis:8"}).status_code == 202
    assert wait_for(images, 2) == [RADARR, "redis:8"]
    assert c.post(f"/hooks/generic?token={TOKEN}", json={"name": "redis"}).status_code == 400
    assert c.post(f"/hooks/generic?token={TOKEN}", content="not JSON").status_code == 400
    assert c.post(f"/hooks/nobody?token={TOKEN}", json={"image": RADARR}).status_code == 404


def test_diun_and_wud_webhooks(tmp_path, monkeypatch):
    images = hooked(monkeypatch)
    c = client(tmp_path, webhook_token=TOKEN)
    bearer = {"Authorization": f"Bearer {TOKEN}"}
    diun = json.loads((HOOKS / "diun.json").read_text())
    assert c.post("/hooks/diun", json=diun, headers=bearer).status_code == 202
    wud = json.loads((HOOKS / "wud.json").read_text())
    assert c.post("/hooks/wud", json=wud[1], headers=bearer).status_code == 202  # one container
    assert c.post("/hooks/wud", json=wud, headers=bearer).status_code == 202  # batch mode
    expected = ["docker.io/jellyfin/jellyfin:latest", "lscr.io/linuxserver/radarr:latest"]
    expected += ["homeassistant/home-assistant:2024.6.4", "lscr.io/linuxserver/radarr:latest", "library/redis:8"]
    assert wait_for(images, 5) == expected
    assert c.post("/hooks/wud", json={"rollback": True}, headers=bearer).status_code == 400


def test_webhook_checks_queue_up():
    checked, go = [], threading.Event()

    def check(image):
        checked.append(image)
        if image == "broken":
            raise RuntimeError("Docker went away")
        go.wait(5)

    hooks = Hooks(check)
    hooks.add("redis:8")
    wait_for(checked, 1)  # running now, so it's no longer waiting
    assert hooks.add("jellyfin/jellyfin:latest") and not hooks.add("jellyfin/jellyfin:latest")
    assert hooks.add("redis:8")  # again: a newer image may have come out since its check started
    go.set()
    hooks.add("broken")
    hooks.add(RADARR)
    assert wait_for(checked, 5) == ["redis:8", "jellyfin/jellyfin:latest", "redis:8", "broken", RADARR]


def csrf(page):
    return re.search(r'name="csrf" value="([^"]+)"', page.text)[1]


def logged_in(tmp_path, **config):
    c = client(tmp_path, web_password=PASSWORD, **config)
    c.post("/login", data={"csrf": csrf(c.get("/login")), "password": PASSWORD})
    return c


def test_without_a_password_there_is_no_login(tmp_path):
    c = client(tmp_path)
    page = c.get("/")
    assert page.status_code == 200 and "set-cookie" not in page.headers
    assert c.get("/login").status_code == 404


def test_with_a_password_every_page_needs_a_login(tmp_path):
    c = client(tmp_path, web_password=PASSWORD)
    for path in ("/", "/containers"):
        response = c.get(path, follow_redirects=False)
        assert (response.status_code, response.headers["location"]) == (303, "/login")
    assert c.get("/static/style.css").status_code == 200
    assert "Log out" not in c.get("/login").text


def test_login_and_logout(tmp_path):
    c = client(tmp_path, web_password=PASSWORD)
    response = c.post("/login", data={"csrf": csrf(c.get("/login")), "password": PASSWORD}, follow_redirects=False)
    assert (response.status_code, response.headers["location"]) == (303, "/")
    cookie = response.headers["set-cookie"].lower()
    assert cookie.startswith("containertldr=") and "httponly" in cookie and "samesite=strict" in cookie
    page = c.get("/")
    assert page.status_code == 200 and "Log out" in page.text and PASSWORD not in page.text
    c.post("/logout", data={"csrf": csrf(page)})
    assert c.get("/", follow_redirects=False).status_code == 303


def test_wrong_password(tmp_path, monkeypatch):
    monkeypatch.setattr(web, "FAILED_LOGIN_DELAY", 0)
    c = client(tmp_path, web_password=PASSWORD)
    response = c.post("/login", data={"csrf": csrf(c.get("/login")), "password": "hunter2"})
    assert response.status_code == 401 and "Wrong password." in response.text
    assert c.get("/", follow_redirects=False).status_code == 303


def test_forms_from_elsewhere_are_refused(tmp_path):
    c = client(tmp_path, web_password=PASSWORD)
    token = csrf(c.get("/login"))
    assert c.post("/login", data={"password": PASSWORD}).status_code == 403
    assert c.post("/login", data={"csrf": "a guess", "password": PASSWORD}).status_code == 403
    form = {"csrf": token, "password": PASSWORD}
    assert c.post("/login", data=form, headers={"origin": "https://evil.example"}).status_code == 403
    proxied = {"origin": "https://tldr.example", "x-forwarded-host": "tldr.example"}
    assert c.post("/login", data=form, headers=proxied, follow_redirects=False).status_code == 303


def environment(tmp_path):
    return {
        "DOCKER_CONFIG": str(tmp_path),  # an empty folder, so the developer's own Docker logins stay out of it
        "CTLDR_NOTIFY_URLS": "discord://123/AbC-token",
        "CTLDR_LLM_URL": "http://ollama.lan:11434",
        "TZ": "Europe/London",
        "GITHUB_TOKEN": "ghp_secret",
    }


def save(data_dir, **settings):
    (data_dir / "settings.json").write_text(json.dumps({"version": 1, "settings": settings}))


def setting(page, variable):
    """The text of one row on the settings page."""
    row = re.search(rf"<code>{variable}</code>.*?</tr>", page, re.S)[0]
    return " ".join(html.unescape(re.sub(r"<[^>]+>", " ", row)).split())


def test_settings_need_a_password_first(tmp_path):
    c = client(tmp_path, env=environment(tmp_path))
    page = c.get("/settings").text
    assert "once a password is set with <code>CTLDR_WEB_PASSWORD</code>" in page
    assert "ollama.lan" not in page
    c.post("/settings", data={"llm_model": "llama3.2"})
    assert read_settings(tmp_path) == {}
    assert c.get("/settings/test").status_code == 404


def test_the_settings_form(tmp_path):
    save(tmp_path, llm_model="llama3.2", llm_provider="openai_compat", llm_api_key="sk-saved")
    page = logged_in(tmp_path, env=environment(tmp_path)).get("/settings").text
    assert setting(page, "CTLDR_LLM_URL").endswith("http://ollama.lan:11434 set by CTLDR_LLM_URL")
    assert setting(page, "CTLDR_TZ").endswith("Europe/London set by TZ")
    assert setting(page, "GITHUB_TOKEN").endswith("set set by GITHUB_TOKEN")
    assert 'name="llm_url"' not in page  # set by the environment, so there's nothing to change here
    assert 'name="llm_model" value="llama3.2" placeholder="qwen3:8b"' in page
    assert 'name="schedule" value="" placeholder="30 4 * * *"' in page
    assert "<option selected>openai_compat</option>" in page
    assert 'placeholder="saved, type to replace"' in page and 'name="clear_llm_api_key"' in page
    assert not any(secret in page for secret in ("AbC-token", "ghp_secret", "sk-saved", PASSWORD))


def test_saving_settings(tmp_path):
    env = environment(tmp_path)
    c = logged_in(tmp_path, env=env)
    form = {"llm_model": "llama3.2", "llm_provider": "none", "schedule": "0 3 * * *", "llm_url": "http://elsewhere"}
    response = c.post("/settings", data={"csrf": csrf(c.get("/settings")), **form}, follow_redirects=False)
    assert (response.status_code, response.headers["location"]) == (303, "/settings?saved")
    # llm_url is set by the environment, so the form can't change it
    assert read_settings(tmp_path) == {"llm_model": "llama3.2", "llm_provider": "none", "schedule": "0 3 * * *"}
    assert load(env, read_settings(tmp_path)).llm_model == "llama3.2"
    assert oct((tmp_path / "settings.json").stat().st_mode & 0o777) == "0o600"
    assert '<p class="ok">Saved.</p>' in c.get("/settings?saved").text
    assert c.post("/settings", data=form).status_code == 403  # no token


def test_invalid_settings_are_not_saved(tmp_path):
    save(tmp_path, llm_model="llama3.2")
    c = logged_in(tmp_path, env=environment(tmp_path))
    form = {"llm_model": "mistral", "llm_num_ctx": "8k", "schedule": "daily", "llm_api_key": "sk-typed"}
    response = c.post("/settings", data={"csrf": csrf(c.get("/settings")), **form})
    assert response.status_code == 400
    assert read_settings(tmp_path) == {"llm_model": "llama3.2"}
    assert setting(response.text, "CTLDR_LLM_NUM_CTX").endswith("should be a positive whole number, not '8k'")
    assert "a cron expression like '30 4 * * *', not 'daily'" in setting(response.text, "CTLDR_SCHEDULE")
    assert 'name="llm_model" value="mistral"' in response.text  # what was typed stays
    assert "sk-typed" not in response.text


def test_secrets_are_write_only(tmp_path):
    c = logged_in(tmp_path, env=environment(tmp_path))
    token = csrf(c.get("/settings"))
    c.post("/settings", data={"csrf": token, "llm_api_key": "sk-new"})
    c.post("/settings", data={"csrf": token, "llm_api_key": ""})  # left empty, so it stays
    assert read_settings(tmp_path) == {"llm_api_key": "sk-new"}
    assert not any("sk-new" in c.get(path).text for path in ("/", "/containers", "/settings", "/login"))
    c.post("/settings", data={"csrf": token, "clear_llm_api_key": "on"})
    assert read_settings(tmp_path) == {}


def test_pages_go_by_the_settings_in_use(tmp_path):
    save(tmp_path, schedule="0 3 * * *", docker_host="tcp://socket-proxy:2375", include_label="tldr")
    hosts, docker = [], FakeDocker([])

    def connect(host):
        hosts.append(host)
        return docker

    c = TestClient(create_app(Config(notify_urls=["x"], data_dir=tmp_path), connect, environment(tmp_path)))
    assert "<code>0 3 * * *</code>, Europe/London" in c.get("/").text
    c.get("/containers")
    c.get("/containers")
    assert hosts == ["tcp://socket-proxy:2375"]  # one client, kept while the host stays the same
    assert docker.filters == [("tldr", None), ("tldr", None)]


def finished(jobs):
    for _ in range(500):
        if jobs.last.done:
            return jobs.last
        time.sleep(0.01)
    raise AssertionError("the test didn't finish")


def test_one_test_at_a_time():
    jobs, go = Jobs(), threading.Event()
    jobs.start("Slow", lambda: "done" if go.wait(5) else "timed out", secrets=[])
    jobs.start("Impatient", lambda: "ran", secrets=[])
    assert (jobs.last.title, jobs.last.done) == ("Slow", False)
    go.set()
    assert finished(jobs).result == "done"


def test_secrets_are_hidden_in_errors():
    webhook = "https://discord.com/api/webhooks/123/AbC-token"

    def leaky():
        raise RuntimeError(f"POST {webhook} failed")

    jobs = Jobs()
    jobs.start("Notifications", leaky, secrets=[webhook])
    assert finished(jobs).error == "RuntimeError: POST [hidden] failed"


def result_page(c):
    """The test page, once its test has finished."""
    for _ in range(500):
        if "This page reloads" not in (page := c.get("/settings/test").text):
            return page
        time.sleep(0.01)
    raise AssertionError("the test didn't finish")


def test_testing_docker_from_the_settings_page(tmp_path):
    docker = FakeDocker([group(RADARR), group("redis:8")])
    c = logged_in(tmp_path, docker=docker, env=environment(tmp_path))
    token = csrf(c.get("/settings"))
    response = c.post("/settings/test", data={"csrf": token, "test": "docker"}, follow_redirects=False)
    assert (response.status_code, response.headers["location"]) == (303, "/settings/test")
    page = result_page(c)
    assert '<p class="ok">Docker: OK' in page and "2 running containers to check, on 2 images." in page
    assert c.post("/settings/test", data={"csrf": token, "test": "everything"}).status_code == 400


def test_testing_notifications(monkeypatch):
    monkeypatch.setattr("containertldr.notify.time.sleep", lambda seconds: None)
    transport = httpx.MockTransport(lambda r: httpx.Response(404 if "/gone" in r.url.path else 204))
    config = Config(notify_urls=["discord://1/works", "discord://2/gone"])
    assert try_notify(config, transport) == "Sent to 1 of 2 notification targets, see the log for the rest."


def test_testing_the_llm():
    config = Config(notify_urls=["x"])
    reply = "**Breaking / action needed:** None mentioned\n**Verdict:** Safe to update"
    answer = httpx.MockTransport(lambda request: httpx.Response(200, json={"message": {"content": reply}}))
    assert try_llm(config, answer) == f"qwen3:8b summarised a made-up release:\n\n{reply}"
    assert try_llm(replace(config, llm_provider="none")).startswith("CTLDR_LLM_PROVIDER is none")

    jobs, down = Jobs(), httpx.MockTransport(lambda request: httpx.Response(500))
    jobs.start("LLM", lambda: try_llm(config, down), secrets=[])
    assert finished(jobs).error.startswith("qwen3:8b at http://ollama:11434 gave no summary: HTTPStatusError")


def test_testing_the_github_token():
    core = {"limit": 60, "remaining": 59}
    transport = httpx.MockTransport(lambda request: httpx.Response(200, json={"resources": {"core": core}}))
    assert try_github(Config(notify_urls=["x"]), transport) == "59 of 60 requests left this hour, without a token."


APP = "ghcr.io/someone/app"


def test_fixing_an_image(tmp_path):
    c = logged_in(tmp_path, docker=FakeDocker([group(f"{APP}:latest", {VERSION: "2.1.0"})]), env=environment(tmp_path))
    assert f'<a href="/containers/{APP}">Change how it' in c.get("/containers").text
    page = c.get(f"/containers/{APP}")
    assert 'name="repo" value="" placeholder="someone/app"' in page.text  # what the guess would be
    assert "Running in app (<code>ghcr.io/someone/app:latest</code>)." in page.text
    assert '<button name="container" value="app">app</button>' in page.text  # for a test summary
    token = csrf(page)
    form = {"csrf": token, "repo": "someone/app-server", "version": "env:APP_VERSION", "commits": ""}
    response = c.post(f"/containers/{APP}", data=form, follow_redirects=False)
    assert (response.status_code, response.headers["location"]) == (303, f"/containers/{APP}?saved")
    assert read_fixes(tmp_path) == {APP: {"repo": "someone/app-server", "version": "env:APP_VERSION"}}
    assert "fixed in the web UI" in c.get("/containers").text
    c.post(f"/containers/{APP}", data={"csrf": token, "remove": "1"})
    assert read_fixes(tmp_path) == {}


def test_a_bad_fix_is_not_saved(tmp_path):
    c = logged_in(tmp_path, docker=FakeDocker([group(f"{APP}:latest")]), env=environment(tmp_path))
    form = {"repo": "app-server", "version": "APP_VERSION", "notes_url": "javascript:alert(1)"}
    response = c.post(f"/containers/{APP}", data={"csrf": csrf(c.get(f"/containers/{APP}")), **form})
    assert response.status_code == 400 and read_fixes(tmp_path) == {}
    text = html.unescape(response.text)
    assert "The repo should be owner/name, like Radarr/Radarr, not 'app-server'" in text
    assert "containertldr.version should be label, env:NAME or none, not 'APP_VERSION'" in text
    assert "The notes URL should start with https://" in text
    assert 'name="repo" value="app-server"' in response.text  # what was typed stays


def test_labels_win_over_a_fix(tmp_path):
    labelled = group(f"{APP}:latest", container_labels={"containertldr.repo": "me/fork"})
    c = logged_in(tmp_path, docker=FakeDocker([labelled]), env=environment(tmp_path))
    page = c.get(f"/containers/{APP}").text
    assert page.count("wins over this") == 1 and "The label on app wins over this." in page


def test_sending_a_test_summary(tmp_path, monkeypatch):
    class FakeChecker:
        tested = []

        @classmethod
        def from_config(cls, config):
            return cls()

        def test(self, container):
            self.tested.append(container)
            if container == "tool":
                raise ValueError("no GitHub repo known for tool")

    monkeypatch.setattr(web.jobs, "Checker", FakeChecker)
    config = Config(notify_urls=["x"])
    assert try_summary(config, "app") == "Sent a [TEST] summary of app's last few releases to the notification targets."
    with pytest.raises(Failed, match="^no GitHub repo known for tool"):
        try_summary(config, "tool")

    c = logged_in(tmp_path, docker=FakeDocker([group(f"{APP}:latest")]), env=environment(tmp_path))
    c.post("/settings/test", data={"csrf": csrf(c.get(f"/containers/{APP}")), "test": "summary", "container": "app"})
    assert '<p class="ok">Test summary of app: OK' in result_page(c)
    assert FakeChecker.tested == ["app", "tool", "app"]


def test_fixes_need_a_password(tmp_path):
    assert client(tmp_path).get(f"/containers/{APP}").status_code == 404
    assert "Change how it" not in client(tmp_path, FakeDocker([group(f"{APP}:latest")])).get("/containers").text


def test_broken_saved_settings_are_shown(tmp_path):
    save(tmp_path, llm_num_ctx="8k")
    page = logged_in(tmp_path, env=environment(tmp_path)).get("/settings").text
    assert "(set in the web UI) should be a positive whole number, not '8k'" in setting(page, "CTLDR_LLM_NUM_CTX")


def test_a_new_password_logs_everyone_out(tmp_path):
    session = logged_in(tmp_path).cookies
    assert oct((tmp_path / "session.key").stat().st_mode & 0o777) == "0o600"
    restarted = client(tmp_path, web_password=PASSWORD)
    restarted.cookies = session
    assert restarted.get("/", follow_redirects=False).status_code == 200  # a restart doesn't log anyone out
    changed = client(tmp_path, web_password="a new one")
    changed.cookies = session
    assert changed.get("/", follow_redirects=False).status_code == 303
