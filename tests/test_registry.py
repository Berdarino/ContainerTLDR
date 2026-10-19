import base64
import hashlib
import json
from pathlib import Path

import httpx
import pytest

from containertldr.registry import Registry, RegistryError, parse_ref, pick_platform

HUB = "registry-1.docker.io"
FIXTURES = Path(__file__).parent / "fixtures" / "registry"  # recorded 2026-10-19


def load(name):
    return json.loads((FIXTURES / name).read_text())


RADARR = parse_ref("lscr.io/linuxserver/radarr:latest")
RADARR_DIGEST = "sha256:ff4430fe2d6159b4dbaac9903acfecee5aa68dec8c929259e2f3114c63f7a09e"
GHCR_CHALLENGE = 'Bearer realm="https://ghcr.io/token",service="ghcr.io",scope="repository:linuxserver/radarr:pull"'
CDN = "https://pkg-containers.githubusercontent.com/ghcrblobs04/blobs/sha256:ba5f79fa47?se=2026-10-19T16%3A00Z&sig=x"


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


def ghcr(requests):
    """ghcr.io serving linuxserver/radarr the way it did when the fixtures were recorded."""
    index, manifest, config = load("radarr_index.json"), load("radarr_manifest_arm64.json"), load("radarr_config.json")
    arm64 = next(m["digest"] for m in index["manifests"] if m["platform"]["architecture"] == "arm64")

    def handler(request):
        requests.append(request)
        url = str(request.url)
        if url.startswith("https://ghcr.io/token"):
            return httpx.Response(200, json={"token": "t0k3n"})
        if url == CDN:
            if "Authorization" in request.headers:
                return httpx.Response(400, text="Only one auth mechanism allowed")
            return httpx.Response(200, json=config)
        if request.headers.get("Authorization") != "Bearer t0k3n":
            return httpx.Response(401, headers={"WWW-Authenticate": GHCR_CHALLENGE})
        path = request.url.path.removeprefix("/v2/linuxserver/radarr")
        if path in ("/manifests/latest", f"/manifests/{RADARR_DIGEST}"):
            return httpx.Response(200, json=index, headers={"Docker-Content-Digest": RADARR_DIGEST})
        if path == f"/manifests/{arm64}":
            return httpx.Response(200, json=manifest)
        if path == f"/blobs/{manifest['config']['digest']}":
            return httpx.Response(307, headers={"Location": CDN})
        return httpx.Response(404)

    return httpx.MockTransport(handler)


def responding(*responses, requests=None):
    """Answer with the given responses in order."""
    queue = list(responses)

    def handler(request):
        if requests is not None:
            requests.append(request)
        return queue.pop(0)

    return httpx.MockTransport(handler)


def test_digest_with_anonymous_token():
    requests = []
    assert Registry(transport=ghcr(requests)).digest(RADARR) == RADARR_DIGEST
    assert [(r.method, r.url.path) for r in requests] == [
        ("HEAD", "/v2/linuxserver/radarr/manifests/latest"),
        ("GET", "/token"),
        ("HEAD", "/v2/linuxserver/radarr/manifests/latest"),
    ]
    assert dict(requests[1].url.params) == {"service": "ghcr.io", "scope": "repository:linuxserver/radarr:pull"}
    assert "Authorization" not in requests[1].headers


def test_token_is_reused_for_the_repo():
    requests = []
    registry = Registry(transport=ghcr(requests))
    registry.digest(RADARR)
    registry.digest(RADARR)
    assert sum(r.url.path == "/token" for r in requests) == 1


def test_config_from_index_to_platform_manifest_to_blob():
    requests = []
    config = Registry(transport=ghcr(requests)).config(RADARR, RADARR_DIGEST, "linux/arm64/v8")
    assert config["config"]["Labels"]["org.opencontainers.image.version"] == "6.4.4.10685-ls322"
    assert config["created"] == "2026-10-19T02:22:34.855525258Z"
    assert str(requests[-1].url) == CDN


