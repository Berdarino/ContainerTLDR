import asyncio
import json
import logging
import os
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx
from starlette.applications import Starlette
from starlette.concurrency import run_in_threadpool
from starlette.exceptions import HTTPException
from starlette.middleware import Middleware
from starlette.middleware.sessions import SessionMiddleware
from starlette.requests import Request
from starlette.responses import PlainTextResponse, RedirectResponse, Response
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles
from starlette.templating import Jinja2Templates

from ..config import Config, ConfigError, load, read_fixes, read_settings, write_fixes, write_settings
from ..discovery import Docker, ImageGroup
from ..heartbeat import overdue
from ..pipeline import CHECKING, Checker
from ..registry import ImageRef, parse_ref
from ..resolve import GUESSES, Upstream, image_key, load_mappings, resolve
from ..state import Entry, State, state_key
from ..versions import read_version
from .auth import RequireLogin, page_context, posted, same_password, sent_token, session_key
from .fixes import FIELDS, LABEL_NAMES, defaults, fix_from, fix_problems, overridden
from .hooks import SENDERS, Hooks
from .jobs import Jobs, secrets_of, try_docker, try_github, try_llm, try_notify, try_summary
from .markdown import markdown
from .settings import form_rows, problems, updated

log = logging.getLogger(__name__)

HERE = Path(__file__).parent
FAILED_LOGIN_DELAY = 1.0  # seconds, times the failed attempts in a row, up to ten


@dataclass(frozen=True)
class Row:
    """One image on the containers page, resolved the way the next check would."""

    group: ImageGroup
    upstream: Upstream | None  # None when one of its containertldr.* labels is wrong, see error
    version: str | None
    entry: Entry | None  # there while a newer image is known
    error: str | None = None

    @property
    def key(self) -> str:
        return image_key(self.group.image.name)

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


