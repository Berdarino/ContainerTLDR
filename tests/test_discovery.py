from datetime import UTC, datetime

import httpx
import pytest

from containertldr.discovery import Docker

SONARR = "lscr.io/linuxserver/sonarr:latest"
OLD, NEW = "sha256:" + "a" * 64, "sha256:" + "b" * 64
OLD_DIGEST, NEW_DIGEST = "sha256:" + "1" * 64, "sha256:" + "2" * 64


def container(cid, name, ref, image_id, labels=None):
    return {"Id": cid, "Name": f"/{name}", "Image": image_id, "Config": {"Image": ref, "Labels": labels}}


def image(image_id, *repo_digests, arch="amd64", variant=None):
    # Shaped like Docker 29's /images/{id}/json, trimmed to what discovery reads
    return {
        "Id": image_id,
        "RepoDigests": list(repo_digests),
        "Created": "2026-10-01T02:22:34.855525258Z",
        "Os": "linux",
        "Architecture": arch,
        "Variant": variant,
        "Config": {"Labels": {"org.opencontainers.image.version": "4.0.15.2941-ls290"}, "Env": ["PUID=1000"]},
    }


OLD_SONARR = image(OLD, f"lscr.io/linuxserver/sonarr@{OLD_DIGEST}")
NEW_SONARR = image(NEW, f"lscr.io/linuxserver/sonarr@{NEW_DIGEST}")


def engine(containers, images, requests=None):
    """A Docker Engine API that knows the given containers and images."""

    def handler(request):
        if requests is not None:
            requests.append(request)
        kind, _, rest = request.url.path.strip("/").partition("/")
        if (kind, rest) == ("containers", "json"):
            return httpx.Response(200, json=[{"Id": c["Id"], "Image": c["Image"]} for c in containers])
        objects = {c["Id"]: c for c in containers} if kind == "containers" else images
        found = objects.get(rest.removesuffix("/json"))
        return httpx.Response(200, json=found) if found else httpx.Response(404, json={"message": "No such object"})

    return httpx.MockTransport(handler)


def running(containers, images, **filters):
    images = {i["Id"]: i for i in images}
    return Docker("tcp://socket-proxy:2375", transport=engine(containers, images)).running(**filters)


def test_containers_on_the_same_image_share_a_group():
    groups = running(
        [
            container("c1", "sonarr", SONARR, OLD, {"com.docker.compose.project": "arr"}),
            container("c2", "sonarr-anime", SONARR, OLD, {"com.docker.compose.project": "arr"}),
        ],
        [OLD_SONARR],
    )
    assert len(groups) == 1
    g = groups[0]
    assert (g.ref, g.image.host, g.containers, g.stacks) == (SONARR, "ghcr.io", ["sonarr", "sonarr-anime"], ["arr"])
    assert (g.local.id, g.local.digests, g.local.platform) == (OLD, {OLD_DIGEST}, "linux/amd64")
    assert g.local.created == datetime(2026, 10, 1, 2, 22, 34, 855525, tzinfo=UTC)
    assert g.local.config["Labels"]["org.opencontainers.image.version"] == "4.0.15.2941-ls290"


def test_recreating_one_container_splits_the_group():
    groups = running(
        [container("c1", "sonarr", SONARR, NEW), container("c2", "sonarr-anime", SONARR, OLD)],
        [OLD_SONARR, NEW_SONARR],
    )
    assert {(g.containers[0], g.local.id) for g in groups} == {("sonarr", NEW), ("sonarr-anime", OLD)}


def test_repo_digests_matched_by_canonical_name():
    groups = running(
        [container("c1", "redis", "docker.io/library/redis:8.8-alpine", OLD)],
        [image(OLD, f"redis@{OLD_DIGEST}", f"registry.lan:5000/cache/redis@{NEW_DIGEST}")],
    )
    assert groups[0].local.digests == {OLD_DIGEST}


def test_skips_pinned_and_locally_built_images():
    groups = running(
        [
            container("c1", "db", f"postgres:18-alpine@{OLD_DIGEST}", OLD),
            container("c2", "myapp", "myapp:dev", NEW),
        ],
        [image(OLD, f"postgres@{OLD_DIGEST}"), image(NEW)],
    )
    assert groups == []


@pytest.mark.parametrize(
    "include, exclude, expected",
    [
        (None, None, ["plain", "opted-in"]),
        ("containertldr.enable=true", None, ["opted-in"]),
        (None, "com.example.skip", ["opted-in"]),
    ],
)
def test_label_filters(include, exclude, expected):
    containers = [
        container("c1", "plain", "plain/app:1", OLD, {"com.example.skip": "yes"}),
        container("c2", "opted-in", "opted/app:1", OLD, {"containertldr.enable": "true"}),
        container("c3", "opted-out", "out/app:1", OLD, {"containertldr.enable": "False"}),
    ]
    digests = [f"{name}@{OLD_DIGEST}" for name in ("plain/app", "opted/app", "out/app")]
    groups = running(containers, [image(OLD, *digests)], include=include, exclude=exclude)
    assert sorted(g.containers[0] for g in groups) == sorted(expected)


def test_collects_containertldr_labels():
    labels = {"containertldr.repo": "Sonarr/Sonarr", "com.docker.compose.project": "arr"}
    groups = running([container("c1", "sonarr", SONARR, OLD, labels)], [OLD_SONARR])
    assert groups[0].labels == {"containertldr.repo": "Sonarr/Sonarr"}


@pytest.mark.parametrize("variant, platform", [("v8", "linux/arm64/v8"), (None, "linux/arm64")])
def test_platform_from_the_running_image(variant, platform):
    img = image(OLD, f"redis@{OLD_DIGEST}", arch="arm64", variant=variant)
    assert running([container("c1", "redis", "redis:8.8-alpine", OLD)], [img])[0].local.platform == platform


def test_talks_to_a_socket_proxy_over_tcp():
    requests = []
    Docker("tcp://socket-proxy:2375", transport=engine([], {}, requests)).running()
    assert str(requests[0].url) == "http://socket-proxy:2375/containers/json"
    assert {r.method for r in requests} == {"GET"}
