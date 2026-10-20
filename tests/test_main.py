import json
import os
import socket
import time
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import httpx
import pytest

from containertldr import __main__ as cli
from containertldr.config import Config, load
from containertldr.heartbeat import GRACE
from containertldr.pipeline import RunResult


class FakeChecker:
    result: RunResult | Exception = RunResult()
    calls: list = []
    config: Config | None = None

    @classmethod
    def from_config(cls, config, dry_run=False):
        cls.calls.append(("from_config", dry_run))
        cls.config = config
        return cls()

    def run(self):
        if isinstance(self.result, Exception):
            raise self.result
        return self.result

    def test(self, container):
        self.calls.append(("test", container))


class FakeNotifier:
    sent: list = []

    def __init__(self, urls, dry_run=False):
        pass

    def errors(self, errors):
        self.sent.append(errors)


@pytest.fixture(autouse=True)
def env(monkeypatch, tmp_path):
    for key in list(os.environ):
        if key.startswith("CTLDR_") or key in ("TZ", "GITHUB_TOKEN"):
            monkeypatch.delenv(key)
    monkeypatch.setenv("CTLDR_NOTIFY_URLS", "discord://123/AbC-token")
    monkeypatch.setenv("CTLDR_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("DOCKER_CONFIG", str(tmp_path))
    monkeypatch.setattr(cli, "Checker", FakeChecker)
    monkeypatch.setattr(cli, "Notifier", FakeNotifier)
    FakeChecker.result, FakeChecker.calls, FakeNotifier.sent = RunResult(), [], []
    return tmp_path


def test_next_check_follows_the_timezone():
    config = Config(notify_urls=["x"], tz="Europe/London")
    after = datetime(2026, 10, 24, 12, 0, tzinfo=ZoneInfo("Europe/London"))
    due = cli.next_check(config, after)
    assert due == datetime(2026, 10, 25, 4, 30, tzinfo=ZoneInfo("Europe/London"))
    assert due.utcoffset() == timedelta(0)  # the clocks went back that night


@pytest.mark.parametrize(
    "setting, problem",
    [
        ({"CTLDR_NOTIFY_URLS": ""}, "CTLDR_NOTIFY_URLS is required"),
        ({"CTLDR_SCHEDULE": "every day at 4"}, "CTLDR_SCHEDULE should be a cron expression"),
    ],
)
def test_bad_settings_stop_startup(monkeypatch, capsys, setting, problem):
    for key, value in setting.items():
        monkeypatch.setenv(key, value)
    assert cli.main(["--once"]) == 2
    assert problem in capsys.readouterr().err


@pytest.mark.skipif(os.geteuid() == 0, reason="root can write to a read-only directory")
def test_unwritable_data_dir_stops_startup(monkeypatch, capsys, env):
    locked = env / "locked"
    locked.mkdir(mode=0o500)
    monkeypatch.setenv("CTLDR_DATA_DIR", str(locked / "data"))
    assert cli.main(["--once"]) == 2
    assert "has to be writable by uid" in capsys.readouterr().err


@pytest.mark.parametrize("result, code", [(RunResult(updates=1), 0), (RunResult(errors=["redis: oops"]), 1)])
def test_once_exit_code(result, code):
    FakeChecker.result = result
    assert cli.main(["--once"]) == code
    assert FakeChecker.calls == [("from_config", False)]


def test_dry_run_is_passed_on():
    cli.main(["--once", "--dry-run"])
    assert FakeChecker.calls == [("from_config", True)]


def test_test_mode():
    assert cli.main(["--test", "radarr"]) == 0
    assert ("test", "radarr") in FakeChecker.calls


def test_a_failed_check_is_reported(env):
    FakeChecker.result = ConnectionError("can't reach socket-proxy:2375")
    assert cli.main(["--once"]) == 1
    failure = "the whole check failed: ConnectionError: can't reach socket-proxy:2375"
    assert FakeNotifier.sent == [[failure]]
    assert json.loads((env / "data" / "last_run.json").read_text())["errors"] == [failure]


def test_the_last_run_is_kept_for_the_status_page(env):
    FakeChecker.result = RunResult(updates=1, baselined=2, errors=["redis: oops"])
    cli.main(["--once"])
    last = json.loads((env / "data" / "last_run.json").read_text())
    assert (last["updates"], last["rebuilds"], last["baselined"], last["errors"]) == (1, 0, 2, ["redis: oops"])
    assert last["started"] <= last["finished"]
    assert sorted(p.name for p in (env / "data").iterdir()) == ["last_run.json"]  # no temp file left behind


def test_dry_run_keeps_no_last_run(env):
    (env / "data").mkdir()
    cli.main(["--once", "--dry-run"])
    assert not (env / "data" / "last_run.json").exists()


def test_schedule_writes_the_heartbeat_before_waiting(monkeypatch, env):
    def stop(seconds):
        raise KeyboardInterrupt

    monkeypatch.setattr(cli.time, "sleep", stop)
    config = Config(notify_urls=["x"], data_dir=env)
    with pytest.raises(KeyboardInterrupt):
        cli.follow_schedule(config, dry_run=False)
    due = datetime.fromisoformat(json.loads((env / "heartbeat.json").read_text())["next_check"])
    assert datetime.now(UTC) < due <= datetime.now(UTC) + timedelta(days=1)
    assert cli.healthcheck(env) == 0


def save(data_dir, **settings):
    data_dir.mkdir(exist_ok=True)
    (data_dir / "settings.json").write_text(json.dumps({"version": 1, "settings": settings}))


def test_saved_settings_are_read_at_startup(monkeypatch, env):
    monkeypatch.delenv("CTLDR_NOTIFY_URLS")
    save(env / "data", notify_urls="discord://123/AbC-token", llm_model="llama3.2")
    assert cli.main(["--once"]) == 0
    assert FakeChecker.config.llm_model == "llama3.2"


def test_a_schedule_saved_while_waiting_moves_the_next_check(monkeypatch, env):
    naps = []

    def nap(seconds):
        naps.append(seconds)
        if len(naps) == 2:
            raise KeyboardInterrupt
        save(env / "data", schedule="*/5 * * * *")

    monkeypatch.setattr(cli.time, "sleep", nap)
    (env / "data").mkdir()
    with pytest.raises(KeyboardInterrupt):
        cli.follow_schedule(load(os.environ), dry_run=False)
    due = datetime.fromisoformat(json.loads((env / "data" / "heartbeat.json").read_text())["next_check"])
    assert due <= datetime.now(UTC) + timedelta(minutes=5)
    assert FakeChecker.calls == []  # a new schedule isn't a reason to check straight away


def test_saved_settings_reach_the_next_check(monkeypatch, env):
    config = load(os.environ)
    save(env / "data", llm_model="llama3.2")
    monkeypatch.setattr(cli, "next_check", lambda config, after: datetime.now(UTC))

    def check(config, dry_run):
        raise KeyboardInterrupt(config.llm_model)

    monkeypatch.setattr(cli, "check", check)
    with pytest.raises(KeyboardInterrupt, match="llama3.2"):
        cli.follow_schedule(config, dry_run=False)


def test_broken_saved_settings_keep_the_ones_in_use(env, caplog):
    config = Config(notify_urls=["x"], data_dir=env)
    (env / "settings.json").write_text("{")
    assert cli.reload(config) is config
    assert "keeping the settings in use" in caplog.text


def test_the_web_ui_only_runs_on_a_schedule_with_a_port(monkeypatch):
    started = []
    monkeypatch.setattr(cli, "start_web", lambda config, dry_run: started.append(config.web_port))
    monkeypatch.setattr(cli, "follow_schedule", lambda config, dry_run: None)
    monkeypatch.setattr(cli.signal, "signal", lambda *args: None)
    cli.main([])
    monkeypatch.setenv("CTLDR_WEB_PORT", "8080")
    cli.main(["--once"])
    cli.main(["--test", "radarr"])
    assert started == []
    cli.main([])
    assert started == [8080]


def test_the_web_ui_serves_beside_the_schedule(env):
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    server = cli.start_web(Config(notify_urls=["x"], data_dir=env, web_host="127.0.0.1", web_port=port))
    try:
        for _ in range(500):
            if server.started:
                break
            time.sleep(0.01)
        assert httpx.get(f"http://127.0.0.1:{port}/").status_code == 200
    finally:
        server.should_exit = True


def test_healthcheck(env):
    assert cli.healthcheck(env) == 1  # no heartbeat yet
    cli.write_heartbeat(env, datetime.now(UTC) - timedelta(hours=1))
    assert cli.healthcheck(env) == 0  # running late, but within the grace period
    cli.write_heartbeat(env, datetime.now(UTC) - GRACE - timedelta(minutes=1))
    assert cli.healthcheck(env) == 1
