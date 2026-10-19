import re

OCI_VERSION = "org.opencontainers.image.version"

_NUMBERS = re.compile(r"\d+(?:\.\d+)+")
# 1.2.0-beta.3, 2.0.0-rc1, 1.0.0.dev2 (semver style) or 2026.10.0b2 (PEP 440 style)
_PRERELEASE = re.compile(r"[-.](?:alpha|beta|rc|pre|preview|dev)(?![a-z])|\d(?:a|b|rc)\d+(?![0-9a-z])", re.I)


def read_version(config: dict, source: str | None = "label") -> str | None:
    """Raw version string from an image config, read from "label", "env:NAME" or nothing ("none")."""
    if source == "label":
        return (config.get("Labels") or {}).get(OCI_VERSION)
    if source and source.startswith("env:"):
        prefix = source[4:] + "="
        for item in config.get("Env") or []:
            if item.startswith(prefix):
                return item[len(prefix) :]
    return None


def parse_version(s: str | None) -> tuple[int, ...] | None:
    """First dotted number in s as an int tuple: 'v1.6.1-ls364' -> (1, 6, 1).

    Trailing zeros are dropped so that 1.6 and 1.6.0 compare equal. Only use the result for comparing.
    """
    m = _NUMBERS.search(s or "")
    if not m:
        return None
    parts = [int(x) for x in m[0].split(".")]
    while len(parts) > 1 and parts[-1] == 0:
        parts.pop()
    return tuple(parts)


def is_prerelease(s: str | None) -> bool:
    return bool(_PRERELEASE.search(s or ""))
