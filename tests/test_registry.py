import pytest

from containertldr.registry import parse_ref

HUB = "registry-1.docker.io"


@pytest.mark.parametrize(
    "ref, name, host, repo, tag",
    [
        ("redis:8.8-alpine", "redis", HUB, "library/redis", "8.8-alpine"),
        ("postgres", "postgres", HUB, "library/postgres", "latest"),
        ("docker.io/redis:8", "redis", HUB, "library/redis", "8"),
        ("docker.io/library/redis:8", "redis", HUB, "library/redis", "8"),
        ("jellyfin/jellyfin", "jellyfin/jellyfin", HUB, "jellyfin/jellyfin", "latest"),
        ("index.docker.io/jellyfin/jellyfin:10.11", "jellyfin/jellyfin", HUB, "jellyfin/jellyfin", "10.11"),
        ("ghcr.io/recyclarr/recyclarr:7", "ghcr.io/recyclarr/recyclarr", "ghcr.io", "recyclarr/recyclarr", "7"),
        ("lscr.io/linuxserver/radarr:latest", "lscr.io/linuxserver/radarr", "ghcr.io", "linuxserver/radarr", "latest"),
        ("localhost:5000/test/app:1.0", "localhost:5000/test/app", "localhost:5000", "test/app", "1.0"),
        ("registry.lan:5000/app", "registry.lan:5000/app", "registry.lan:5000", "app", "latest"),
        ("localhost/app:dev", "localhost/app", "localhost", "app", "dev"),
    ],
)
def test_parse_ref(ref, name, host, repo, tag):
    r = parse_ref(ref)
    assert (r.name, r.host, r.repo, r.tag, r.digest) == (name, host, repo, tag, None)


def test_parse_ref_with_digest():
    r = parse_ref("redis:8-alpine@sha256:abc123")
    assert (r.name, r.tag, r.digest) == ("redis", "8-alpine", "sha256:abc123")
