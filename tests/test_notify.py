import json

import apprise
import httpx
import pytest

from containertldr.messages import Rebuild, Update
from containertldr.notify import Notifier, NotifyError, discord_webhook, redact

UPDATE = Update(["radarr"], "lscr.io/linuxserver/radarr:latest", ["arr"], "6.4.4", "6.4.5", "Summary", None, "Footer")
WEBHOOK = "https://discord.com/api/webhooks/123/AbC-token"


@pytest.fixture(autouse=True)
def no_sleeping(monkeypatch):
    slept = []
    monkeypatch.setattr("containertldr.notify.time.sleep", slept.append)
    return slept


@pytest.fixture
def apprise_calls(monkeypatch):
    calls = []

    class Result:
        success_count, failed_count = 1, 0

        def __bool__(self):
            return True

    def notify(self, body, **kwargs):
        calls.append({"body": body, **kwargs})
        return Result()

    monkeypatch.setattr(apprise.Apprise, "notify", notify)
    return calls


def responding(*responses, requests):
    queue = list(responses)

    def handler(request):
        requests.append(request)
        return queue.pop(0)

    return httpx.MockTransport(handler)


@pytest.mark.parametrize(
    "url, expected",
    [
        ("discord://123/AbC-token", (WEBHOOK, None)),
        ("discord://Updates@123/AbC-token", (WEBHOOK, "Updates")),
        ("discord://123/AbC-token?thread=456", (WEBHOOK + "?thread_id=456", None)),
        (WEBHOOK, (WEBHOOK, None)),
        ("https://discordapp.com/api/webhooks/123/AbC-token", (WEBHOOK, None)),
        ("ntfy://ntfy.sh/updates", None),
        ("https://example.com/api/webhooks/123/x", None),
    ],
)
def test_discord_webhook(url, expected):
    assert discord_webhook(url) == expected


@pytest.mark.parametrize(
    "url, expected",
    [
        ("discord://123/AbC-token", "discord://***"),
        (WEBHOOK, "https://discord.com/***"),
        ("ntfy://user:secret@ntfy.lan/updates", "ntfy://***"),
    ],
)
def test_redact(url, expected):
    assert redact(url) == expected


def test_discord_gets_the_embed(no_sleeping):
    requests = []
    transport = responding(httpx.Response(204), requests=requests)
    Notifier(["discord://Updates@123/AbC-token"], transport=transport).update(UPDATE)
    payload = json.loads(requests[0].content)
    assert str(requests[0].url) == WEBHOOK
    assert payload["username"] == "Updates"
    assert payload["embeds"][0]["title"] == "⬆️ radarr: 6.4.4 → 6.4.5"
    assert payload["allowed_mentions"] == {"parse": []}
    assert no_sleeping == [1]  # spacing before the next post


def test_discord_rate_limit_is_waited_out(no_sleeping):
    requests = []
    transport = responding(httpx.Response(429, headers={"Retry-After": "2.5"}), httpx.Response(204), requests=requests)
    Notifier(["discord://123/AbC-token"], transport=transport).update(UPDATE)
    assert len(requests) == 2
    assert no_sleeping == [3.0, 1]


def test_other_targets_go_through_apprise(apprise_calls):
    notifier = Notifier(["ntfy://ntfy.sh/updates"])
    notifier.update(UPDATE)
    notifier.rebuilds([Rebuild(["sonarr"], "4.0.15")])
    notifier.rebuilds([])  # nothing to send
    assert [c["title"] for c in apprise_calls] == [
        "⬆️ radarr: 6.4.4 → 6.4.5",
        "🔁 Rebuilt images (same app version, base OS/library updates)",
    ]
    assert apprise_calls[0]["body_format"] == apprise.NotifyFormat.MARKDOWN
    assert apprise_calls[0]["body"].startswith("Summary\n")


def test_one_working_target_is_enough(apprise_calls):
    requests = []
    transport = responding(httpx.Response(404), requests=requests)
    Notifier(["discord://123/gone", "ntfy://ntfy.sh/updates"], transport=transport).errors(["redis: oops"])
    assert len(apprise_calls) == 1


def test_every_target_failing_is_an_error():
    requests = []
    transport = responding(httpx.Response(500), requests=requests)
    with pytest.raises(NotifyError):
        Notifier(["discord://123/AbC-token"], transport=transport).update(UPDATE)


def test_bad_url_is_rejected_without_showing_it():
    with pytest.raises(ValueError, match=r"notaservice://\*\*\*") as error:
        Notifier(["notaservice://admin:hunter2@notify.lan/updates"])
    assert "hunter2" not in str(error.value)


def test_dry_run_prints_instead(capsys):
    requests = []
    Notifier(["discord://123/AbC-token"], dry_run=True, transport=responding(requests=requests)).update(UPDATE)
    assert capsys.readouterr().out.startswith("⬆️ radarr: 6.4.4 → 6.4.5\nSummary")
    assert requests == []
