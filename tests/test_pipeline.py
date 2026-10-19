from datetime import UTC, datetime

import httpx
import pytest

from containertldr.config import Config
from containertldr.discovery import ImageGroup, LocalImage
from containertldr.llm import UNAVAILABLE
from containertldr.messages import Rebuild
from containertldr.notify import NotifyError
from containertldr.pipeline import Checker
from containertldr.registry import RegistryError, parse_ref
from containertldr.releases import Release
from containertldr.resolve import load_mappings
from containertldr.state import State, state_key

REPLY = "**Breaking / action needed:** None mentioned\n**Verdict:** Safe to update"
NOTES = "- Fixed import of movies with special characters in the title\n" * 5
VERSION = "org.opencontainers.image.version"
REVISION = "org.opencontainers.image.revision"


def day(d):
    return datetime.fromisoformat(d).replace(tzinfo=UTC)


def group(ref, labels=None, env=None, created="2026-09-20", container_labels=None):
    image = parse_ref(ref)
    name = image.name.rsplit("/", 1)[-1]
    local = LocalImage(
        f"sha256:id-{name}",
        frozenset({"sha256:old"}),
        {"Labels": labels or {}, "Env": env or []},
        day(created),
        "linux/amd64",
    )
    return ImageGroup(ref, image, local, [name], ["stack"], container_labels or {})


def remote(digest, labels=None, env=None, created="2026-10-15"):
    return digest, {"created": f"{created}T04:00:00Z", "config": {"Labels": labels or {}, "Env": env or []}}


def radarr_release(version, published):
    return Release(f"v{version}", day(published), NOTES, f"https://github.com/Radarr/Radarr/releases/tag/v{version}")


RADARR = "lscr.io/linuxserver/radarr:latest"
RADARR_RELEASES = {
    "Radarr/Radarr": [radarr_release("6.4.5.10720", "2026-10-10"), radarr_release("6.4.4.10685", "2026-10-01")]
}


class FakeDocker:
    def __init__(self, groups):
        self.groups = groups

    def running(self, include=None, exclude=None):
        return self.groups


class FakeRegistry:
    def __init__(self, images):
        self.images = images  # "name:tag" -> (digest, config), or an exception to raise
        self.configs = []

    def digest(self, image):
        return self._find(image)[0]

    def config(self, image, digest, platform):
        self.configs.append(image.name)
        return self._find(image)[1]

    def _find(self, image):
        found = self.images[f"{image.name}:{image.tag}"]
        if isinstance(found, Exception):
            raise found
        return found


class FakeGitHub:
    def __init__(self, releases, compare):
        self.repos, self.compared, self.calls = releases, compare, []

    def releases(self, repo, since):
        self.calls.append(repo)
        return self.repos.get(repo, [])

    def compare(self, repo, base, head):
        self.calls.append(f"{repo} {base[:7]}...{head[:7]}")
        return self.compared


class FakeLLM:
    model = "qwen3:8b"

    def __init__(self, reply):
        self.reply = reply

    def complete(self, messages):
        if isinstance(self.reply, Exception):
            raise self.reply
        return self.reply


class FakeNotifier:
    def __init__(self):
        self.updates, self.rebuilt, self.errors_sent, self.fail = [], [], [], False

    def update(self, update):
        if self.fail:
            raise NotifyError("no notification target took it")
        self.updates.append(update)

    def rebuilds(self, rebuilds):
        self.rebuilt.append(rebuilds)

    def errors(self, errors):
        self.errors_sent.append(errors)


class Homelab:
    """The fakes for one test. check() makes a fresh Checker for every run, like the real loop."""

    def __init__(self, tmp_path, groups, images, releases=None, compare=None, llm=REPLY, first_run=False, **config):
        self.path = tmp_path / "state.json"
        if not first_run:
            State.load(self.path).save()
        self.docker, self.registry = FakeDocker(groups), FakeRegistry(images)
        self.github = FakeGitHub(releases or {}, compare)
        self.llm = FakeLLM(llm) if llm is not None else None
        self.notifier = FakeNotifier()
        self.config = Config(notify_urls=["discord://1/a"], **config)

    def checker(self):
        args = (
            self.docker,
            self.registry,
            self.github,
            self.llm,
            self.notifier,
            State.load(self.path),
            load_mappings(),
        )
        return Checker(self.config, *args)

    def check(self):
        return self.checker().run()

    @property
    def state(self):
        return State.load(self.path)


