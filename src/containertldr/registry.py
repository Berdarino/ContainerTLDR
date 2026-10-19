from dataclasses import dataclass

DOCKER_HUB = "registry-1.docker.io"

# Registry hosts whose API lives somewhere else
API_HOSTS = {
    "docker.io": DOCKER_HUB,
    "index.docker.io": DOCKER_HUB,
    "lscr.io": "ghcr.io",  # LinuxServer's registry is a front for ghcr.io
}


@dataclass(frozen=True)
class ImageRef:
    name: str  # familiar name, as Docker writes it in RepoDigests: "redis", "lscr.io/linuxserver/radarr"
    host: str  # registry API host
    repo: str  # repository path on that host
    tag: str
    digest: str | None = None


def parse_ref(ref: str) -> ImageRef:
    """'lscr.io/linuxserver/radarr:latest' -> name 'lscr.io/linuxserver/radarr', host 'ghcr.io', ..."""
    name, _, digest = ref.partition("@")
    tag = "latest"
    if ":" in name.rsplit("/", 1)[-1]:
        name, tag = name.rsplit(":", 1)

    first, _, rest = name.partition("/")
    if rest and ("." in first or ":" in first or first == "localhost"):
        host, repo = first, rest
    else:
        host, repo = "docker.io", name

    if host in ("docker.io", "index.docker.io"):
        if "/" not in repo:
            repo = f"library/{repo}"
        name = repo.removeprefix("library/") if repo.count("/") == 1 else repo
    else:
        name = f"{host}/{repo}"

    return ImageRef(name, API_HOSTS.get(host, host), repo, tag, digest or None)
