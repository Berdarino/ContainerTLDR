import logging
import time
from urllib.parse import parse_qs, urlsplit

import apprise
import httpx

from .messages import (
    Rebuild,
    Update,
    discord_errors,
    discord_rebuilds,
    discord_update,
    plain_errors,
    plain_rebuilds,
    plain_update,
)

log = logging.getLogger(__name__)

SPACING = 1  # seconds between Discord posts, to stay clear of its rate limit


class NotifyError(Exception):
    pass


def redact(url: str) -> str:
    """Enough of a notification URL to tell which one it is, without the tokens in it."""
    parts = urlsplit(url)
    if parts.scheme in ("http", "https"):
        return f"{parts.scheme}://{parts.hostname}/***"
    return f"{parts.scheme or '?'}://***"


def discord_webhook(url: str) -> tuple[str, str | None] | None:
    """(webhook URL, bot name) for an Apprise discord:// URL or a Discord webhook URL, otherwise None."""
    parts = urlsplit(url)
    if parts.scheme == "discord":  # discord://[botname@]webhook_id/webhook_token
        webhook, bot = f"https://discord.com/api/webhooks/{parts.netloc.rpartition('@')[2]}{parts.path}", parts.username
    elif parts.hostname in ("discord.com", "discordapp.com") and parts.path.startswith("/api/webhooks/"):
        webhook, bot = f"https://discord.com{parts.path}", None
    else:
        return None
    query = parse_qs(parts.query)
    if thread := query.get("thread") or query.get("thread_id"):
        webhook += f"?thread_id={thread[0]}"
    return webhook, bot


class Notifier:
    """Sends to every target: Discord webhooks get native embeds, everything else goes through Apprise.

    A message counts as sent if at least one target took it. Otherwise one dead target would make the
    others repeat the same update on every run.
    """

    def __init__(self, urls: list[str], dry_run: bool = False, transport: httpx.BaseTransport | None = None):
        self.dry_run = dry_run
        self.discord: list[tuple[str, str | None]] = []
        self.apprise = apprise.Apprise()
        for url in urls:
            if webhook := discord_webhook(url):
                self.discord.append(webhook)
            elif not self.apprise.add(url):
                raise ValueError(f"not a notification URL Apprise understands: {redact(url)}")
        self.http = httpx.Client(timeout=30, transport=transport)

    def update(self, update: Update) -> None:
        self._send(discord_update(update), *plain_update(update))

    def rebuilds(self, rebuilds: list[Rebuild]) -> None:
        if rebuilds:
            self._send(discord_rebuilds(rebuilds), *plain_rebuilds(rebuilds))

    def errors(self, errors: list[str]) -> None:
        if errors:
            self._send(discord_errors(errors), *plain_errors(errors))

    def _send(self, payload: dict, title: str, body: str) -> None:
        if self.dry_run:
            print(f"{title}\n{body}\n")
            return
        sent = 0
        for webhook, bot in self.discord:
            try:
                self._post_discord(webhook, {**payload, "username": bot} if bot else payload)
                sent += 1
            except httpx.HTTPError as e:
                log.warning("couldn't post to %s: %s", redact(webhook), type(e).__name__)
        if len(self.apprise):
            result = self.apprise.notify(body, title=title, body_format=apprise.NotifyFormat.MARKDOWN)
            sent += result.success_count
            if not result:
                log.warning("%d Apprise target(s) failed, see Apprise's log", result.failed_count)
        if not sent:
            raise NotifyError(f"no notification target took {title!r}")

    def _post_discord(self, webhook: str, payload: dict) -> None:
        for attempt in range(3):
            r = self.http.post(webhook, json=payload)
            if r.status_code == 429 and attempt < 2:
                time.sleep(float(r.headers.get("Retry-After", 1)) + 0.5)
                continue
            r.raise_for_status()
            break
        time.sleep(SPACING)
