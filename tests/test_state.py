import json

import pytest

from containertldr.state import State, same_version, state_key

RADARR = "lscr.io/linuxserver/radarr:latest"
KEY = state_key(RADARR, "sha256:old")


def test_first_run_until_a_file_exists(tmp_path):
    path = tmp_path / "state.json"
    state = State.load(path)
    assert state.first_run
    state.save()
    assert not State.load(path).first_run


def test_lifecycle_of_an_update(tmp_path):
    path = tmp_path / "state.json"

    # A new image for 6.4.5 is found and reported
    state = State.load(path)
    assert not state.reported(KEY, "sha256:r1")
    state.record(KEY, "sha256:r1", "6.4.5.10720-ls320")

    # Next run: same image, nothing to do
    assert State.load(path).reported(KEY, "sha256:r1")

    # A week later LinuxServer rebuilds 6.4.5; same app version, so it isn't reported again
    state = State.load(path)
    assert not state.reported(KEY, "sha256:r2")
    assert state.reported_version(KEY, "6.4.5.10720-ls321")
    state.record(KEY, "sha256:r2", "6.4.5.10720-ls321")

    # Then 6.4.6 comes out, which is new
    assert not State.load(path).reported_version(KEY, "6.4.6.10800-ls322")

    # The user redeploys, so the container runs a new image and the old entry goes
    state.keep_only({state_key(RADARR, "sha256:new")})
    assert State.load(path).entries == {}


def test_forget_when_up_to_date(tmp_path):
    path = tmp_path / "state.json"
    state = State.load(path)
    state.record(KEY, "sha256:r1", "6.4.5")
    state.forget(KEY)
    assert State.load(path).entries == {}


def test_file_format(tmp_path):
    path = tmp_path / "state.json"
    State.load(path).record(KEY, "sha256:r1", "6.4.5.10720-ls320")
    data = json.loads(path.read_text())
    assert data["version"] == 1
    assert data["images"][KEY]["digest"] == "sha256:r1"
    assert data["images"][KEY]["version"] == "6.4.5.10720-ls320"
    assert list(tmp_path.iterdir()) == [path]  # no temp file left behind


def test_readonly_never_writes(tmp_path):
    path = tmp_path / "state.json"
    state = State.load(path, readonly=True)
    state.record(KEY, "sha256:r1", "6.4.5")
    state.save()
    assert not path.exists()
    assert state.reported(KEY, "sha256:r1")


@pytest.mark.parametrize(
    "a, b, expected",
    [
        ("6.4.5.10720-ls320", "6.4.5.10720-ls321", True),
        ("v1.6.2", "1.6.2", True),
        ("v1.6.2-beta.1", "v1.6.2-beta.3", False),
        ("v0.17.0", "v0.17.1", False),
        ("main@de41b24", "main@de41b24", True),
        ("main@de41b24", "main@6911d07", False),
        (None, None, False),
        ("1.0", None, False),
    ],
)
def test_same_version(a, b, expected):
    assert same_version(a, b) is expected


def test_sent_errors_are_remembered(tmp_path):
    path = tmp_path / "state.json"
    state = State.load(path)
    state.errors = {"redis: rate limit reached"}
    state.save()
    assert State.load(path).errors == {"redis: rate limit reached"}
    assert json.loads(path.read_text())["errors"] == ["redis: rate limit reached"]


def test_files_from_0_1_0_still_load(tmp_path):
    path = tmp_path / "state.json"
    path.write_text('{"version": 1, "images": {}}')
    assert State.load(path).errors == set()
