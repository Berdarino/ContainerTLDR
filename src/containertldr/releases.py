from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime

from .versions import parse_version


@dataclass(frozen=True)
class Release:
    tag: str
    published: datetime
    body: str
    url: str
    prerelease: bool = False


def select_releases(
    releases: Iterable[Release],
    current: str | None,
    new: str | None,
    since: datetime,
    until: datetime,
    tag_prefix: str = "",
) -> list[Release]:
    """Releases after the running version, up to and including the new one, oldest first.

    If either version is unknown, falls back to releases published between the two image builds
    (since, until], which is only approximate.
    """
    releases = [r for r in releases if r.tag.startswith(tag_prefix)]
    cur_v, new_v = parse_version(current), parse_version(new)

    if cur_v is None or new_v is None:
        picked = [r for r in releases if not r.prerelease and since < r.published <= until]
    else:
        versioned = [(r, parse_version(r.tag.removeprefix(tag_prefix))) for r in releases]
        picked = [r for r, v in versioned if v is not None and cur_v < v <= new_v]
        # Pre-releases only count when the new image is one: a beta tag, or an *arr develop build whose
        # version has no marker but whose GitHub release is flagged as a pre-release
        if not (new_v.is_prerelease or any(r.prerelease and v == new_v for r, v in versioned)):
            picked = [r for r in picked if not r.prerelease]
    return sorted(picked, key=lambda r: r.published)