def radarr_lab(tmp_path, new="6.4.5.10720-ls320", **kwargs):
    groups = [group(RADARR, {VERSION: "6.4.4.10685-ls318"})]
    return Homelab(tmp_path, groups, {RADARR: remote("sha256:new", {VERSION: new})}, RADARR_RELEASES, **kwargs)


def test_update_is_summarised_and_remembered(tmp_path):
    lab = radarr_lab(tmp_path)
    assert lab.check().updates == 1

    update = lab.notifier.updates[0]
    assert update.title == "⬆️ radarr: 6.4.4.10685-ls318 → 6.4.5.10720-ls320"
    assert (update.summary, update.link) == (REPLY, "https://github.com/Radarr/Radarr/releases/tag/v6.4.5.10720")
    assert update.footer == "Summarised by qwen3:8b from 1 release note"
    key = state_key(RADARR, "sha256:id-radarr")
    assert lab.state.reported(key, "sha256:new")

    # Next run: same image in the registry, so nothing new and no second config fetch
    assert lab.check().updates == 0
    assert len(lab.notifier.updates) == 1 and len(lab.registry.configs) == 1


def test_weekly_rebuild_of_a_reported_update_stays_quiet(tmp_path):
    lab = radarr_lab(tmp_path)
    lab.check()
    lab.registry.images[RADARR] = remote("sha256:newer", {VERSION: "6.4.5.10720-ls321"})
    result = lab.check()
    assert (result.updates, result.rebuilds, len(lab.notifier.updates)) == (0, 0, 1)
    assert lab.state.reported(state_key(RADARR, "sha256:id-radarr"), "sha256:newer")
    assert lab.github.calls == ["Radarr/Radarr"]  # only the first run needed GitHub


def test_rebuild_of_the_running_version_is_grouped(tmp_path):
    lab = radarr_lab(tmp_path, new="6.4.4.10685-ls319")
    assert lab.check().rebuilds == 1
    assert lab.notifier.rebuilt == [[Rebuild(["radarr"], "6.4.4.10685-ls319")]]
    assert lab.notifier.updates == [] and lab.github.calls == []


def test_first_run_records_a_baseline(tmp_path):
    lab = radarr_lab(tmp_path, first_run=True)
    assert lab.check().baselined == 1
    assert lab.notifier.updates == []
    assert lab.check().updates == 0  # the baselined update isn't reported later either


def test_first_run_can_notify(tmp_path):
    lab = radarr_lab(tmp_path, first_run=True, initial_run="notify")
    assert lab.check().updates == 1


def test_up_to_date_and_removed_images_are_forgotten(tmp_path):
    lab = radarr_lab(tmp_path)
    state = State.load(lab.path)
    state.record(state_key(RADARR, "sha256:id-radarr"), "sha256:older", "6.4.4.10685-ls317")
    state.record(state_key("ghcr.io/someone/gone:1", "sha256:id-gone"), "sha256:x", "1.0")
    lab.registry.images[RADARR] = remote("sha256:old")
    lab.check()
    assert lab.state.entries == {}


def test_one_broken_image_does_not_stop_the_others(tmp_path):
    lab = radarr_lab(tmp_path)
    lab.docker.groups.insert(0, group("redis:8.8-alpine", env=["REDIS_VERSION=8.8.2"]))
    lab.registry.images["redis:8.8-alpine"] = RegistryError("registry-1.docker.io rate limit reached")
    result = lab.check()
    assert result.updates == 1
    assert result.errors == ["redis: registry-1.docker.io rate limit reached"]
    assert lab.notifier.errors_sent == [result.errors]


def test_label_mistakes_are_reported(tmp_path):
    lab = radarr_lab(tmp_path)
    lab.docker.groups[0] = group(
        RADARR, {VERSION: "6.4.4.10685-ls318"}, container_labels={"containertldr.tag_prefix": "v"}
    )
    assert lab.check().errors[0].startswith("radarr: unknown label containertldr.tag_prefix")


