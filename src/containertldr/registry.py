import hashlib
import re
from collections.abc import Iterable
from dataclasses import dataclass

import httpx

DOCKER_HUB = "registry-1.docker.io"

# Registry hosts whose API lives somewhere else
API_HOSTS = {
    "docker.io": DOCKER_HUB,
    "index.docker.io": DOCKER_HUB,
    "lscr.io": "ghcr.io",  # LinuxServer's registry is a front for ghcr.io
}

MANIFEST_TYPES = ", ".join(
    [
        "application/vnd.oci.image.index.v1+json",
        "application/vnd.docker.distribution.manifest.list.v2+json",
        "application/vnd.oci.image.manifest.v1+json",
        "application/vnd.docker.distribution.manifest.v2+json",
    ]
)


class RegistryError(Exception):
    pass


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


def pick_platform(manifests: list[dict], platform: str) -> str:
    """Digest of the index entry for platform, e.g. "linux/arm64/v8". Attestations ("unknown") never match."""
    os_name, arch, *variant = platform.split("/")
    matches = [m for m in manifests if (m["platform"]["os"], m["platform"]["architecture"]) == (os_name, arch)]
    if not matches:
        raise RegistryError(f"no {platform} image in the index")
    exact = [m for m in matches if m["platform"].get("variant") in variant]
    return (exact or matches)[0]["digest"]


class Registry:
    """Read-only registry v2 client.

    HEAD requests don't count against Docker Hub's pull limit but GETs do, so only call config() for images
    whose digest has changed.
    """

    def __init__(
        self,
        credentials: dict[str, tuple[str, str]] | None = None,
        insecure: Iterable[str] = (),
        transport: httpx.BaseTransport | None = None,
    ):
        self.credentials = credentials or {}  # API host -> (username, password or token)
        self.insecure = set(insecure)  # hosts served over plain http, like a local registry:2
        self.http = httpx.Client(headers={"User-Agent": "containertldr"}, timeout=30, transport=transport)
        self._tokens: dict[tuple[str, str], str] = {}

    def digest(self, ref: ImageRef) -> str:
        r = self._request("HEAD", ref, f"/manifests/{ref.tag}")
        if digest := r.headers.get("Docker-Content-Digest"):
            return digest
        # Some registries leave the header out of HEAD responses
        r = self._request("GET", ref, f"/manifests/{ref.tag}")
        return r.headers.get("Docker-Content-Digest") or "sha256:" + hashlib.sha256(r.content).hexdigest()

    def config(self, ref: ImageRef, digest: str, platform: str) -> dict:
        """Config blob (created, config.Labels, config.Env) of the image behind digest, for platform."""
        manifest = self._request("GET", ref, f"/manifests/{digest}").json()
        if "manifests" in manifest:  # multi-platform index
            platform_digest = pick_platform(manifest["manifests"], platform)
            manifest = self._request("GET", ref, f"/manifests/{platform_digest}").json()
        r = self._request("GET", ref, f"/blobs/{manifest['config']['digest']}", accept="*/*")
        if r.is_redirect:
            # Blobs are usually served from a CDN or S3, which can reject the registry's token
            r = _check(self.http.get(r.url.join(r.headers["Location"])), ref)
        return r.json()

    def _request(self, method: str, ref: ImageRef, path: str, accept: str = MANIFEST_TYPES) -> httpx.Response:
        scheme = "http" if ref.host in self.insecure else "https"
        url = f"{scheme}://{ref.host}/v2/{ref.repo}{path}"
        key = (ref.host, ref.repo)
        r = self.http.request(method, url, headers=self._headers(key, accept))
        if r.status_code == 401:  # no token yet, or it expired
            self._tokens[key] = self._token(ref, r.headers.get("WWW-Authenticate", ""))
            r = self.http.request(method, url, headers=self._headers(key, accept))
        return _check(r, ref)

    def _headers(self, key: tuple[str, str], accept: str) -> dict[str, str]:
        token = self._tokens.get(key)
        return {"Accept": accept, "Authorization": f"Bearer {token}"} if token else {"Accept": accept}

    def _token(self, ref: ImageRef, challenge: str) -> str:
        """Token for the challenge 'Bearer realm="...",service="...",scope="..."', anonymous without credentials."""
        params = dict(re.findall(r'(\w+)="([^"]*)"', challenge))
        if not challenge.lower().startswith("bearer") or "realm" not in params:
            raise RegistryError(f"{ref.host} wants an unsupported kind of auth: {challenge[:100]}")
        query = {k: params[k] for k in ("service", "scope") if k in params}
        query.setdefault("scope", f"repository:{ref.repo}:pull")
        r = self.http.get(params["realm"], params=query, auth=self.credentials.get(ref.host))
        if r.status_code in (401, 403):
            raise RegistryError(f"{ref.host} refused a token for {ref.repo}, check the credentials")
        r.raise_for_status()
        data = r.json()
        return data.get("token") or data["access_token"]


def _check(r: httpx.Response, ref: ImageRef) -> httpx.Response:
    if r.status_code == 401:
        raise RegistryError(f"{ref.host} refused access to {ref.repo}, private images need credentials")
    if r.status_code == 404:
        raise RegistryError(f"not found on {ref.host}: {r.url.path}")
    if r.status_code == 429:
        raise RegistryError(f"{ref.host} rate limit reached, credentials raise it on Docker Hub")
    if r.status_code >= 400:
        r.raise_for_status()
    return r
