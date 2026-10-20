from collections.abc import Mapping
from dataclasses import dataclass

from starlette.datastructures import FormData

from ..config import INITIAL_RUNS, LOG_LEVELS, PREFIX, PROVIDERS, Config, ConfigError, load


@dataclass(frozen=True)
class Field:
    name: str  # the Config field, which is also its key in settings.json
    help: str = ""
    secret: bool = False
    choices: tuple[str, ...] = ()

    @property
    def variables(self) -> tuple[str, ...]:
        """The environment variables that set it."""
        if self.name == "github_token":
            return ("GITHUB_TOKEN",)
        if self.name == "tz":
            return (PREFIX + "TZ", "TZ")
        return (PREFIX + self.name.upper(),)

    @property
    def default(self) -> str:
        value = getattr(DEFAULTS, self.name)
        if isinstance(value, bool):
            return "true" if value else "false"
        if isinstance(value, list):
            return ", ".join(value)
        return "" if value is None else str(value)

    def locked_by(self, env: Mapping[str, str]) -> str | None:
        """The environment variable that sets it, if one does. The page can't change it then."""
        for name in self.variables:
            for variable in (name + "_FILE", name) if self.secret else (name,):
                if env.get(variable):
                    return variable
        return None


# The README's configuration table, minus what only the environment can set
GROUPS = {
    "Notifications": [
        Field("notify_urls", "Apprise URLs, separated by commas or new lines. Discord webhooks get embeds", True),
        Field("notify_rebuilds", 'Send the grouped "rebuilt images" message', choices=("true", "false")),
    ],
    "LLM": [
        Field("llm_provider", "none sends the first lines of each release instead of a summary", choices=PROVIDERS),
        Field("llm_url", "For openai_compat, the base URL including /v1"),
        Field("llm_model"),
        Field("llm_api_key", "For hosted OpenAI-compatible APIs", True),
        Field("llm_num_ctx", "Ollama's context size"),
        Field("notes_max_chars", "How much release-note text goes to the model at most"),
    ],
    "Schedule": [
        Field("schedule", "When to check, as a cron expression"),
        Field("tz", "Timezone for the schedule"),
        Field("initial_run", "What the first run does with updates that are already pending", choices=INITIAL_RUNS),
    ],
    "GitHub": [
        Field("github_token", "Raises GitHub's limit from 60 to 5000 requests an hour. It needs no scopes", True),
    ],
    "Docker": [
        Field("docker_host", "Or tcp://socket-proxy:2375"),
        Field("insecure_registries", "Registries to reach over plain http, comma-separated"),
    ],
    "Filters": [
        Field("include_label", "Only check containers with this label (key or key=value)"),
        Field("exclude_label", "Never check containers with this label"),
    ],
    "Advanced": [
        Field("log_level", choices=LOG_LEVELS),
    ],
}
FIELDS = [field for fields in GROUPS.values() for field in fields]
DEFAULTS = Config(notify_urls=[])


@dataclass(frozen=True)
class Row:
    field: Field
    value: str  # what the input holds; for a locked setting, what the environment says
    locked_by: str | None
    saved: bool
    problem: str | None


def form_rows(env: Mapping[str, str], settings: Mapping[str, str], problems: list[str]) -> dict[str, list[Row]]:
    """The settings form, group by group. A secret's value is never part of it."""
    groups = {}
    for group, fields in GROUPS.items():
        groups[group] = []
        for field in fields:
            locked = field.locked_by(env)
            value = "" if field.secret else env[locked] if locked else settings.get(field.name, "")
            problem = next((p for p in problems if p.startswith(field.variables[0] + " ")), None)
            groups[group].append(Row(field, value, locked, bool(settings.get(field.name)), problem))
    return groups


def updated(settings: Mapping[str, str], form: FormData, env: Mapping[str, str]) -> dict[str, str]:
    """The saved settings with the form's changes. An empty secret field keeps the secret; Clear removes it."""
    new = dict(settings)
    for field in FIELDS:
        value = str(form.get(field.name, "")).strip()
        if field.locked_by(env) or (field.secret and not value and not form.get("clear_" + field.name)):
            continue
        if value:
            new[field.name] = value
        else:
            new.pop(field.name, None)
    return new


def problems(env: Mapping[str, str], settings: Mapping[str, str]) -> list[str]:
    """What load() would say about these settings: the same checks as at startup."""
    try:
        load(env, settings)
    except ConfigError as e:
        return str(e).splitlines()
    return []
