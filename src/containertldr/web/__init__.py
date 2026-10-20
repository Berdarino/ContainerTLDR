import json
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import Response
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles
from starlette.templating import Jinja2Templates

from ..config import Config
from ..discovery import Docker, ImageGroup
from ..resolve import GUESSES, Upstream, load_mappings, resolve
from ..state import Entry, State, state_key
from ..versions import read_version

HERE = Path(__file__).parent


@dataclass(frozen=True)
class Row:
    """One image on the containers page, resolved the way the next check would."""

    group: ImageGroup
    upstream: Upstream | None  # None when one of its containertldr.* labels is wrong, see error
    version: str | None
    entry: Entry | None  # there while a newer image is known
    error: str | None = None

    @property
    def guessed(self) -> bool:
        return self.upstream is not None and self.upstream.found_by in GUESSES

    @property
    def needs_fix(self) -> bool:
        u = self.upstream
        if u is None:
            return True
        nothing_to_link = u.repo is None and u.notes_url is None
        no_version = u.version is not None and self.version is None and not u.commits
        return nothing_to_link or no_version or self.guessed


def create_app(config: Config, docker: Docker | None = None) -> Starlette:
    """The web UI. It reads what the scheduled checks leave in the data dir, so a page never waits for a check."""
    tz = ZoneInfo(config.tz)
    templates = Jinja2Templates(HERE / "templates")
    templates.env.filters["local"] = lambda iso: datetime.fromisoformat(iso).astimezone(tz).strftime("%Y-%m-%d %H:%M")
    docker = docker or Docker(config.docker_host)
    mappings = load_mappings()

    def status(request: Request) -> Response:
        entries = State.load(config.data_dir / "state.json", readonly=True).entries.values()
        context = {
            "last": _read_json(config.data_dir / "last_run.json"),
            "heartbeat": _read_json(config.data_dir / "heartbeat.json"),
            "schedule": config.schedule,
            "tz": config.tz,
            "pending": sorted((e for e in entries if e.message), key=lambda e: e.at, reverse=True),
        }
        return templates.TemplateResponse(request, "status.html", context)

    def containers(request: Request) -> Response:
        # Only the Docker API: registry and GitHub calls would cost rate limit on every page load
        try:
            groups = docker.running(config.include_label, config.exclude_label)
        except httpx.HTTPError as e:
            error = f"Can't reach Docker at {config.docker_host}: {e}"
            return templates.TemplateResponse(request, "containers.html", {"error": error})
        entries = State.load(config.data_dir / "state.json", readonly=True).entries
        rows = sorted((_row(g, mappings, entries) for g in groups), key=lambda r: r.group.containers)
        return templates.TemplateResponse(request, "containers.html", {"rows": rows})

    static = StaticFiles(directory=HERE / "static")
    return Starlette(routes=[Route("/", status), Route("/containers", containers), Mount("/static", static)])


def _row(group: ImageGroup, mappings: dict[str, dict], entries: dict[str, Entry]) -> Row:
    entry = entries.get(state_key(group.ref, group.local.id))
    try:
        # The running image's labels; a check reads the new image's, which nearly always say the same
        upstream = resolve(group.image, group.labels, group.local.config.get("Labels"), mappings)
    except ValueError as e:
        return Row(group, None, None, entry, str(e))
    return Row(group, upstream, read_version(group.local.config, upstream.version), entry)


def _read_json(path: Path) -> dict | None:
    try:
        return json.loads(path.read_text())
    except FileNotFoundError:
        return None
