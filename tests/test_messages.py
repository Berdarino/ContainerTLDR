from dataclasses import replace

from containertldr.messages import (
    Rebuild,
    Update,
    discord_errors,
    discord_rebuilds,
    discord_update,
    plain_errors,
    plain_rebuilds,
    plain_update,
)

SUMMARY = """\
**Breaking / action needed:** None mentioned
**Security:** None mentioned
**Highlights:**
- Fixed import of movies with special characters in the title
- Translations updated
**Verdict:** Safe to update, small fixes only"""

RADARR = Update(
    containers=["radarr"],
    image="lscr.io/linuxserver/radarr:latest",
    stacks=["arr"],
    current="6.4.4.10685",
    new="6.4.5.10720",
    summary=SUMMARY,
    link="https://github.com/Radarr/Radarr/releases/tag/v6.4.5.10720",
    footer="Summarised by qwen3:8b from 2 release notes",
)


def test_discord_update():
    assert discord_update(RADARR) == {
        "embeds": [
            {
                "title": "⬆️ radarr: 6.4.4.10685 → 6.4.5.10720",
                "url": "https://github.com/Radarr/Radarr/releases/tag/v6.4.5.10720",
                "description": SUMMARY,
                "color": 0x3B82F6,
                "fields": [
                    {"name": "Stack", "value": "arr", "inline": True},
                    {"name": "Image", "value": "lscr.io/linuxserver/radarr:latest", "inline": True},
                ],
                "footer": {"text": "Summarised by qwen3:8b from 2 release notes"},
            }
        ],
        "allowed_mentions": {"parse": []},
    }


def test_test_runs_are_marked_and_links_are_optional():
    embed = discord_update(replace(RADARR, test=True, link=None, stacks=[]))["embeds"][0]
    assert embed["title"].startswith("[TEST] ⬆️ radarr")
    assert "url" not in embed
    assert embed["fields"][0]["value"] == "-"


def test_discord_limits():
    update = replace(RADARR, containers=[f"container-{i}" for i in range(40)], summary="x" * 10_000, footer="f" * 3000)
    embed = discord_update(update)["embeds"][0]
    assert len(embed["title"]) == 256 and embed["title"].endswith("…")
    assert len(embed["footer"]["text"]) == 2048
    assert embed["description"].endswith("…")
    texts = [embed["title"], embed["description"], embed["footer"]["text"]]
    assert sum(map(len, texts)) + sum(len(f["name"]) + len(f["value"]) for f in embed["fields"]) <= 6000


def test_release_notes_cannot_ping_everyone():
    update = replace(RADARR, summary="**Highlights:**\n- @everyone please update now")
    assert "@everyone" not in discord_update(update)["embeds"][0]["description"]
    assert "@​everyone" in plain_update(update)[1]


def test_plain_update():
    title, body = plain_update(RADARR)
    assert title == "⬆️ radarr: 6.4.4.10685 → 6.4.5.10720"
    assert body == "\n".join(
        [
            SUMMARY,
            "",
            "https://github.com/Radarr/Radarr/releases/tag/v6.4.5.10720",
            "Stack: arr · Image: lscr.io/linuxserver/radarr:latest",
            "Summarised by qwen3:8b from 2 release notes",
        ]
    )


def test_rebuilds():
    rebuilds = [Rebuild(["sonarr", "sonarr-anime"], "4.0.15.2941-ls290"), Rebuild(["jellyfin"], "10.11.2")]
    title, body = plain_rebuilds(rebuilds)
    assert title == "🔁 Rebuilt images (same app version, base OS/library updates)"
    assert body == "- sonarr, sonarr-anime (4.0.15.2941-ls290)\n- jellyfin (10.11.2)"
    assert discord_rebuilds(rebuilds)["embeds"][0]["description"] == body


def test_errors():
    errors = ["redis: registry-1.docker.io rate limit reached", "bazarr: GitHub rate limit reached until 05:12 UTC"]
    assert plain_errors(errors)[1] == f"- {errors[0]}\n- {errors[1]}"
    assert discord_errors(errors)["content"].startswith("⚠️ Update check had errors\n- redis:")
    assert len(discord_errors([f"image-{i}: " + "e" * 100 for i in range(50)])["content"]) == 2000
