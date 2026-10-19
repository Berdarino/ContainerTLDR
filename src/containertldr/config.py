import base64
import json
import logging
import os
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .registry import API_HOSTS

log = logging.getLogger(__name__)

PREFIX = "CTLDR_"
PROVIDERS = ("ollama", "openai_compat", "none")
INITIAL_RUNS = ("baseline", "notify")
LOG_LEVELS = ("debug", "info", "warning", "error")


class ConfigError(Exception):
    pass


@dataclass(frozen=True)
class Config:
    # Secrets are left out of repr, so the config can be logged
    notify_urls: list[str] = field(repr=False)
    schedule: str = "30 4 * * *"
    tz: str = "UTC"
    docker_host: str = "unix:///var/run/docker.sock"
    llm_provider: str = "ollama"
    llm_url: str = "http://ollama:11434"
    llm_model: str = "qwen3:8b"
    llm_api_key: str | None = field(default=None, repr=False)
    llm_num_ctx: int = 8192
    notes_max_chars: int = 18000
    github_token: str | None = field(default=None, repr=False)
    github_url: str = "https://api.github.com"
    initial_run: str = "baseline"
    notify_rebuilds: bool = True
    include_label: str | None = None
    exclude_label: str | None = None
    insecure_registries: list[str] = field(default_factory=list)
    registry_credentials: dict[str, tuple[str, str]] = field(default_factory=dict, repr=False)
    data_dir: Path = Path("/data")
    log_level: str = "info"


def load(env: Mapping[str, str] = os.environ) -> Config:
    """Settings from CTLDR_* environment variables, reporting every problem at once.

    Secrets (the notify URLs, the LLM key, GITHUB_TOKEN) can come from a file named in <NAME>_FILE instead,
    for Docker secrets.
    """
    read = _Reader(env)
    notify_urls = split_urls(read.secret(PREFIX + "NOTIFY_URLS") or "")
    if not notify_urls:
        read.problems.append(f"{PREFIX}NOTIFY_URLS is required: one or more Apprise or Discord webhook URLs")

    tz = read.text("TZ") or env.get("TZ") or "UTC"
    try:
        ZoneInfo(tz)
    except (ZoneInfoNotFoundError, ValueError):
        read.problems.append(f"unknown timezone {tz!r}, expected something like Europe/London")

    docker_config = Path(env.get("DOCKER_CONFIG") or Path.home() / ".docker") / "config.json"
    try:
        credentials = registry_credentials(docker_config)
    except (OSError, ValueError) as e:
        read.problems.append(f"can't read registry logins from {docker_config}: {e}")
        credentials = {}

    config = Config(
        notify_urls=notify_urls,
        schedule=read.text("SCHEDULE", "30 4 * * *"),
        tz=tz,
        docker_host=read.text("DOCKER_HOST", "unix:///var/run/docker.sock"),
        llm_provider=read.choice("LLM_PROVIDER", "ollama", PROVIDERS),
        llm_url=read.text("LLM_URL", "http://ollama:11434"),
        llm_model=read.text("LLM_MODEL", "qwen3:8b"),
        llm_api_key=read.secret(PREFIX + "LLM_API_KEY"),
        llm_num_ctx=read.number("LLM_NUM_CTX", 8192),
        notes_max_chars=read.number("NOTES_MAX_CHARS", 18000),
        github_token=read.secret("GITHUB_TOKEN"),
        github_url=read.text("GITHUB_URL", "https://api.github.com"),
        initial_run=read.choice("INITIAL_RUN", "baseline", INITIAL_RUNS),
        notify_rebuilds=read.flag("NOTIFY_REBUILDS", True),
        include_label=read.text("INCLUDE_LABEL"),
        exclude_label=read.text("EXCLUDE_LABEL"),
        insecure_registries=[h.strip() for h in read.text("INSECURE_REGISTRIES", "").split(",") if h.strip()],
        registry_credentials=credentials,
        data_dir=Path(read.text("DATA_DIR", "/data")),
        log_level=read.choice("LOG_LEVEL", "info", LOG_LEVELS),
    )
    if read.problems:
        raise ConfigError("\n".join(read.problems))
    return config


def split_urls(value: str) -> list[str]:
    """Split on commas, spaces or newlines, but only before a new scheme://, since URLs can contain commas
    themselves (mailto://...?to=a@x.com,b@x.com)."""
    return [url for url in re.split(r"[\s,]+(?=[a-z][a-z0-9+.-]*://)", value.strip()) if url]


def registry_credentials(path: Path) -> dict[str, tuple[str, str]]:
    """Registry logins from a Docker config.json, keyed by registry API host."""
    if not path.exists():
        return {}
    data = json.loads(path.read_text())
    if data.get("credsStore") or data.get("credHelpers"):
        log.warning("%s uses a credential helper, which isn't supported; only its auths entries are read", path)
    credentials = {}
    for registry, entry in (data.get("auths") or {}).items():
        if entry.get("auth"):
            username, _, password = base64.b64decode(entry["auth"]).decode().partition(":")
        elif entry.get("username"):
            username, password = entry["username"], entry.get("password", "")
        else:
            continue
        host = re.sub(r"^https?://", "", registry).split("/")[0]  # "https://index.docker.io/v1/" -> index.docker.io
        credentials[API_HOSTS.get(host, host)] = (username, password)
    return credentials


class _Reader:
    """Reads CTLDR_* variables and collects what's wrong with them instead of stopping at the first."""

    def __init__(self, env: Mapping[str, str]):
        self.env = env
        self.problems: list[str] = []

    def text(self, name: str, default: str | None = None) -> str | None:
        return self.env.get(PREFIX + name) or default

    def secret(self, key: str) -> str | None:
        if path := self.env.get(key + "_FILE"):
            try:
                return Path(path).read_text().strip()
            except OSError as e:
                self.problems.append(f"{key}_FILE: can't read {path}: {e.strerror}")
        return self.env.get(key) or None

    def number(self, name: str, default: int) -> int:
        value = self.text(name)
        if value is None:
            return default
        if not value.isdigit() or int(value) == 0:
            self.problems.append(f"{PREFIX}{name} should be a positive whole number, not {value!r}")
            return default
        return int(value)

    def flag(self, name: str, default: bool) -> bool:
        value = (self.text(name) or "").lower()
        if not value:
            return default
        if value not in ("true", "false", "yes", "no", "1", "0", "on", "off"):
            self.problems.append(f"{PREFIX}{name} should be true or false, not {value!r}")
            return default
        return value in ("true", "yes", "1", "on")

    def choice(self, name: str, default: str, choices: tuple[str, ...]) -> str:
        value = (self.text(name) or default).lower()
        if value not in choices:
            self.problems.append(f"{PREFIX}{name} should be one of {', '.join(choices)}, not {value!r}")
            return default
        return value
