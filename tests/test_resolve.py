import pytest

from containertldr.registry import parse_ref
from containertldr.resolve import LABELS, Upstream, load_mappings, resolve

MAPPINGS = load_mappings()
LSIO_SOURCE = {"org.opencontainers.image.source": "https://github.com/linuxserver/docker-radarr"}


def test_builtin_mappings_are_valid():
    assert MAPPINGS["ghcr.io/linuxserver/radarr"] == {"repo": "Radarr/Radarr"}
    for name, entry in MAPPINGS.items():
        assert entry.keys() <= set(LABELS.values()), name
        resolve(parse_ref(name), {}, None, MAPPINGS)  # raises on a bad version


@pytest.mark.parametrize(
    "ref, image_labels, expected",
    [
        # Mapped, so the packaging repo in the source label is ignored
        ("lscr.io/linuxserver/radarr:latest", LSIO_SOURCE, Upstream("Radarr/Radarr", found_by="mappings")),
        ("ghcr.io/linuxserver/radarr:latest", LSIO_SOURCE, Upstream("Radarr/Radarr", found_by="mappings")),
        (
            "tailscale/tailscale:stable",
            None,
            Upstream("tailscale/tailscale", None, notes_url="https://tailscale.com/changelog", found_by="mappings"),
        ),
        # repo: null stops the guessing, even with a source label
        (
            "docker.io/library/postgres:18-alpine",
            {"org.opencontainers.image.source": "https://github.com/docker-library/postgres"},
            Upstream(None, "env:PG_VERSION", notes_url="https://www.postgresql.org/docs/release/"),
        ),
        # Not mapped
        (
            "lscr.io/linuxserver/readarr:develop",
            {"org.opencontainers.image.source": "https://github.com/linuxserver/docker-readarr"},
            Upstream("linuxserver/docker-readarr", found_by="image source label"),
        ),
        (
            "ghcr.io/someone/app:1",
            {"org.opencontainers.image.source": "https://github.com/Someone/App.git"},
            Upstream("Someone/App", found_by="image source label"),
        ),
        ("ghcr.io/someone/app:1", None, Upstream("someone/app", found_by="ghcr path")),
        ("ghcr.io/someone/group/app:1", None, Upstream(None)),
        ("someone/app:1", {"org.opencontainers.image.source": "https://gitlab.com/someone/app"}, Upstream(None)),
        ("someone/app:1", None, Upstream(None)),
    ],
)
def test_resolve(ref, image_labels, expected):
    assert resolve(parse_ref(ref), {}, image_labels, MAPPINGS) == expected


def test_container_labels_win():
    labels = {
        "containertldr.enable": "true",
        "containertldr.repo": "me/radarr-fork",
        "containertldr.version": "none",
        "containertldr.tag-prefix": "fork-",
        "containertldr.notes-url": "https://example.com/changes",
    }
    upstream = resolve(parse_ref("lscr.io/linuxserver/radarr:latest"), labels, LSIO_SOURCE, MAPPINGS)
    expected = Upstream("me/radarr-fork", None, "fork-", notes_url="https://example.com/changes", found_by="label")
    assert upstream == expected


def test_labels_alone_configure_an_unknown_image():
    labels = {
        "containertldr.repo": "me/tool",
        "containertldr.version": "env:TOOL_VERSION",
        "containertldr.commits": "True",
    }
    upstream = resolve(parse_ref("registry.lan/tools/tool:main"), labels, None, MAPPINGS)
    assert upstream == Upstream("me/tool", "env:TOOL_VERSION", commits=True, found_by="label")


@pytest.mark.parametrize(
    "labels, error",
    [
        ({"containertldr.tag_prefix": "v"}, "unknown label containertldr.tag_prefix"),
        ({"containertldr.version": "APP_VERSION"}, "version should be label, env:NAME or none"),
    ],
)
def test_label_mistakes_are_errors(labels, error):
    with pytest.raises(ValueError, match=error):
        resolve(parse_ref("ghcr.io/someone/app:1"), labels, None, MAPPINGS)
