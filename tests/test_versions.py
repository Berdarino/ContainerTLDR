import pytest

from containertldr.versions import parse_version, read_version

CONFIG = {
    "Labels": {"org.opencontainers.image.version": "v1.6.1-ls364"},
    "Env": ["PATH=/usr/local/bin:/usr/bin", "PAPRA_VERSION=26.6.2"],
}


@pytest.mark.parametrize(
    "source, expected",
    [
        ("label", "v1.6.1-ls364"),
        ("env:PAPRA_VERSION", "26.6.2"),
        ("env:PG_VERSION", None),
        ("none", None),
        (None, None),
    ],
)
def test_read_version(source, expected):
    assert read_version(CONFIG, source) == expected


def test_read_version_without_labels_or_env():
    assert read_version({"Labels": None, "Env": None}) is None
    assert read_version({"Labels": None, "Env": None}, "env:PG_VERSION") is None


@pytest.mark.parametrize(
    "s, expected",
    [
        ("6.4.4.10685-ls318", "6.4.4.10685"),  # linuxserver/radarr
        ("v6.4.4.10685", "6.4.4.10685"),
        ("5.2.3_v2.0.14-ls477", "5.2.3"),  # linuxserver/qbittorrent: app version, then libtorrent's
        ("release-5.2.4", "5.2.4"),
        ("v1.6.1-ls364", "1.6.1"),  # linuxserver/bazarr
        ("v1.6.2-beta.3", "1.6.2b3"),
        ("2026.9.3", "2026.9.3"),  # home-assistant
        ("2026.10.0b2", "2026.10.0b2"),
        ("12.1", "12.1"),  # jellyfin
        ("0.9.7-3272", "0.9.7"),  # notifiarr
        ("@papra/app@26.7.0", "26.7.0"),  # monorepo tag
        ("8.8-alpine", "8.8"),
        ("1.2.3-devel", "1.2.3"),
        ("main", None),
        ("", None),
        (None, None),
    ],
)
def test_parse_version(s, expected):
    v = parse_version(s)
    assert (str(v) if v else None) == expected


def test_trailing_zeros_compare_equal():
    assert parse_version("1.6") == parse_version("v1.6.0")
    assert parse_version("1.6") < parse_version("1.6.1")


def test_parallel_branches_compare_numerically():
    assert parse_version("8.8.2") < parse_version("8.8.3") < parse_version("8.10.2")


def test_prereleases_sort_before_their_release():
    assert parse_version("v1.6.1") < parse_version("v1.6.2-beta.1") < parse_version("v1.6.2-beta.3")
    assert parse_version("v1.6.2-beta.3") < parse_version("v1.6.2")


def test_linuxserver_rebuild_keeps_version():
    assert parse_version("v1.6.1-ls364") == parse_version("v1.6.1-ls365")


@pytest.mark.parametrize(
    "s, expected",
    [
        ("v1.6.2-beta.3", True),
        ("2.0.0-rc1", True),
        ("2.0.0-RC.2", True),
        ("v3.0.0-alpha", True),
        ("1.0.0-preview.2", True),
        ("1.0.0.dev2", True),
        ("2026.10.0b2", True),
        ("v1.6.1-ls364", False),
        ("5.2.3_v2.0.14-ls477", False),
        ("8.8-alpine", False),
        ("1.2.3-devel", False),
    ],
)
def test_is_prerelease(s, expected):
    assert parse_version(s).is_prerelease is expected