def test_model_down_still_reports_the_update(tmp_path):
    lab = radarr_lab(tmp_path, llm=httpx.ConnectError("Connection refused"))
    lab.check()
    update = lab.notifier.updates[0]
    assert (update.summary, update.footer) == (UNAVAILABLE, "1 release note, summary unavailable")


def test_without_a_model_sends_brief_notes(tmp_path):
    lab = radarr_lab(tmp_path, llm=None)
    lab.check()
    update = lab.notifier.updates[0]
    assert update.summary.startswith("**v6.4.5.10720**\n- Fixed import")
    assert update.footer == "1 release note, not summarised"


def test_failed_notification_is_tried_again_next_run(tmp_path):
    lab = radarr_lab(tmp_path)
    lab.notifier.fail = True
    assert lab.check().errors == ["radarr: no notification target took it"]
    assert lab.state.entries == {}
    lab.notifier.fail = False
    assert lab.check().updates == 1


def test_version_change_without_notes_links_elsewhere(tmp_path):
    lab = Homelab(
        tmp_path,
        [group("postgres:18-alpine", env=["PG_VERSION=18.6"])],
        {"postgres:18-alpine": remote("sha256:new", env=["PG_VERSION=18.7"])},
    )
    lab.check()
    update = lab.notifier.updates[0]
    assert update.title == "⬆️ postgres: 18.6 → 18.7"
    assert update.link == "https://www.postgresql.org/docs/release/"
    assert update.summary == "No detailed release notes for this change, see the link above."
    assert lab.github.calls == []


def test_unknown_versions_go_by_build_dates(tmp_path):
    app = "ghcr.io/someone/app:latest"
    releases = {
        "someone/app": [Release("v2.1.0", day("2026-10-01"), NOTES, "https://github.com/someone/app/releases/v2.1.0")]
    }
    lab = Homelab(tmp_path, [group(app)], {"ghcr.io/someone/app:latest": remote("sha256:new")}, releases)
    lab.check()
    update = lab.notifier.updates[0]
    assert update.title == "⬆️ app: latest built 2026-09-20 → latest built 2026-10-15"
    assert update.footer.endswith(" · repo someone/app guessed from the ghcr path")
    assert lab.state.entries[state_key(app, "sha256:id-app")].version == "v2.1.0"


def test_unknown_versions_and_no_releases_is_a_rebuild(tmp_path):
    lab = Homelab(tmp_path, [group("ghcr.io/someone/app:latest")], {"ghcr.io/someone/app:latest": remote("sha256:new")})
    assert lab.check().rebuilds == 1


@pytest.mark.parametrize("container_labels", [{"containertldr.commits": "true"}, {}])
def test_branch_builds_compare_commits(tmp_path, container_labels):
    old, new = "a" * 40, "b" * 40
    commits = Release(
        f"{old[:7]}...{new[:7]}", day("2026-10-14"), NOTES * 2, "https://github.com/someone/app/compare/x"
    )
    lab = Homelab(
        tmp_path,
        [group("ghcr.io/someone/app:main", {VERSION: "main", REVISION: old}, container_labels=container_labels)],
        {"ghcr.io/someone/app:main": remote("sha256:new", {VERSION: "main", REVISION: new})},
        compare=commits,
    )
    lab.check()
    update = lab.notifier.updates[0]
    assert update.title == "⬆️ app: main@aaaaaaa → main@bbbbbbb"
    assert update.link == "https://github.com/someone/app/compare/x"
    assert update.footer.startswith("Summarised by qwen3:8b from 10 commits")
    assert lab.github.calls == ["someone/app aaaaaaa...bbbbbbb"]


def test_test_mode(tmp_path):
    releases = [radarr_release(f"6.4.{i}", f"2026-0{i + 1}-01") for i in range(5)]
    releases.append(Release("v6.5.0-beta.1", day("2026-07-01"), NOTES, "", prerelease=True))
    lab = Homelab(tmp_path, [group(RADARR)], {}, {"Radarr/Radarr": releases})
    lab.checker().test("radarr")
    update = lab.notifier.updates[0]
    assert update.title == "[TEST] ⬆️ radarr: v6.4.1 → v6.4.4"
    assert update.footer == "Summarised by qwen3:8b from 3 release notes"
    assert lab.registry.configs == [] and lab.state.entries == {}
    with pytest.raises(ValueError, match="no running container called sonarr"):
        lab.checker().test("sonarr")
