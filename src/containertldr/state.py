import json
import os
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path

from .versions import parse_version


@dataclass(frozen=True)
class Entry:
    digest: str  # remote digest last reported
    version: str | None  # what it was reported as: app version, newest release tag, or branch@commit
    at: str
    message: dict | None = None  # title, summary, link and footer of the update as sent; None if nothing was sent


def state_key(ref: str, image_id: str) -> str:
    """Entries are per reference and running image, because two containers on one tag can run different images."""
    return f"{ref} {image_id}"


def same_version(a: str | None, b: str | None) -> bool:
    if a is None or b is None:
        return False
    va, vb = parse_version(a), parse_version(b)
    return va == vb if va is not None and vb is not None else a == b


class State:
    """What has been reported, kept in a JSON file.

    Every change is written straight away, so a run that dies halfway doesn't repeat what it already sent.
    Call save() at the end of each run as well, so that the first run leaves a file behind even when it had
    nothing to record.
    """

    def __init__(
        self,
        path: Path,
        entries: dict[str, Entry],
        first_run: bool,
        readonly: bool = False,
        errors: set[str] | None = None,
    ):
        self.path = path
        self.entries = entries
        self.first_run = first_run
        self.readonly = readonly  # --dry-run
        self.errors = errors or set()  # error lines already sent, so a lasting problem isn't sent every run

    @classmethod
    def load(cls, path: Path, readonly: bool = False) -> "State":
        if not path.exists():
            return cls(path, {}, first_run=True, readonly=readonly)
        data = json.loads(path.read_text())
        entries = {k: Entry(**v) for k, v in data["images"].items()}
        return cls(path, entries, first_run=False, readonly=readonly, errors=set(data.get("errors", [])))

    def reported(self, key: str, digest: str) -> bool:
        entry = self.entries.get(key)
        return entry is not None and entry.digest == digest

    def reported_version(self, key: str, version: str | None) -> bool:
        """True if this version was already reported under an older digest, e.g. LinuxServer's weekly rebuilds."""
        entry = self.entries.get(key)
        return entry is not None and same_version(entry.version, version)

    def record(self, key: str, digest: str, version: str | None, message: dict | None = None) -> None:
        self.entries[key] = Entry(digest, version, datetime.now(UTC).isoformat(timespec="seconds"), message)
        self.save()

    def forget(self, key: str) -> None:
        """The running image is up to date again."""
        if self.entries.pop(key, None):
            self.save()

    def keep_only(self, keys: set[str]) -> None:
        """Drop entries for images that no longer run: the container was removed or redeployed on a newer image."""
        stale = self.entries.keys() - keys
        for key in stale:
            del self.entries[key]
        if stale:
            self.save()

    def save(self) -> None:
        if self.readonly:
            return
        images = {k: asdict(e) for k, e in sorted(self.entries.items())}
        data = {"version": 1, "images": images, "errors": sorted(self.errors)}
        tmp = self.path.with_name(self.path.name + ".tmp")
        tmp.write_text(json.dumps(data, indent=2) + "\n")
        os.replace(tmp, self.path)  # atomic, so a crash never leaves half a file
