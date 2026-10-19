from datetime import UTC, datetime, timedelta

from containertldr.notes import brief_notes, build_notes, clean_body
from containertldr.releases import Release


def rel(tag, published, body):
    return Release(tag, datetime.fromisoformat(published).replace(tzinfo=UTC), body, "")


def test_clean_github_generated_notes():
    body = """<!-- Release notes generated using configuration in .github/release.yml -->

## What's Changed
* Fix login redirect by @alice in https://github.com/papra-hq/papra/pull/512
* Add Hungarian translation by @bob in https://github.com/papra-hq/papra/pull/518

## New Contributors
* @bob made their first contribution in https://github.com/papra-hq/papra/pull/518

**Full Changelog**: https://github.com/papra-hq/papra/compare/v26.6.2...v26.7.0
""".replace("\n", "\r\n")
    assert clean_body(body) == "\n".join(
        [
            "## What's Changed",
            "* Fix login redirect by @alice in #512",
            "* Add Hungarian translation by @bob in #518",
        ]
    )


def test_clean_home_assistant_link_references():
    body = """- Fix Hue light flicker ([@alice] - [#130001]) ([hue docs])
- Bump aiohttp to 3.13.2 ([@bob] - [#130002]) ([dependencies docs])

[#130001]: https://github.com/home-assistant/core/pull/130001
[#130002]: https://github.com/home-assistant/core/pull/130002
[@alice]: https://github.com/alice
[@bob]: https://github.com/bob
[hue docs]: https://www.home-assistant.io/integrations/hue/
[dependencies docs]: https://developers.home-assistant.io/docs/creating_platform_code_review.html
"""
    assert clean_body(body) == "\n".join(
        [
            "- Fix Hue light flicker ([@alice] - [#130001]) ([hue docs])",
            "- Bump aiohttp to 3.13.2 ([@bob] - [#130002]) ([dependencies docs])",
        ]
    )


def test_clean_changesets_credits():
    body = (
        "- [#1531](https://github.com/papra-hq/papra/pull/1531) "
        "[`4ac1d68`](https://github.com/papra-hq/papra/commit/4ac1d68e472b115a21bfd3316946a1170076a762) "
        "Thanks [@CorentinTh](https://github.com/CorentinTh)! - Added Hungarian translation\n"
        "- [#1461](https://github.com/papra-hq/papra/pull/1461) "
        "[`5d3063b`](https://github.com/papra-hq/papra/commit/5d3063b) "
        "Thanks [@bartdenhoed](https://github.com/bartdenhoed), [@CorentinTh](https://github.com/CorentinTh)! "
        "- Fixed refresh"
    )
    assert clean_body(body) == "- #1531 Added Hungarian translation\n- #1461 Fixed refresh"


def test_clean_linuxserver_release():
    body = """**CI Report:**

https://ci-tests.linuxserver.io/linuxserver/radarr/6.4.4.10685-ls322/index.html

**LinuxServer Changes:**


* sed commit sha tags in release notes by @aptalca in https://github.com/linuxserver/docker-radarr/pull/271


**Full Changelog**: https://github.com/linuxserver/docker-radarr/compare/6.4.4.10685-ls321...6.4.4.10685-ls322

**Remote Changes:**

## Changes:

* [a96bf7](https://github.com/Radarr/Radarr/commit/a96bf7c3ecb23cb76993770727285dc17a613fbc) Translations updated
"""
    assert clean_body(body) == "\n".join(
        [
            "**LinuxServer Changes:**",
            "",
            "* sed commit sha tags in release notes by @aptalca in #271",
            "",
            "**Remote Changes:**",
            "",
            "## Changes:",
            "",
            "* a96bf7 Translations updated",
        ]
    )


def test_clean_links_and_images():
    body = "![screenshot](https://example.com/a.png)\nSee [the upgrade guide](https://docs.example.com/upgrade) first."
    assert clean_body(body) == "See the upgrade guide first."


def test_clean_keeps_bracketed_text_that_is_not_a_link():
    assert clean_body("[BREAKING]: config.yml moved to /config") == "[BREAKING]: config.yml moved to /config"


def test_build_oldest_first_with_headers():
    notes = build_notes([rel("v1.6.1", "2026-09-01", "Fixes"), rel("v1.6.2", "2026-09-10", "More fixes")], 18000)
    assert notes.text == "## v1.6.1 (2026-09-01)\nFixes\n\n## v1.6.2 (2026-09-10)\nMore fixes"
    assert (notes.count, notes.truncated, notes.body_chars) == (2, False, 15)


def test_build_gives_short_releases_room_and_cuts_the_long_one():
    short = "- fix\n" * 50
    releases = [
        rel("2026.10.0", "2026-10-01", "- new integration\n" * 3000),
        rel("2026.10.1", "2026-10-10", short),
        rel("2026.10.2", "2026-10-17", short),
    ]
    notes = build_notes(releases, 18000)
    assert 17000 < len(notes.text) <= 18000
    assert notes.text.count("- fix") == 100
    assert "[...]" in notes.text
    assert (notes.count, notes.truncated) == (3, True)


def test_build_drops_oldest_when_too_many_releases():
    start = datetime(2026, 1, 1, tzinfo=UTC)
    releases = [Release(f"v1.0.{i}", start + timedelta(days=i), "x" * 2000, "") for i in range(40)]
    notes = build_notes(releases, 18000)
    assert len(notes.text) <= 18000
    assert "## v1.0.39 " in notes.text
    assert "## v1.0.0 " not in notes.text
    assert 0 < notes.count < 40
    assert notes.text.count("## ") == notes.count
    assert notes.truncated


def test_body_chars_ignore_headers():
    releases = [rel(f"v1.90.{i}", f"2026-10-0{i}", "Changelog: https://tailscale.com/changelog") for i in range(1, 5)]
    notes = build_notes(releases, 18000)
    assert notes.body_chars < 200 < len(notes.text)


def test_brief_notes_newest_first_without_headings():
    releases = [
        rel("v1.6.1", "2026-09-01", "### Fixes\n- one\n\n- two"),
        rel("v1.6.2", "2026-09-10", "## What's Changed\n" + "\n".join(f"- change {i}" for i in range(8))),
    ]
    assert brief_notes(releases, lines=3) == "\n".join(
        [
            "**v1.6.2**",
            "- change 0",
            "- change 1",
            "- change 2",
            "…",
            "",
            "**v1.6.1**",
            "- one",
            "- two",
        ]
    )
