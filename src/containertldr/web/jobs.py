import threading
import time
from collections.abc import Callable
from dataclasses import dataclass

import httpx

from ..config import Config
from ..discovery import Docker
from ..github import GitHub


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
        error = f"{type(e).__name__}: {e}"
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


def try_github(config: Config, transport: httpx.BaseTransport | None = None) -> str:
    limit, remaining = GitHub(config.github_token, config.github_url, transport).rate_limit()
    return f"{remaining} of {limit} requests left this hour, {'with' if config.github_token else 'without'} a token."
