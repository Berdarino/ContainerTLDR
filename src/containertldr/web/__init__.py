import json
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import Response
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles
from starlette.templating import Jinja2Templates

from ..config import Config
from ..state import State

HERE = Path(__file__).parent


def create_app(config: Config) -> Starlette:
    """The web UI. It reads what the scheduled checks leave in the data dir, so a page never waits for a check."""
    tz = ZoneInfo(config.tz)
    templates = Jinja2Templates(HERE / "templates")
    templates.env.filters["local"] = lambda iso: datetime.fromisoformat(iso).astimezone(tz).strftime("%Y-%m-%d %H:%M")

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

    static = StaticFiles(directory=HERE / "static")
    return Starlette(routes=[Route("/", status), Mount("/static", static)])


def _read_json(path: Path) -> dict | None:
    try:
        return json.loads(path.read_text())
    except FileNotFoundError:
        return None
