import html
import json
import re
import threading
import time
from dataclasses import replace
from datetime import UTC, datetime

import httpx
from starlette.testclient import TestClient

from containertldr import web
from containertldr.__main__ import write_heartbeat, write_last_run
from containertldr.config import Config, load, read_settings
from containertldr.discovery import ImageGroup, LocalImage
from containertldr.pipeline import RunResult
from containertldr.registry import parse_ref
from containertldr.state import Entry, State, state_key
from containertldr.web import create_app
from containertldr.web.jobs import Jobs, try_github, try_llm, try_notify

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
    assert page.count(FOOTER) == 2 and page.count("**Verdict:** Safe to update") == 2


def test_summaries_are_shown_as_text(tmp_path):
    summary = '<script>alert("from the release notes")</script>'
    write_state(tmp_path, ("ghcr.io/someone/app:latest", "2026-10-20T04:31:00+00:00", sent("⬆️ app: 1 → 2", summary)))
    page = client(tmp_path).get("/").text
    assert "<script>" not in page
    assert "&lt;script&gt;" in page


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


def test_testing_docker_from_the_settings_page(tmp_path):
    docker = FakeDocker([group(RADARR), group("redis:8")])
    c = logged_in(tmp_path, docker=docker, env=environment(tmp_path))
    token = csrf(c.get("/settings"))
    response = c.post("/settings/test", data={"csrf": token, "test": "docker"}, follow_redirects=False)
    assert (response.status_code, response.headers["location"]) == (303, "/settings/test")
    for _ in range(500):
        if "This page reloads" not in (page := c.get("/settings/test").text):
            break
        time.sleep(0.01)
    assert '<p class="ok">Docker works' in page and "2 running containers to check, on 2 images." in page
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
