import threading
import time
from collections.abc import Callable
from dataclasses import dataclass

import httpx

from ..config import Config
from ..discovery import Docker
from ..github import GitHub, GitHubError
from ..llm import llm_for, summarise
from ..notify import Notifier, NotifyError
from ..pipeline import Checker

# Made up, with one breaking change, to see whether the model finds it
SAMPLE_NOTES = """\
## v2.4.0
- Added a dark theme
- Fixed a crash when a library path contains spaces
- PostgreSQL 13 is no longer supported, upgrade to 15 or newer before updating
"""


class Failed(Exception):
    """A test that didn't work, with a message that says why on its own."""


@dataclass
class Job:
    title: str
    started: float
    result: str | None = None
    error: str | None = None
    took: float | None = None  # set last, once result or error is

    @property
    def done(self) -> bool:
        return self.took is not None


class Jobs:
    """The settings page's tests, one at a time and each in a thread, since the LLM can take a minute and more."""

    def __init__(self):
        self.last: Job | None = None
        self._lock = threading.Lock()

    def start(self, title: str, work: Callable[[], str], secrets: list[str]) -> None:
        with self._lock:
            if self.last and not self.last.done:
                return  # its page shows the one that's running
            self.last = job = Job(title, time.monotonic())
        threading.Thread(target=_run, args=(job, work, secrets), name="test", daemon=True).start()


def _run(job: Job, work: Callable[[], str], secrets: list[str]) -> None:
    try:
        job.result = work()
    except Exception as e:
        error = str(e) if isinstance(e, Failed) else f"{type(e).__name__}: {e}"
        for secret in secrets:  # an error from a library could quote a URL with a token in it
            error = error.replace(secret, "[hidden]")
        job.error = error
    job.took = time.monotonic() - job.started


def secrets_of(config: Config) -> list[str]:
    return [s for s in (*config.notify_urls, config.llm_api_key, config.github_token, config.web_password) if s]


def try_docker(docker: Docker, config: Config) -> str:
    groups = docker.running(config.include_label, config.exclude_label)
    containers = sum(len(g.containers) for g in groups)
    return f"{containers} running containers to check, on {len(groups)} images."


def try_notify(config: Config, transport: httpx.BaseTransport | None = None) -> str:
    notifier = Notifier(config.notify_urls, transport=transport)
    targets = len(notifier.discord) + len(notifier.apprise)
    sent = notifier.test()  # NotifyError if none took it
    rest = "" if sent == targets else ", see the log for the rest"
    return f"Sent to {sent} of {targets} notification targets{rest}."


def try_llm(config: Config, transport: httpx.BaseTransport | None = None) -> str:
    llm = llm_for(config, transport)
    if llm is None:
        return "CTLDR_LLM_PROVIDER is none, so updates come with the first lines of their release notes instead."
    summary = summarise(llm, "example", "2.3.1", "2.4.0", SAMPLE_NOTES)
    if summary.error:
        raise Failed(f"{config.llm_model} at {config.llm_url} gave no summary: {summary.error}")
    return f"{summary.model} summarised a made-up release:\n\n{summary.text}"


def try_summary(config: Config, container: str) -> str:
    """What --test does: a [TEST] summary of the container's last few releases, sent to the notification targets."""
    try:
        Checker.from_config(config).test(container)
    except (ValueError, GitHubError, NotifyError) as e:  # ours, and written to be read as they are
        raise Failed(str(e)) from e
    return f"Sent a [TEST] summary of {container}'s last few releases to the notification targets."


def try_github(config: Config, transport: httpx.BaseTransport | None = None) -> str:
    limit, remaining = GitHub(config.github_token, config.github_url, transport).rate_limit()
    return f"{remaining} of {limit} requests left this hour, {'with' if config.github_token else 'without'} a token."
