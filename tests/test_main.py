import json
import os
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from containertldr import __main__ as cli
from containertldr.config import Config
from containertldr.pipeline import RunResult


class FakeChecker:
    result: RunResult | Exception = RunResult()
    calls: list = []

    @classmethod
    def from_config(cls, config, dry_run=False):
        cls.calls.append(("from_config", dry_run))
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


def test_healthcheck(env):
    assert cli.healthcheck(env) == 1  # no heartbeat yet
    cli.write_heartbeat(env, datetime.now(UTC) - timedelta(hours=1))
    assert cli.healthcheck(env) == 0  # running late, but within the grace period
    cli.write_heartbeat(env, datetime.now(UTC) - cli.GRACE - timedelta(minutes=1))
    assert cli.healthcheck(env) == 1
