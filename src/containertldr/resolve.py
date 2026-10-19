import re
from dataclasses import dataclass
from pathlib import Path

import yaml

from .discovery import LABEL_PREFIX
from .registry import ImageRef, parse_ref

MAPPINGS = Path(__file__).with_name("mappings.yaml")
# containertldr.<label> -> the field it sets
LABELS = {
    "repo": "repo",
    "version": "version",
    "tag-prefix": "tag_prefix",
    "notes-url": "notes_url",
    "commits": "commits",
}
OCI_SOURCE = "org.opencontainers.image.source"
_GITHUB_URL = re.compile(r"https?://github\.com/([\w.-]+)/([\w.-]+?)(?:\.git)?/?")


@dataclass(frozen=True)
class Upstream:
    repo: str | None  # GitHub owner/name
    version: str | None = "label"  # where the app version is: "label", "env:NAME", or None to use build dates
    tag_prefix: str = ""
    commits: bool = False
    notes_url: str | None = None
    found_by: str = "none"  # how repo was found, for the notification footer


def load_mappings(path: Path = MAPPINGS) -> dict[str, dict]:
    return {_key(name): entry for name, entry in yaml.safe_load(path.read_text()).items()}


def resolve(
    image: ImageRef, labels: dict[str, str], image_labels: dict[str, str] | None, mappings: dict[str, dict]
) -> Upstream:
    """Where an image's release notes are and how to read its version.

    labels are the containertldr.* labels on its containers, image_labels the image's own. The repo comes from
    the containertldr.repo label, then the built-in mappings, then the image's OCI source label, then its
    ghcr.io path. Raises ValueError for a label it doesn't understand, so typos don't go unnoticed.
    """
    entry = dict(mappings.get(_key(image.name), {}))
    for key, value in labels.items():
        name = key.removeprefix(LABEL_PREFIX)
        if name in LABELS:
            entry[LABELS[name]] = value
        elif name != "enable":
            known = ", ".join(LABEL_PREFIX + n for n in ["enable", *LABELS])
            raise ValueError(f"unknown label {key}, expected one of {known}")
    found_by = "label" if LABEL_PREFIX + "repo" in labels else "mappings"
    if "repo" not in entry:  # an explicit repo: null in the mappings means there's nothing to guess
        entry["repo"], found_by = _guess_repo(image, image_labels or {})

    version = entry.get("version", "label")
    if version not in ("label", "none", None) and not str(version).startswith("env:"):
        raise ValueError(f"{LABEL_PREFIX}version should be label, env:NAME or none, not {version!r}")
    commits = entry.get("commits", False)
    return Upstream(
        repo=entry["repo"],
        version=None if version in ("none", None) else version,
        tag_prefix=entry.get("tag_prefix") or "",
        commits=commits is True or str(commits).lower() == "true",
        notes_url=entry.get("notes_url"),
        found_by=found_by if entry["repo"] else "none",
    )


def _guess_repo(image: ImageRef, image_labels: dict[str, str]) -> tuple[str | None, str]:
    if m := _GITHUB_URL.fullmatch(image_labels.get(OCI_SOURCE, "")):
        return f"{m[1]}/{m[2]}", "image source label"
    if image.host == "ghcr.io" and image.repo.count("/") == 1:
        return image.repo, "ghcr path"
    return None, "none"


def _key(name: str) -> str:
    # Docker Hub names in their short form, and lscr.io under ghcr.io since it's the same registry
    name = parse_ref(name).name
    return "ghcr.io/" + name.removeprefix("lscr.io/") if name.startswith("lscr.io/") else name
