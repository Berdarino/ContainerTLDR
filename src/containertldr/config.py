import base64
import functools
import json
import logging
import os
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from croniter import croniter

from .registry import API_HOSTS

log = logging.getLogger(__name__)

PREFIX = "CTLDR_"
PROVIDERS = ("ollama", "openai_compat", "none")
INITIAL_RUNS = ("baseline", "notify")
LOG_LEVELS = ("debug", "info", "warning", "error")
SETTINGS = "settings.json"
# Where the settings file is, and how the web UI is reached, can't be changed from the web UI itself
ENV_ONLY = ("data_dir", "web_port", "web_host", "web_password", "webhook_token")


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
    web_port: int | None = None  # no web UI, and no open port, unless it's set
    web_host: str = "0.0.0.0"
    web_password: str | None = field(default=None, repr=False)  # without one, the settings pages stay off
    webhook_token: str | None = field(default=None, repr=False)  # without one, there are no webhooks


def load(env: Mapping[str, str] = os.environ, settings: Mapping[str, str] | None = None) -> Config:
    """Settings from CTLDR_* environment variables, then from settings saved in the web UI, then the defaults.

    Every problem is reported at once. Secrets (the notify URLs, the LLM key, GITHUB_TOKEN, the web UI password and
    the webhook token) can come from a file named in <NAME>_FILE instead, for Docker secrets.
    """
    saved = {k: v for k, v in (settings or {}).items() if k not in ENV_ONLY}
    read = _Reader(env, saved)
    notify_urls = split_urls(read.secret(PREFIX + "NOTIFY_URLS") or "")
    if not notify_urls:
        read.problems.append(f"{PREFIX}NOTIFY_URLS is required: one or more Apprise or Discord webhook URLs")

    docker_config = Path(env.get("DOCKER_CONFIG") or Path.home() / ".docker") / "config.json"
    try:
        credentials = registry_credentials(docker_config)
    except (OSError, ValueError) as e:
        read.problems.append(f"can't read registry logins from {docker_config}: {e}")
        credentials = {}

    web_port = read.number("WEB_PORT", 0) or None
    if web_port and web_port > 65535:
        read.problems.append(f"{PREFIX}WEB_PORT should be a port number like 8080, not {web_port}")

    config = Config(
        notify_urls=notify_urls,
        schedule=read.cron("SCHEDULE", "30 4 * * *"),
        tz=read.timezone(),
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
        data_dir=data_dir(env),
        log_level=read.choice("LOG_LEVEL", "info", LOG_LEVELS),
        web_port=web_port,
        web_host=read.text("WEB_HOST", "0.0.0.0"),
        web_password=read.secret(PREFIX + "WEB_PASSWORD"),
        webhook_token=read.secret(PREFIX + "WEBHOOK_TOKEN"),
    )
    if read.problems:
        raise ConfigError("\n".join(read.problems))
    return config


def data_dir(env: Mapping[str, str] = os.environ) -> Path:
    """Where the state and the saved settings are, so it has to be known before the settings are read."""
    return Path(env.get(PREFIX + "DATA_DIR") or "/data")


def read_settings(data_dir: Path) -> dict[str, str]:
    """What was saved on the web UI's settings page, as the strings the environment variables would hold."""
    return _saved(data_dir).get("settings", {})


def read_fixes(data_dir: Path) -> dict[str, dict[str, str]]:
    """How images should be read, saved in the web UI, by image_key(): the containertldr.* label fields."""
    return _saved(data_dir).get("images", {})


def write_settings(data_dir: Path, settings: Mapping[str, str]) -> None:
    _write(data_dir, settings=dict(sorted(settings.items())))


def write_fixes(data_dir: Path, fixes: Mapping[str, dict[str, str]]) -> None:
    _write(data_dir, images=dict(sorted(fixes.items())))


def _saved(data_dir: Path) -> dict:
    path = data_dir / SETTINGS
    try:
        return json.loads(path.read_text())
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as e:
        raise ConfigError(f"can't read {path}: {e}") from e


def _write(data_dir: Path, **sections: dict) -> None:
    """Replaces one part of the file and keeps the rest. In one go, and readable by its owner only, since it can
    hold webhook URLs and tokens."""
    path, tmp = data_dir / SETTINGS, data_dir / (SETTINGS + ".tmp")
    data = {"version": 1, **_saved(data_dir), **sections}
    tmp.unlink(missing_ok=True)  # so the mode below applies
    with os.fdopen(os.open(tmp, os.O_WRONLY | os.O_CREAT, 0o600), "w") as f:
        f.write(json.dumps(data, indent=2) + "\n")
    os.replace(tmp, path)


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
        _warn_once(f"{path} uses a credential helper, which isn't supported; only its auths entries are read")
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


@functools.cache
def _warn_once(message: str) -> None:
    # Settings are loaded again before every check and for web pages, which shouldn't repeat this each time
    log.warning(message)


class _Reader:
    """Reads CTLDR_* variables, falling back to the saved settings, and collects what's wrong with them instead of
    stopping at the first. Saved settings are keyed by Config field: CTLDR_LLM_URL is llm_url."""

    def __init__(self, env: Mapping[str, str], settings: Mapping[str, str]):
        self.env = env
        self.settings = settings
        self.problems: list[str] = []

    def text(self, name: str, default: str | None = None) -> str | None:
        return self.env.get(PREFIX + name) or self.settings.get(name.lower()) or default

    def secret(self, key: str) -> str | None:
        if path := self.env.get(key + "_FILE"):
            try:
                return Path(path).read_text().strip()
            except OSError as e:
                self.problems.append(f"{key}_FILE: can't read {path}: {e.strerror}")
        return self.env.get(key) or self.settings.get(key.removeprefix(PREFIX).lower()) or None

    def number(self, name: str, default: int) -> int:
        value = self.text(name)
        if value is None:
            return default
        if not value.isdigit() or int(value) == 0:
            self.problems.append(f"{self._label(name)} should be a positive whole number, not {value!r}")
            return default
        return int(value)

    def flag(self, name: str, default: bool) -> bool:
        value = (self.text(name) or "").lower()
        if not value:
            return default
        if value not in ("true", "false", "yes", "no", "1", "0", "on", "off"):
            self.problems.append(f"{self._label(name)} should be true or false, not {value!r}")
            return default
        return value in ("true", "yes", "1", "on")

    def choice(self, name: str, default: str, choices: tuple[str, ...]) -> str:
        value = (self.text(name) or default).lower()
        if value not in choices:
            self.problems.append(f"{self._label(name)} should be one of {', '.join(choices)}, not {value!r}")
            return default
        return value

    def cron(self, name: str, default: str) -> str:
        value = self.text(name, default)
        if not croniter.is_valid(value):
            self.problems.append(f"{self._label(name)} should be a cron expression like '30 4 * * *', not {value!r}")
            return default
        return value

    def timezone(self) -> str:
        # Plain TZ is set for the whole container, so it comes before a saved timezone as well
        name = next((n for n in (PREFIX + "TZ", "TZ") if self.env.get(n)), None)
        value = self.env[name] if name else self.settings.get("tz") or "UTC"
        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError):
            self.problems.append(f"{name or self._label('TZ')} should be a timezone like Europe/London, not {value!r}")
            return "UTC"
        return value

    def _label(self, name: str) -> str:
        if self.env.get(PREFIX + name) or name.lower() not in self.settings:
            return PREFIX + name
        return f"{PREFIX}{name} (set in the web UI)"  # so nobody goes looking for a variable they never set
