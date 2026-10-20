import logging
from dataclasses import dataclass
from datetime import datetime

import httpx

from .registry import ImageRef, parse_ref

log = logging.getLogger(__name__)

LABEL_PREFIX = "containertldr."
COMPOSE_PROJECT = "com.docker.compose.project"


@dataclass(frozen=True)
class LocalImage:
    id: str
    digests: frozenset[str]  # registry digests under the name the containers use
    config: dict  # Labels and Env
    created: datetime
    platform: str  # "linux/arm64/v8"
    built_here: bool = False


@dataclass(frozen=True)
class ImageGroup:
    """Running containers that use the same image reference and the same image behind it."""

    ref: str  # as the containers name it: "lscr.io/linuxserver/sonarr:latest"
    image: ImageRef
    local: LocalImage
    containers: list[str]
    stacks: list[str]  # compose projects
    labels: dict[str, str]  # containertldr.* labels from the containers


class Docker:
    """Read-only Docker Engine API client. Only GETs, so it works through a socket proxy with POST=0."""

    def __init__(self, host: str = "unix:///var/run/docker.sock", transport: httpx.BaseTransport | None = None):
        if host.startswith("unix://"):
            transport = transport or httpx.HTTPTransport(uds=host.removeprefix("unix://"))
            base_url = "http://docker"
        else:
            base_url = host.replace("tcp://", "http://", 1)
        self.http = httpx.Client(base_url=base_url, timeout=30, transport=transport)

    def running(self, include: str | None = None, exclude: str | None = None) -> list[ImageGroup]:
        """Running containers grouped by image reference and image ID.

        The image ID comes from the container, not the tag, which may already point to a newer image that was
        pulled but not deployed. So two containers on the same tag where only one was recreated end up in
        separate groups.
        """
        by_image: dict[tuple[str, str], list[dict]] = {}
        for summary in self._get("/containers/json"):
            c = self._get(f"/containers/{summary['Id']}/json")
            ref = c["Config"]["Image"]
            if "@" in ref:
                log.debug("skipping %s: pinned to a digest", ref)
            elif _wanted(c["Config"].get("Labels") or {}, include, exclude):
                by_image.setdefault((ref, c["Image"]), []).append(c)

        groups = []
        for (ref, image_id), containers in sorted(by_image.items()):
            image = parse_ref(ref)
            local = self._local_image(image_id, image.name)
            if not local.digests:
                log.debug("skipping %s: no registry digest, probably built locally", ref)
                continue
            labels = [c["Config"].get("Labels") or {} for c in containers]
            groups.append(
                ImageGroup(
                    ref=ref,
                    image=image,
                    local=local,
                    containers=sorted(c["Name"].lstrip("/") for c in containers),
                    stacks=sorted({lb[COMPOSE_PROJECT] for lb in labels if COMPOSE_PROJECT in lb}),
                    labels={k: v for lb in labels for k, v in lb.items() if k.startswith(LABEL_PREFIX)},
                )
            )
        return groups

    def _local_image(self, image_id: str, name: str) -> LocalImage:
        d = self._get(f"/images/{image_id}/json")
        # An image can carry digests for several names; only the one the containers use counts. Comparing
        # parsed names makes "redis" and "docker.io/library/redis" the same.
        digests = frozenset(r.digest for r in map(parse_ref, d.get("RepoDigests") or []) if r.name == name and r.digest)
        platform = "/".join(filter(None, [d["Os"], d["Architecture"], d.get("Variant")]))
        # With the containerd image store (Docker 29's default), local builds get a RepoDigest too, so only
        # Identity tells them apart from pulled images. Older Docker has no Identity at all.
        identity = d.get("Identity") or {}
        built_here = bool(identity.get("Build")) and not identity.get("Pull")
        created = datetime.fromisoformat(d["Created"])
        return LocalImage(image_id, digests, d.get("Config") or {}, created, platform, built_here)

    def _get(self, path: str):
        r = self.http.get(path)
        r.raise_for_status()
        return r.json()


def _wanted(labels: dict[str, str], include: str | None, exclude: str | None) -> bool:
    if labels.get(f"{LABEL_PREFIX}enable", "").lower() == "false":
        return False
    if include and not _has_label(labels, include):
        return False
    return not (exclude and _has_label(labels, exclude))


def _has_label(labels: dict[str, str], spec: str) -> bool:
    """spec is "key" or "key=value"."""
    key, sep, value = spec.partition("=")
    return key in labels and (not sep or labels[key] == value)
