import re

from starlette.datastructures import FormData

from ..discovery import LABEL_PREFIX, ImageGroup
from ..registry import ImageRef
from ..resolve import LABELS, resolve

# The containertldr.* labels' fields, which a fix can set for every container on an image at once
FIELDS = {
    "repo": "The GitHub repo with the release notes, as owner/name",
    "version": "Where the image keeps its app version: label, env:NAME, or none to go by build dates",
    "tag_prefix": "Only count release tags starting with this, for repos that release several things",
    "notes_url": "Link to send when there are no release notes on GitHub",
    "commits": "The image is built from a branch: summarise the commits between the two builds",
}
LABEL_NAMES = {field: LABEL_PREFIX + label for label, field in LABELS.items()}
_REPO = re.compile(r"[\w.-]+/[\w.-]+")


def fix_from(form: FormData) -> dict[str, str]:
    return {field: value for field in FIELDS if (value := str(form.get(field, "")).strip())}


def fix_problems(image: ImageRef, key: str, fix: dict[str, str], mappings: dict[str, dict]) -> list[str]:
    """What's wrong with a fix, checked the way labels are."""
    problems = []
    if (repo := fix.get("repo")) and not _REPO.fullmatch(repo):
        problems.append(f"The repo should be owner/name, like Radarr/Radarr, not {repo!r}")
    if (url := fix.get("notes_url")) and not url.startswith(("https://", "http://")):
        problems.append(f"The notes URL should start with https://, not {url!r}")
    try:
        resolve(image, {}, None, mappings, {key: fix})
    except ValueError as e:
        problems.append(str(e))
    return problems


def defaults(image: ImageRef, groups: list[ImageGroup], mappings: dict[str, dict]) -> dict[str, str]:
    """What each field comes to without a fix or labels: the built-in default, or the guess."""
    upstream = resolve(image, {}, groups[0].local.config.get("Labels") if groups else None, mappings)
    return {
        "repo": upstream.repo or "",
        "version": upstream.version or "none",
        "tag_prefix": upstream.tag_prefix,
        "notes_url": upstream.notes_url or "",
        "commits": "true" if upstream.commits else "false",
    }


def overridden(groups: list[ImageGroup]) -> dict[str, list[str]]:
    """The containers whose own labels win over the fix, by field."""
    found: dict[str, list[str]] = {}
    for group in groups:
        for label in group.labels:
            if field := LABELS.get(label.removeprefix(LABEL_PREFIX)):
                found.setdefault(field, []).extend(group.containers)
    return found
