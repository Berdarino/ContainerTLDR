import re
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
    releases = sorted((r for r in releases if r.tag.startswith(tag_prefix)), key=lambda r: r.published)
    cur_v, new_v = parse_version(current), parse_version(new)
    if cur_v is None or new_v is None:
        return [r for r in releases if not r.prerelease and since < r.published <= until]

    versioned = [(r, parse_version(r.tag.removeprefix(tag_prefix))) for r in releases]
    # Repos that tag several branches put a word in front (develop-6.4.4, nightly-7.0.0). If some tags are shaped
    # like the running version, only those count.
    versioned = [(r, v) for r, v in versioned if _shape(r.tag.removeprefix(tag_prefix)) == _shape(new)] or versioned
    # Pre-releases only count when the new image is one: a beta tag, or an *arr develop build whose
    # version has no marker but whose GitHub release is flagged as a pre-release
    want_pre = new_v.is_prerelease or any(r.prerelease and v == new_v for r, v in versioned)
    # One release per version, the newest: LinuxServer publishes one for every rebuild, all with the same notes
    by_version = {v: r for r, v in versioned if v is not None and cur_v < v <= new_v and (want_pre or not r.prerelease)}
    return sorted(by_version.values(), key=lambda r: r.published)


def _shape(tag: str) -> str:
    """What comes before the version number, ignoring a "v": 'develop-6.4.4' -> 'develop-', 'v1.2' -> ''."""
    m = re.match(r"\D*", tag)
    return m[0].lower().removesuffix("v") if m else ""