def create_app(
    config: Config,
    docker: Callable[[str], Docker] = Docker,
    env: Mapping[str, str] = os.environ,
    dry_run: bool = False,
) -> Starlette:
    """The web UI. It reads what the scheduled checks leave in the data dir, so a page never waits for a check.

    config is what the process started with. Pages go by the settings in use now, which can have been saved since.
    """
    templates = Jinja2Templates(HERE / "templates", context_processors=[page_context])
    templates.env.filters["local"] = _local
    templates.env.filters["markdown"] = markdown
    mappings = load_mappings()
    clients: dict[str, Docker] = {}  # by Docker host, so a client is kept until the host is changed

    def current() -> Config:
        try:
            return load(env, read_settings(config.data_dir))
        except ConfigError:  # the settings page shows what's wrong
            return config

    def docker_at(host: str) -> Docker:
        if host not in clients:
            clients[host] = docker(host)
        return clients[host]

    def health(request: Request) -> Response:
        """For uptime monitors: 503 when the scheduled check is overdue. Open without a login, and says no more."""
        problem = overdue(config.data_dir)
        return PlainTextResponse(problem or "ok", status_code=503 if problem else 200)

    def status(request: Request) -> Response:
        in_use = current()
        entries = State.load(config.data_dir / "state.json", readonly=True).entries.values()
        context = {
            "last": _read_json(config.data_dir / "last_run.json"),
            "heartbeat": _read_json(config.data_dir / "heartbeat.json"),
            "schedule": in_use.schedule,
            "tz": in_use.tz,
            "pending": sorted((e for e in entries if e.message), key=lambda e: e.at, reverse=True),
        }
        return templates.TemplateResponse(request, "status.html", context)

    def containers(request: Request) -> Response:
        in_use = current()
        # Only the Docker API: registry and GitHub calls would cost rate limit on every page load
        try:
            groups = docker_at(in_use.docker_host).running(in_use.include_label, in_use.exclude_label)
        except httpx.HTTPError as e:
            error = f"Can't reach Docker at {in_use.docker_host}: {e}"
            return templates.TemplateResponse(request, "containers.html", {"error": error})
        try:
            fixes = read_fixes(config.data_dir)
        except ConfigError:  # the settings page shows what's wrong
            fixes = {}
        entries = State.load(config.data_dir / "state.json", readonly=True).entries
        rows = sorted((_row(g, mappings, fixes, entries) for g in groups), key=lambda r: r.group.containers)
        return templates.TemplateResponse(request, "containers.html", {"rows": rows})

    async def settings(request: Request) -> Response:
        if not config.web_password:  # the other pages are open then, so this one shows nothing
            return templates.TemplateResponse(request, "settings.html", {"no_password": True})
        try:
            saved = read_settings(config.data_dir)
        except ConfigError as e:
            return templates.TemplateResponse(request, "settings.html", {"error": str(e)})
        if request.method == "GET":
            return settings_form(request, saved, problems(env, saved), saved="saved" in request.query_params)
        new = updated(saved, await posted(request), env)
        if found := problems(env, new):  # nothing is saved then, and the form keeps what was typed
            return settings_form(request, new, found, status_code=400)
        write_settings(config.data_dir, new)
        log.info("settings saved in the web UI")
        return RedirectResponse("/settings?saved", status_code=303)

    def settings_form(request: Request, settings: dict, found: list[str], status_code=200, **context) -> Response:
        groups = form_rows(env, settings, found)
        placed = {row.problem for group in groups.values() for row in group}
        context |= {"groups": groups, "other_problems": [p for p in found if p not in placed]}
        return templates.TemplateResponse(request, "settings.html", context, status_code=status_code)

    async def image_fix(request: Request) -> Response:
        if not request.path_params["image"]:
            raise HTTPException(404)
        key, in_use = image_key(request.path_params["image"]), current()
        try:
            running = await run_in_threadpool(
                docker_at(in_use.docker_host).running, in_use.include_label, in_use.exclude_label
            )
            fixes = read_fixes(config.data_dir)
        except (httpx.HTTPError, ConfigError) as e:
            return templates.TemplateResponse(request, "fix.html", {"key": key, "error": str(e)})
        groups = [g for g in running if image_key(g.image.name) == key]
        image = groups[0].image if groups else parse_ref(key)
        if request.method == "GET":
            return fix_page(request, key, image, groups, fixes.get(key, {}), saved="saved" in request.query_params)
        form = await posted(request)
        fix = {} if form.get("remove") else fix_from(form)
        if found := fix_problems(image, key, fix, mappings):  # nothing is saved then
            return fix_page(request, key, image, groups, fix, status_code=400, problems=found)
        write_fixes(config.data_dir, {k: v for k, v in (fixes | {key: fix}).items() if v})
        log.info("fix for %s saved in the web UI", key)
        return RedirectResponse(f"/containers/{key}?saved", status_code=303)

    def fix_page(
        request: Request, key: str, image: ImageRef, groups: list, fix: dict, status_code=200, **context
    ) -> Response:
        context |= {
            "key": key,
            "groups": groups,
            "fix": fix,
            "fields": FIELDS,
            "label_names": LABEL_NAMES,
            "defaults": defaults(image, groups, mappings),
            "overridden": overridden(groups),
        }
        return templates.TemplateResponse(request, "fix.html", context, status_code=status_code)

    def check_one(image: str) -> None:
        with CHECKING:
            result = Checker.from_config(current(), dry_run).run(image)
        counts = f"{result.updates} update(s), {result.rebuilds} rebuild(s), {len(result.errors)} error(s)"
        log.info("webhook check of %s: %s", image, counts)

    hooks = Hooks(check_one) if config.webhook_token else None

    async def hook(request: Request) -> Response:
        if not hooks or not same_password(sent_token(request), config.webhook_token or ""):
            return PlainTextResponse("Wrong or missing token.", status_code=401)
        if (read := SENDERS.get(request.path_params["sender"])) is None:
            raise HTTPException(404)
        try:
            images = read(await request.json())
        except ValueError as e:  # not JSON, or not what this sender sends
            return PlainTextResponse(str(e), status_code=400)
        if not images:  # e.g. a sender's own test message, which shouldn't look like a failure there
            return PlainTextResponse("Nothing to check.")
        for image in images:
            hooks.add(image)
        log.info("webhook from %s: checking %s", request.path_params["sender"], ", ".join(images))
        return PlainTextResponse(f"Checking {', '.join(images)}.", status_code=202)

    jobs = Jobs()

    async def testing(request: Request) -> Response:
        if request.method == "GET":
            return templates.TemplateResponse(request, "test.html", {"job": jobs.last})
        form = await posted(request)
        in_use, container = current(), str(form.get("container", ""))
        tests = {
            "notify": ("Notifications", lambda: try_notify(in_use)),
            "llm": ("LLM", lambda: try_llm(in_use)),
            "docker": ("Docker", lambda: try_docker(docker_at(in_use.docker_host), in_use)),
            "github": ("GitHub", lambda: try_github(in_use)),
            "summary": (f"Test summary of {container}", lambda: try_summary(in_use, container)),
        }
        if (chosen := tests.get(str(form.get("test")))) is None:
            raise HTTPException(400, "There's no such test.")
        jobs.start(*chosen, secrets=secrets_of(in_use))
        return RedirectResponse("/settings/test", status_code=303)

    attempts, failures = asyncio.Lock(), 0

    async def login(request: Request) -> Response:
        nonlocal failures
        if request.method == "GET":
            return templates.TemplateResponse(request, "login.html")
        form = await posted(request)
        async with attempts:  # one at a time, so the delay can't be waited out in parallel
            if same_password(str(form.get("password", "")), config.web_password or ""):
                failures = 0
                request.session["login"] = True
                return RedirectResponse("/", status_code=303)
            failures += 1
            log.warning("wrong web UI password from %s", request.client.host if request.client else "unknown")
            await asyncio.sleep(min(failures, 10) * FAILED_LOGIN_DELAY)
        return templates.TemplateResponse(request, "login.html", {"error": "Wrong password."}, status_code=401)

    async def logout(request: Request) -> Response:
        await posted(request)
        request.session.clear()
        return RedirectResponse("/login", status_code=303)

    static = StaticFiles(directory=HERE / "static")
    routes = [
        Route("/", status),
        Route("/containers", containers),
        Route("/settings", settings, methods=["GET", "POST"]),
        Route("/health", health),
        Mount("/static", static),
    ]
    if hooks:
        routes.append(Route("/hooks/{sender}", hook, methods=["POST"]))
    if not config.web_password:
        return Starlette(routes=routes)
    routes += [
        Route("/settings/test", testing, methods=["GET", "POST"]),
        Route("/containers/{image:path}", image_fix, methods=["GET", "POST"]),
        Route("/login", login, methods=["GET", "POST"]),
        Route("/logout", logout, methods=["POST"]),
    ]
    key = session_key(config.data_dir, config.web_password)
    # A cookie name of its own: cookies are shared by every app on the same host, whatever the port
    session = Middleware(SessionMiddleware, secret_key=key, session_cookie="containertldr", same_site="strict")
    return Starlette(routes=routes, middleware=[session, Middleware(RequireLogin)])


def _row(group: ImageGroup, mappings: dict[str, dict], fixes: dict[str, dict], entries: dict[str, Entry]) -> Row:
    entry = entries.get(state_key(group.ref, group.local.id))
    try:
        # The running image's labels; a check reads the new image's, which nearly always say the same
        upstream = resolve(group.image, group.labels, group.local.config.get("Labels"), mappings, fixes)
    except ValueError as e:
        return Row(group, None, None, entry, str(e))
    return Row(group, upstream, read_version(group.local.config, upstream.version), entry)


def _local(iso: str, tz: str) -> str:
    return datetime.fromisoformat(iso).astimezone(ZoneInfo(tz)).strftime("%Y-%m-%d %H:%M")


def _read_json(path: Path) -> dict | None:
    try:
        return json.loads(path.read_text())
    except FileNotFoundError:
        return None
