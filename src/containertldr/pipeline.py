import logging
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime

from .config import Config
from .discovery import Docker, ImageGroup
from .github import GitHub, GitHubError
from .llm import Ollama, OpenAICompatible, summarise
from .messages import Rebuild, Update
from .notes import brief_notes, build_notes
from .notify import Notifier, NotifyError
from .registry import Registry, RegistryError, Unavailable
from .releases import Release, select_releases
from .resolve import Upstream, load_mappings, resolve
from .state import State, state_key
from .versions import parse_version, read_version

log = logging.getLogger(__name__)

MIN_NOTES_CHARS = 200  # less than this is usually just a link to a changelog somewhere else
REVISION = "org.opencontainers.image.revision"


@dataclass
class RunResult:
    updates: int = 0
    rebuilds: int = 0
    baselined: int = 0
    errors: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class Change:
    """What changed between the running image and the registry's, before anything is summarised."""

    group: ImageGroup
    digest: str
    upstream: Upstream
    current: str  # as shown in the title
    new: str
    version: str | None  # what the state remembers it as, see State.reported_version
    releases: list[Release]
    rebuild: bool
    unit: str = "release note"


class Checker:
    """One check of every running container. Make a new one for each run, so caches and state are fresh."""

    def __init__(
        self,
        config: Config,
        docker: Docker,
        registry: Registry,
        github: GitHub,
        llm: Ollama | OpenAICompatible | None,
        notifier: Notifier,
        state: State,
        mappings: dict[str, dict],
    ):
        self.config = config
        self.docker = docker
        self.registry = registry
        self.github = github
        self.llm = llm
        self.notifier = notifier
        self.state = state
        self.mappings = mappings

    @classmethod
    def from_config(cls, config: Config, dry_run: bool = False) -> "Checker":
        llm: Ollama | OpenAICompatible | None = None
        if config.llm_provider == "ollama":
            llm = Ollama(config.llm_url, config.llm_model, config.llm_num_ctx)
        elif config.llm_provider == "openai_compat":
            llm = OpenAICompatible(config.llm_url, config.llm_model, config.llm_api_key)
        return cls(
            config,
            Docker(config.docker_host),
            Registry(config.registry_credentials, config.insecure_registries),
            GitHub(config.github_token, config.github_url),
            llm,
            Notifier(config.notify_urls, dry_run=dry_run),
            State.load(config.data_dir / "state.json", readonly=dry_run),
            load_mappings(),
        )

    def run(self) -> RunResult:
        result = RunResult()
        groups = self.docker.running(self.config.include_label, self.config.exclude_label)
        self.state.keep_only({state_key(g.ref, g.local.id) for g in groups})
        baseline = self.state.first_run and self.config.initial_run == "baseline"
        rebuilds = []
        for group in groups:
            try:
                change = self._change(group)
                if change is None:
                    continue
                key = state_key(group.ref, group.local.id)
                if baseline:
                    log.info("baseline, not reported: %s %s -> %s", group.ref, change.current, change.new)
                    self.state.record(key, change.digest, change.version)
                    result.baselined += 1
                elif change.rebuild:
                    rebuilds.append(change)
                else:
                    self.notifier.update(self._message(change))
                    self.state.record(key, change.digest, change.version)  # only once it's been sent
                    result.updates += 1
            except Exception as e:  # one broken image must never stop the others
                log.warning("%s: %s", group.ref, _describe(e), exc_info=log.isEnabledFor(logging.DEBUG))
                result.errors.append(f"{', '.join(group.containers)}: {_describe(e)}")

        self._report_rebuilds(rebuilds, result)
        self._report_errors(result.errors)
        self.state.save()  # also leaves a file behind after a first run that had nothing to record
        return result

    def test(self, container: str) -> None:
        """Post a [TEST] summary of the app's last few releases, pretending the oldest of them runs. Ignores state."""
        group = next((g for g in self.docker.running() if container in g.containers), None)
        if group is None:
            raise ValueError(f"no running container called {container}")
        upstream = resolve(group.image, group.labels, group.local.config.get("Labels"), self.mappings)
        if not upstream.repo:
            raise ValueError(f"no GitHub repo known for {container}, set the containertldr.repo label")
        stable = [
            r
            for r in self.github.releases(upstream.repo, datetime.now(UTC))
            if not r.prerelease
            and r.tag.startswith(upstream.tag_prefix)
            and parse_version(r.tag.removeprefix(upstream.tag_prefix))
        ]
        recent = sorted(stable, key=lambda r: r.published)[-4:]
        if len(recent) < 2:
            raise ValueError(f"{upstream.repo} doesn't have enough releases to test with")
        change = Change(group, "", upstream, recent[0].tag, recent[-1].tag, None, recent[1:], rebuild=False)
        self.notifier.update(replace(self._message(change), test=True))

    def _change(self, group: ImageGroup) -> Change | None:
        """None when there's nothing to report: up to date, or this change has been reported already."""
        key = state_key(group.ref, group.local.id)
        try:
            digest = self.registry.digest(group.image)  # a HEAD, which Docker Hub doesn't count
        except Unavailable:
            if not group.local.built_here:
                raise  # e.g. a pulled private image without credentials, which the user should hear about
            log.debug("skipping %s: built on this machine and not in the registry", group.ref)
            return None
        if digest in group.local.digests:
            self.state.forget(key)
            return None
        if self.state.reported(key, digest):
            return None

        remote = self.registry.config(group.image, digest, group.local.platform)
        remote_config = remote.get("config") or {}
        upstream = resolve(group.image, group.labels, remote_config.get("Labels"), self.mappings)
        current, new = read_version(group.local.config, upstream.version), read_version(remote_config, upstream.version)
        cur_v, new_v = parse_version(current), parse_version(new)
        since, until = group.local.created, datetime.fromisoformat(remote["created"])
        old_rev = (group.local.config.get("Labels") or {}).get(REVISION)
        new_rev = (remote_config.get("Labels") or {}).get(REVISION)

        # Built from a branch (the commits label, or a version like "main"): compare the two builds' revisions
        if upstream.repo and old_rev and new_rev and (upstream.commits or (new and new_v is None)):
            current, new = f"{group.image.tag}@{old_rev[:7]}", f"{group.image.tag}@{new_rev[:7]}"
            if self.state.reported_version(key, new):
                return self._quietly(key, digest, new)
            if old_rev == new_rev:
                return Change(group, digest, upstream, current, new, new, [], rebuild=True)
            commits = self.github.compare(upstream.repo, old_rev, new_rev)
            rebuild = not commits.body
            return Change(group, digest, upstream, current, new, new, [commits], rebuild, unit="commit")

        if cur_v is not None and new_v is not None:
            if self.state.reported_version(key, new):  # e.g. LinuxServer's weekly rebuild of an update already sent
                return self._quietly(key, digest, new)
            if cur_v == new_v:
                return Change(group, digest, upstream, current, new, new, [], rebuild=True)

        releases = []
        if upstream.repo:
            found = self.github.releases(upstream.repo, since)
            releases = select_releases(found, current, new, since, until, upstream.tag_prefix)
        if cur_v is not None and new_v is not None:
            return Change(group, digest, upstream, current, new, new, releases, rebuild=False)

        # No usable versions: go by the releases published between the two builds
        newest = releases[-1].tag if releases else None
        if self.state.reported_version(key, newest):
            return self._quietly(key, digest, newest)
        return Change(
            group,
            digest,
            upstream,
            current=f"{group.image.tag} built {since:%Y-%m-%d}",
            new=f"{group.image.tag} built {until:%Y-%m-%d}",
            version=newest,
            releases=releases,
            rebuild=not releases,
        )

    def _quietly(self, key: str, digest: str, version: str | None) -> None:
        """Remember a new digest without reporting it: the change it carries was reported already."""
        self.state.record(key, digest, version)

    def _message(self, change: Change) -> Update:
        group, upstream, releases = change.group, change.upstream, change.releases
        notes = build_notes(releases, self.config.notes_max_chars)
        count = len(releases[0].body.splitlines()) if change.unit == "commit" else notes.count
        counted = f"{count} {change.unit}{'' if count == 1 else 's'}"
        link = releases[-1].url if releases else upstream.notes_url

        if notes.body_chars < MIN_NOTES_CHARS:
            link = upstream.notes_url or link  # Tailscale's releases are empty; its changelog lives elsewhere
            summary = "No detailed release notes for this change" + (", see the link above." if link else ".")
            footer = "No usable release notes on GitHub"
        elif self.llm is None:
            summary, footer = brief_notes(releases), f"{counted}, not summarised"
        else:
            result = summarise(self.llm, ", ".join(group.containers), change.current, change.new, notes.text)
            summary = result.text
            if result.error:
                log.warning("no summary for %s: %s", group.ref, result.error)
                footer = f"{counted}, summary unavailable"
            else:
                footer = f"Summarised by {result.model} from {counted}"
                if notes.truncated:
                    footer += ", shortened to fit"
        if upstream.found_by in ("image source label", "ghcr path"):
            footer += f" · repo {upstream.repo} guessed from the {upstream.found_by}"
        return Update(group.containers, group.ref, group.stacks, change.current, change.new, summary, link, footer)

    def _report_errors(self, errors: list[str]) -> None:
        """Send the errors that weren't sent before. One that clears up and later comes back is sent again."""
        remembered = self.state.errors & set(errors)
        new = [e for e in errors if e not in self.state.errors]
        if new:
            try:
                self.notifier.errors(new)
                remembered |= set(new)
            except NotifyError as e:
                log.error("couldn't send the error notice: %s", e)  # not remembered, so it's tried again next run
        self.state.errors = remembered

    def _report_rebuilds(self, changes: list[Change], result: RunResult) -> None:
        if not changes:
            return
        if self.config.notify_rebuilds:
            try:
                self.notifier.rebuilds([Rebuild(c.group.containers, c.new) for c in changes])
            except NotifyError as e:
                result.errors.append(f"rebuilt images: {e}")
                return  # not recorded, so they're tried again next run
        for c in changes:
            self.state.record(state_key(c.group.ref, c.group.local.id), c.digest, c.version)
        result.rebuilds += len(changes)


def _describe(e: Exception) -> str:
    # Our own errors are written to be read; for anything else the type helps
    if isinstance(e, (RegistryError, GitHubError, NotifyError, ValueError)):
        return str(e)
    return f"{type(e).__name__}: {e}"