@pytest.mark.parametrize(
    "index, platform, digest",
    [
        ("redis_index.json", "linux/amd64", "sha256:d7a884c0cf48"),
        ("redis_index.json", "linux/arm64/v8", "sha256:d184fed96d9e"),
        ("redis_index.json", "linux/arm64", "sha256:d184fed96d9e"),
        ("redis_index.json", "linux/arm/v7", "sha256:c46b8ad7180f"),
        ("redis_index.json", "linux/arm/v6", "sha256:a1844c7cd0ab"),
        ("radarr_index.json", "linux/arm64/v8", "sha256:7fa1bc82f4f5"),  # entry has no variant
    ],
)
def test_pick_platform(index, platform, digest):
    assert pick_platform(load(index)["manifests"], platform).startswith(digest)


def test_pick_platform_missing():
    with pytest.raises(RegistryError, match="linux/riscv64"):
        pick_platform(load("radarr_index.json")["manifests"], "linux/riscv64")


def test_docker_hub_token_flow():
    requests = []
    challenge = (
        'Bearer realm="https://auth.docker.io/token",service="registry.docker.io",scope="repository:library/redis:pull"'
    )
    digest = "sha256:0b2b77d3ea5078274795e3177cdbdada8b96316684a38911d528534ed679b5ec"
    transport = responding(
        httpx.Response(401, headers={"WWW-Authenticate": challenge}),
        httpx.Response(200, json={"token": "hub", "access_token": "hub"}),
        httpx.Response(200, headers={"Docker-Content-Digest": digest}),
        requests=requests,
    )
    assert Registry(transport=transport).digest(parse_ref("redis:8.8-alpine")) == digest
    assert str(requests[0].url) == "https://registry-1.docker.io/v2/library/redis/manifests/8.8-alpine"
    assert requests[1].url.host == "auth.docker.io"
    assert requests[2].headers["Authorization"] == "Bearer hub"


def test_credentials_go_to_the_token_endpoint():
    requests = []
    transport = responding(
        httpx.Response(401, headers={"WWW-Authenticate": GHCR_CHALLENGE}),
        httpx.Response(200, json={"token": "t0k3n"}),
        httpx.Response(200, headers={"Docker-Content-Digest": RADARR_DIGEST}),
        requests=requests,
    )
    Registry({"ghcr.io": ("me", "ghp_pat")}, transport=transport).digest(RADARR)
    assert requests[1].headers["Authorization"] == "Basic " + base64.b64encode(b"me:ghp_pat").decode()


def test_digest_falls_back_to_get_without_header():
    body = json.dumps(load("radarr_index.json")).encode()
    transport = responding(httpx.Response(200), httpx.Response(200, content=body))
    assert Registry(transport=transport).digest(RADARR) == "sha256:" + hashlib.sha256(body).hexdigest()


def test_insecure_registry_over_http():
    requests = []
    transport = responding(httpx.Response(200, headers={"Docker-Content-Digest": "sha256:1"}), requests=requests)
    Registry(insecure=["registry:5000"], transport=transport).digest(parse_ref("registry:5000/test/app:1.0"))
    assert str(requests[0].url) == "http://registry:5000/v2/test/app/manifests/1.0"


@pytest.mark.parametrize(
    "responses, error",
    [
        ([httpx.Response(404)], "not found"),
        ([httpx.Response(429)], "rate limit"),
        (
            [
                httpx.Response(401, headers={"WWW-Authenticate": GHCR_CHALLENGE}),
                httpx.Response(200, json={"token": "t0k3n"}),
                httpx.Response(401, headers={"WWW-Authenticate": GHCR_CHALLENGE}),
            ],
            "private images need credentials",
        ),
        ([httpx.Response(401, headers={"WWW-Authenticate": 'Basic realm="Registry"'})], "unsupported"),
    ],
)
def test_errors(responses, error):
    with pytest.raises(RegistryError, match=error):
        Registry(transport=responding(*responses)).digest(RADARR)
