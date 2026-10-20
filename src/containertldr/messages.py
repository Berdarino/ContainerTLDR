import re
from dataclasses import dataclass

BLUE, GREY = 0x3B82F6, 0x9CA3AF
REBUILDS_TITLE = "🔁 Rebuilt images (same app version, base OS/library updates)"
ERRORS_TITLE = "⚠️ Update check had errors"
TEST_TITLE, TEST_BODY = "[TEST] ContainerTLDR", "Sent from the settings page. Update notifications will arrive here."
# Discord's limits, in characters
TITLE_MAX, DESCRIPTION_MAX, FIELD_MAX, FOOTER_MAX, EMBED_MAX, CONTENT_MAX = 256, 4096, 1024, 2048, 6000, 2000
NO_MENTIONS = {"parse": []}  # the summary comes from release notes, which must never ping anyone

_MASS_MENTIONS = re.compile(r"@(everyone|here|channel|all)\b", re.I)


@dataclass(frozen=True)
class Update:
    containers: list[str]
    image: str
    stacks: list[str]
    current: str
    new: str
    summary: str
    link: str | None
    footer: str
    test: bool = False

    @property
    def title(self) -> str:
        return f"{'[TEST] ' if self.test else ''}⬆️ {', '.join(self.containers)}: {self.current} → {self.new}"


@dataclass(frozen=True)
class Rebuild:
    containers: list[str]
    version: str


def discord_update(update: Update) -> dict:
    title = _cut(update.title, TITLE_MAX)
    footer = _cut(update.footer, FOOTER_MAX)
    fields = [
        {"name": "Stack", "value": _cut(", ".join(update.stacks) or "-", FIELD_MAX), "inline": True},
        {"name": "Image", "value": _cut(update.image, FIELD_MAX), "inline": True},
    ]
    # Everything in an embed shares one limit; the summary gets what's left
    room = EMBED_MAX - len(title) - len(footer) - sum(len(f["name"]) + len(f["value"]) for f in fields)
    embed = {
        "title": title,
        "description": _cut(_defuse(update.summary), min(DESCRIPTION_MAX, room)),
        "color": BLUE,
        "fields": fields,
        "footer": {"text": footer},
    }
    if update.link:
        embed["url"] = update.link
    return {"embeds": [embed], "allowed_mentions": NO_MENTIONS}


def discord_rebuilds(rebuilds: list[Rebuild]) -> dict:
    title, body = plain_rebuilds(rebuilds)
    embed = {"title": title, "description": _cut(body, DESCRIPTION_MAX), "color": GREY}
    return {"embeds": [embed], "allowed_mentions": NO_MENTIONS}


def discord_errors(errors: list[str]) -> dict:
    title, body = plain_errors(errors)
    return {"content": _cut(f"{title}\n{body}", CONTENT_MAX), "allowed_mentions": NO_MENTIONS}


def discord_test() -> dict:
    return {"embeds": [{"title": TEST_TITLE, "description": TEST_BODY, "color": GREY}], "allowed_mentions": NO_MENTIONS}


def plain_update(update: Update) -> tuple[str, str]:
    """Title and Markdown body for targets other than Discord."""
    lines = [_defuse(update.summary), ""]
    if update.link:
        lines.append(update.link)
    lines += [f"Stack: {', '.join(update.stacks) or '-'} · Image: {update.image}", update.footer]
    return update.title, "\n".join(lines)


def plain_rebuilds(rebuilds: list[Rebuild]) -> tuple[str, str]:
    return REBUILDS_TITLE, "\n".join(f"- {', '.join(r.containers)} ({r.version})" for r in rebuilds)


def plain_errors(errors: list[str]) -> tuple[str, str]:
    return ERRORS_TITLE, "\n".join(f"- {e}" for e in errors)


def _defuse(text: str) -> str:
    """Break @everyone and friends with a zero-width space, for targets that have no allowed_mentions."""
    return _MASS_MENTIONS.sub(lambda m: "@​" + m[1], text)


def _cut(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"
