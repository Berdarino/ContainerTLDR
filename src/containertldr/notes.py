import re
from dataclasses import dataclass

from .releases import Release

MIN_SHARE = 800  # a release cut shorter than this says too little to be worth including
CUT_MARK = "\n[...]"

_CLEANUP = [
    (re.compile(r"<!--.*?-->", re.S), ""),
    (re.compile(r"!\[[^\]]*\]\([^)]*\)|<img\b[^>]*>", re.I), ""),
    # Link reference definitions: Home Assistant ends every release with hundreds of them
    (re.compile(r"^\[[^\]]+\]:\s*<?https?://.*$", re.M), ""),
    (re.compile(r"\[([^\]]+)\]\(https?://[^)]*\)"), r"\1"),
    (re.compile(r"https://github\.com/[\w.-]+/[\w.-]+/(?:pull|issues)/(\d+)"), r"#\1"),
    # GitHub's generated notes: new contributors and the compare link
    (re.compile(r"^#+ New Contributors$|^.*made their first contribution.*$", re.M), ""),
    (re.compile(r"^\*\*Full Changelog\*\*.*$", re.M), ""),
    (re.compile(r"[ \t]+$", re.M), ""),
    (re.compile(r"\n{3,}"), "\n\n"),
]


@dataclass(frozen=True)
class Notes:
    text: str
    count: int  # releases included
    truncated: bool
    body_chars: int  # cleaned release text before cutting, headers not counted


def clean_body(body: str) -> str:
    """Drop what costs tokens without telling the model anything: comments, images, URLs, contributor lists."""
    body = body.replace("\r\n", "\n")
    for pattern, repl in _CLEANUP:
        body = pattern.sub(repl, body)
    return body.strip()


def build_notes(releases: list[Release], budget: int) -> Notes:
    """Release notes (oldest first) as one text for the LLM, cut to fit in budget characters.

    Short releases are kept whole and the long ones share what's left. If that cuts a release below
    MIN_SHARE, the oldest releases are dropped so the newest ones, including the target, stay readable.
    """
    headers = [f"## {r.tag} ({r.published:%Y-%m-%d})" for r in releases]
    bodies = [clean_body(r.body) for r in releases]

    start = 0
    while True:
        lengths = [len(b) for b in bodies[start:]]
        overhead = sum(len(h) + 3 for h in headers[start:])  # newline after each header, blank line between
        limits = _share(lengths, max(budget - overhead, 0))
        squeezed = any(lim < n and lim < MIN_SHARE for lim, n in zip(limits, lengths, strict=True))
        if not squeezed or start >= len(bodies) - 1:
            break
        start += 1

    parts = [f"{h}\n{_cut(b, lim)}".rstrip() for h, b, lim in zip(headers[start:], bodies[start:], limits, strict=True)]
    truncated = start > 0 or any(lim < n for lim, n in zip(limits, lengths, strict=True))
    return Notes("\n\n".join(parts), len(releases) - start, truncated, sum(len(b) for b in bodies))


def _share(lengths: list[int], budget: int) -> list[int]:
    """Split budget so that short items keep their full length and the rest get equal shares of what's left."""
    limits = [0] * len(lengths)
    order = sorted(range(len(lengths)), key=lengths.__getitem__)
    for done, i in enumerate(order):
        limits[i] = min(lengths[i], budget // (len(order) - done))
        budget -= limits[i]
    return limits


def _cut(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    cut = text[: max(limit - len(CUT_MARK), 0)]
    nl = cut.rfind("\n")
    if nl > len(cut) // 2:
        cut = cut[:nl]
    return cut + CUT_MARK
