import re

from packaging.version import InvalidVersion, Version

OCI_VERSION = "org.opencontainers.image.version"

# A dotted number, optionally followed by a pre-release marker: 1.6.1, 1.6.2-beta.3, 2.0.0-rc1, 2026.10.0b2.
# The lookahead stops "-alpine" or "-devel" from reading as alpha or dev.
_VERSION = re.compile(r"\d+(?:\.\d+)+(?:[-.]?(?:alpha|beta|preview|pre|rc|dev|a|b)\.?\d*(?![0-9a-z]))?", re.I)


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


def parse_version(s: str | None) -> Version | None:
    """The version inside s: 'v1.6.1-ls364' -> 1.6.1, 'v1.6.2-beta.3' -> 1.6.2b3."""
    m = _VERSION.search(s or "")
    if not m:
        return None
    try:
        return Version(m[0])
    except InvalidVersion:
        return None
