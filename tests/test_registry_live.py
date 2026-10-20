"""The registry client against a real registry:2 in Docker. Skipped where Docker isn't available."""

import hashlib
import json
import shutil
import subprocess
import time

import httpx
import pytest

from containertldr.registry import Registry, Unavailable, parse_ref

REGISTRY_IMAGE = "registry:2.8.3"
VERSION = "org.opencontainers.image.version"
OCI_INDEX = "application/vnd.oci.image.index.v1+json"
OCI_MANIFEST = "application/vnd.oci.image.manifest.v1+json"


@pytest.fixture(scope="module")
def registry():
    """host:port of a throwaway registry on a free local port."""
    if not shutil.which("docker") or subprocess.run(["docker", "info"], capture_output=True).returncode:
        pytest.skip("needs Docker")
    run = ["docker", "run", "-d", "--rm", "-p", "127.0.0.1::5000", REGISTRY_IMAGE]
    container = subprocess.run(run, capture_output=True, text=True, check=True).stdout.strip()
    try:
        port = subprocess.run(["docker", "port", container, "5000"], capture_output=True, text=True, check=True)
        host = port.stdout.splitlines()[0].replace("0.0.0.0", "127.0.0.1")
        for _ in range(100):  # Docker can accept the connection before the registry listens
            try:
                httpx.get(f"http://{host}/v2/").raise_for_status()
                break
            except httpx.HTTPError:
                time.sleep(0.1)
        yield host
    finally:
        subprocess.run(["docker", "rm", "-f", container], capture_output=True)


def push_blob(host: str, repo: str, data: bytes) -> dict:
    digest = "sha256:" + hashlib.sha256(data).hexdigest()
    upload = httpx.post(f"http://{host}/v2/{repo}/blobs/uploads/")
    location = httpx.URL(f"http://{host}").join(upload.headers["Location"])  # carries the upload's own state
    httpx.put(location.copy_merge_params({"digest": digest}), content=data).raise_for_status()
    return {"digest": digest, "size": len(data)}


def push_manifest(host: str, repo: str, reference: str | None, manifest: dict) -> dict:
    """Pushed under reference (a tag), or by digest without one."""
    body = json.dumps(manifest).encode()
    digest = "sha256:" + hashlib.sha256(body).hexdigest()
    headers = {"Content-Type": manifest["mediaType"]}
    httpx.put(
        f"http://{host}/v2/{repo}/manifests/{reference or digest}", content=body, headers=headers
    ).raise_for_status()
    return {"mediaType": manifest["mediaType"], "digest": digest, "size": len(body)}


def push_image(host: str, repo: str, arch: str, version: str, tag: str | None = None) -> dict:
    layer = push_blob(host, repo, f"a {arch} layer; the registry never looks inside".encode())
    config = {
        "architecture": arch,
        "os": "linux",
        "created": "2026-10-15T04:00:00Z",
        "config": {"Labels": {VERSION: version}},
        "rootfs": {"type": "layers", "diff_ids": [layer["digest"]]},
    }
    manifest = {
        "schemaVersion": 2,
        "mediaType": OCI_MANIFEST,
        "config": {
            "mediaType": "application/vnd.oci.image.config.v1+json",
            **push_blob(host, repo, json.dumps(config).encode()),
        },
        "layers": [{"mediaType": "application/vnd.oci.image.layer.v1.tar+gzip", **layer}],
    }
    return push_manifest(host, repo, tag, manifest)


def test_a_multi_platform_image(registry):
    platforms = [{"os": "linux", "architecture": arch} for arch in ("amd64", "arm64")]
    images = [push_image(registry, "someone/app", p["architecture"], "6.4.5") for p in platforms]
    index = {
        "schemaVersion": 2,
        "mediaType": OCI_INDEX,
        "manifests": [{**i, "platform": p} for i, p in zip(images, platforms, strict=True)],
    }
    pushed = push_manifest(registry, "someone/app", "6.4.5", index)

    client, ref = Registry(insecure=[registry]), parse_ref(f"{registry}/someone/app:6.4.5")
    assert client.digest(ref) == pushed["digest"]
    config = client.config(ref, pushed["digest"], "linux/arm64")
    assert (config["architecture"], config["config"]["Labels"][VERSION]) == ("arm64", "6.4.5")


def test_a_single_platform_image(registry):
    pushed = push_image(registry, "someone/tool", "amd64", "1.2.0", tag="latest")
    client, ref = Registry(insecure=[registry]), parse_ref(f"{registry}/someone/tool:latest")
    assert client.digest(ref) == pushed["digest"]
    assert client.config(ref, pushed["digest"], "linux/amd64")["config"]["Labels"][VERSION] == "1.2.0"


def test_a_tag_the_registry_does_not_have(registry):
    with pytest.raises(Unavailable, match="not found"):
        Registry(insecure=[registry]).digest(parse_ref(f"{registry}/someone/app:nope"))
