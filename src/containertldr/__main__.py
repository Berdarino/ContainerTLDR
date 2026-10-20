import argparse
import json
import logging
import os
import signal
import sys
import threading
import time
from dataclasses import asdict
from datetime import UTC, datetime, timedelta
from importlib.metadata import version
from pathlib import Path
from zoneinfo import ZoneInfo

import uvicorn
from croniter import croniter

from .config import SETTINGS, Config, ConfigError, data_dir, load, read_settings
from .notify import Notifier
from .pipeline import Checker, RunResult
from .web import create_app

log = logging.getLogger("containertldr")

HEARTBEAT, LAST_RUN = "heartbeat.json", "last_run.json"
# How late a check may finish before the healthcheck fails; many summaries on a slow GPU take a while
GRACE = timedelta(hours=2)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="containertldr",
        description="Container update notifications with an LLM summary of the upstream release notes.",
    )
    parser.add_argument("command", nargs="?", choices=["healthcheck"], help="exit 1 if the scheduled check is overdue")
    parser.add_argument("--once", action="store_true", help="run one check now and exit, instead of on the schedule")
    parser.add_argument("--dry-run", action="store_true", help="print notifications instead of sending, save nothing")
    parser.add_argument("--test", metavar="CONTAINER", help="post a [TEST] summary of CONTAINER's last few releases")
    parser.add_argument("--version", action="version", version=f"%(prog)s {version('containertldr')}")
    args = parser.parse_args(argv)

    if args.command == "healthcheck":
        return healthcheck(data_dir())

    try:
        config = load(os.environ, read_settings(data_dir()))
    except ConfigError as e:
        print(f"containertldr can't start:\n{e}", file=sys.stderr)
        return 2
    if not args.dry_run and not args.test:
        try:
            config.data_dir.mkdir(parents=True, exist_ok=True)
            (config.data_dir / ".write-test").touch()
            (config.data_dir / ".write-test").unlink()
        except OSError as e:
            print(f"containertldr can't write to {config.data_dir}: {e.strerror}.", file=sys.stderr)
            print(f"The data volume has to be writable by uid {os.getuid()}.", file=sys.stderr)
            return 2

    logging.basicConfig(level=config.log_level.upper(), format="%(asctime)s %(levelname)s %(message)s", force=True)
    for noisy in ("httpx", "httpcore"):  # they log request URLs, and webhook URLs contain tokens
        logging.getLogger(noisy).setLevel(logging.WARNING)
    log.info("containertldr %s: %s", version("containertldr"), config)

    if args.test:
        try:
            Checker.from_config(config, args.dry_run).test(args.test)
        except Exception as e:
            log.error("test failed: %s", e)
            return 1
        return 0
    if args.once:
        result = check(config, args.dry_run)
        return 0 if result and not result.errors else 1

    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))  # as PID 1 in a container, SIGTERM is ignored otherwise
    if config.web_port:
        start_web(config)
    follow_schedule(config, args.dry_run)
    return 0


def start_web(config: Config) -> uvicorn.Server:
    """Serve the web UI from a thread, so the schedule keeps the main thread and its SIGTERM handling."""
    # log_config=None keeps our log format; a line per page view would bury the checks' own lines
    uv_config = uvicorn.Config(
        create_app(config), host=config.web_host, port=config.web_port, log_config=None, access_log=False
    )
    server = uvicorn.Server(uv_config)
    threading.Thread(target=server.run, name="web", daemon=True).start()
    return server


def check(config: Config, dry_run: bool) -> RunResult | None:
    """One run. None if it failed as a whole, e.g. Docker was unreachable, which it also tries to report."""
    started = datetime.now(UTC)
    try:
        result = Checker.from_config(config, dry_run).run()
    except Exception as e:
        log.exception("check failed")
        failure = f"the whole check failed: {type(e).__name__}: {e}"
        try:
            Notifier(config.notify_urls, dry_run=dry_run).errors([failure])
        except Exception as notify_error:
            log.error("couldn't report that either: %s", notify_error)
        if not dry_run:
            write_last_run(config.data_dir, started, RunResult(errors=[failure]))
        return None
    log.info(
        "check done: %d update(s), %d rebuild(s), %d baselined, %d error(s)",
        result.updates,
        result.rebuilds,
        result.baselined,
        len(result.errors),
    )
    if not dry_run:
        write_last_run(config.data_dir, started, result)
    return result


def follow_schedule(config: Config, dry_run: bool) -> None:
    while True:
        due = next_check(config, datetime.now(ZoneInfo(config.tz)))
        write_heartbeat(config.data_dir, due)
        log.info("next check at %s", due.strftime("%Y-%m-%d %H:%M %Z"))
        on_time = sleep_until(due, config.data_dir)
        config = reload(config)
        if on_time:
            check(config, dry_run)


def sleep_until(due: datetime, data_dir: Path) -> bool:
    """False if settings were saved in the meantime, since they may hold a new schedule."""
    saved = _saved_at(data_dir)
    while (wait := (due - datetime.now(UTC)).total_seconds()) > 0:
        time.sleep(min(wait, 60))  # short naps, so a host that was suspended doesn't oversleep
        if _saved_at(data_dir) != saved:
            return False
    return True


def reload(config: Config) -> Config:
    """The settings as they are now, so what was saved in the web UI applies without a restart."""
    try:
        fresh = load(os.environ, read_settings(config.data_dir))
    except ConfigError as e:
        log.error("keeping the settings in use, the new ones have a problem:\n%s", e)
        return config
    logging.getLogger().setLevel(fresh.log_level.upper())
    return fresh


def _saved_at(data_dir: Path) -> int | None:
    try:
        return (data_dir / SETTINGS).stat().st_mtime_ns
    except FileNotFoundError:
        return None


def next_check(config: Config, after: datetime) -> datetime:
    return croniter(config.schedule, after).get_next(datetime)


def write_heartbeat(data_dir: Path, due: datetime) -> None:
    _write_json(data_dir / HEARTBEAT, {"next_check": due.isoformat()})


def write_last_run(data_dir: Path, started: datetime, result: RunResult) -> None:
    """For the web UI's status page."""
    finished = datetime.now(UTC)
    times = {"started": started.isoformat(timespec="seconds"), "finished": finished.isoformat(timespec="seconds")}
    _write_json(data_dir / LAST_RUN, times | asdict(result))


def _write_json(path: Path, data: dict) -> None:
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data, indent=2) + "\n")
    os.replace(tmp, path)  # the web UI reads these files from another thread, so never leave half of one


def healthcheck(data_dir: Path) -> int:
    """0 unless the next check is overdue. The schedule loop writes when that is before it starts waiting."""
    try:
        due = datetime.fromisoformat(json.loads((data_dir / HEARTBEAT).read_text())["next_check"])
    except (OSError, ValueError, KeyError):
        print("no heartbeat yet")
        return 1
    if datetime.now(UTC) > due + GRACE:
        print(f"the check due at {due:%Y-%m-%d %H:%M %Z} hasn't finished")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
