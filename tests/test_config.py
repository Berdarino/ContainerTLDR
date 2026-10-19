import base64
import json
from pathlib import Path

import pytest

from containertldr.config import Config, ConfigError, load, registry_credentials, split_urls

DISCORD = "discord://123/AbC-token"


def config(tmp_path, **env):
    # DOCKER_CONFIG points at an empty folder so the developer's own ~/.docker/config.json never leaks in
    return load({"DOCKER_CONFIG": str(tmp_path), **env})


def test_defaults(tmp_path):
    c = config(tmp_path, CTLDR_NOTIFY_URLS=DISCORD)
    assert c == Config(notify_urls=[DISCORD])
    assert c.data_dir == Path("/data")


def test_reports_every_problem_at_once(tmp_path):
    with pytest.raises(ConfigError) as error:
        config(
            tmp_path,
            CTLDR_LLM_PROVIDER="anthropic",
            CTLDR_LLM_NUM_CTX="8k",
            CTLDR_NOTIFY_REBUILDS="sometimes",
            CTLDR_INITIAL_RUN="all",
            TZ="Mars/Olympus_Mons",
        )
    problems = str(error.value).splitlines()
    assert len(problems) == 6
    assert problems[0].startswith("CTLDR_NOTIFY_URLS is required")
    assert "CTLDR_LLM_PROVIDER should be one of ollama, openai_compat, none, not 'anthropic'" in problems
    assert any("CTLDR_LLM_NUM_CTX" in p for p in problems)


def test_settings(tmp_path):
    c = config(
        tmp_path,
        CTLDR_NOTIFY_URLS=DISCORD,
        CTLDR_LLM_PROVIDER="OpenAI_Compat",
        CTLDR_LLM_URL="http://lmstudio:1234/v1",
        CTLDR_NOTIFY_REBUILDS="no",
        CTLDR_INSECURE_REGISTRIES="registry:5000, registry.lan:5000",
        CTLDR_TZ="Europe/London",
        TZ="Asia/Tokyo",
    )
    assert (c.llm_provider, c.llm_url, c.notify_rebuilds) == ("openai_compat", "http://lmstudio:1234/v1", False)
    assert c.insecure_registries == ["registry:5000", "registry.lan:5000"]
    assert c.tz == "Europe/London"


def test_split_urls():
    value = f" {DISCORD}, ntfy://ntfy.sh/updates\nmailto://me:pw@mail.lan?to=a@example.com,b@example.com "
    assert split_urls(value) == [
        DISCORD,
        "ntfy://ntfy.sh/updates",
        "mailto://me:pw@mail.lan?to=a@example.com,b@example.com",
    ]


def test_secrets_from_files(tmp_path):
    (tmp_path / "urls").write_text(f"{DISCORD}\n")
    (tmp_path / "token").write_text("ghp_from_file\n")
    c = config(
        tmp_path,
        CTLDR_NOTIFY_URLS_FILE=str(tmp_path / "urls"),
        GITHUB_TOKEN="ghp_from_env",
        GITHUB_TOKEN_FILE=str(tmp_path / "token"),
        CTLDR_LLM_API_KEY="sk-env",
    )
    assert (c.notify_urls, c.github_token, c.llm_api_key) == ([DISCORD], "ghp_from_file", "sk-env")


def test_unreadable_secret_file(tmp_path):
    with pytest.raises(ConfigError, match="GITHUB_TOKEN_FILE: can't read"):
        config(tmp_path, CTLDR_NOTIFY_URLS=DISCORD, GITHUB_TOKEN_FILE=str(tmp_path / "missing"))


def test_repr_hides_secrets(tmp_path):
    c = config(tmp_path, CTLDR_NOTIFY_URLS=DISCORD, GITHUB_TOKEN="ghp_secret", CTLDR_LLM_API_KEY="sk-secret")
    assert not any(secret in repr(c) for secret in ("AbC-token", "ghp_secret", "sk-secret"))


def test_registry_credentials(tmp_path):
    path = tmp_path / "config.json"
    path.write_text(
        json.dumps(
            {
                "auths": {
                    "https://index.docker.io/v1/": {"auth": base64.b64encode(b"me:dckr_pat").decode()},
                    "lscr.io": {"username": "me", "password": "ghp_pat"},
                    "registry.lan:5000": {"auth": base64.b64encode(b"admin:pa:ss").decode()},
                    "empty.example.com": {},
                },
                "credsStore": "desktop",
            }
        )
    )
    assert registry_credentials(path) == {
        "registry-1.docker.io": ("me", "dckr_pat"),
        "ghcr.io": ("me", "ghp_pat"),
        "registry.lan:5000": ("admin", "pa:ss"),
    }
    assert config(tmp_path, CTLDR_NOTIFY_URLS=DISCORD).registry_credentials["ghcr.io"] == ("me", "ghp_pat")


def test_broken_docker_config(tmp_path):
    (tmp_path / "config.json").write_text("{not json")
    with pytest.raises(ConfigError, match="can't read registry logins"):
        config(tmp_path, CTLDR_NOTIFY_URLS=DISCORD)
