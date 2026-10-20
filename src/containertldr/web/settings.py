from collections.abc import Mapping
from dataclasses import dataclass

from ..config import PREFIX, Config


@dataclass(frozen=True)
class Field:
    name: str  # the Config field, which is also its key in settings.json
    help: str = ""
    secret: bool = False

    @property
    def variables(self) -> tuple[str, ...]:
        """The environment variables that set it. Any of them locks it on the settings page."""
        if self.name == "github_token":
            return ("GITHUB_TOKEN",)
        if self.name == "tz":
            return (PREFIX + "TZ", "TZ")
        return (PREFIX + self.name.upper(),)


# The README's configuration table, minus what only the environment can set
GROUPS = {
    "Notifications": [
        Field("notify_urls", "Apprise URLs, separated by commas or new lines. Discord webhooks get embeds", True),
        Field("notify_rebuilds", 'Send the grouped "rebuilt images" message'),
    ],
    "LLM": [
        Field("llm_provider", "ollama, openai_compat, or none to send the first lines of each release instead"),
        Field("llm_url", "For openai_compat, the base URL including /v1"),
        Field("llm_model"),
        Field("llm_api_key", "For hosted OpenAI-compatible APIs", True),
        Field("llm_num_ctx", "Ollama's context size"),
        Field("notes_max_chars", "How much release-note text goes to the model at most"),
    ],
    "Schedule": [
        Field("schedule", "When to check, as a cron expression"),
        Field("tz", "Timezone for the schedule"),
        Field("initial_run", "What the first run does with updates that are already pending: baseline or notify"),
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
        Field("log_level", "debug, info, warning or error"),
    ],
}


def shown(field: Field, config: Config) -> str:
    """The value in use, as the page shows it. Never a secret's value."""
    value = getattr(config, field.name)
    if field.secret:
        return "set" if value else "not set"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, list):
        return ", ".join(value)
    return "" if value is None else str(value)


def source(field: Field, env: Mapping[str, str], saved: Mapping[str, str]) -> str:
    for name in field.variables:
        for variable in (name + "_FILE", name) if field.secret else (name,):
            if env.get(variable):
                return f"set by {variable}"
    return "saved here" if saved.get(field.name) else "default"
