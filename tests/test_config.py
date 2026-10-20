import base64
import json
from pathlib import Path

import pytest

from containertldr.config import Config, ConfigError, load, read_settings, registry_credentials, split_urls

DISCORD = "discord://123/AbC-token"


def config(tmp_path, settings=None, **env):
    # DOCKER_CONFIG points at an empty folder so the developer's own ~/.docker/config.json never leaks in
    return load({"DOCKER_CONFIG": str(tmp_path), **env}, settings)


def test_defaults(tmp_path):
    c = config(tmp_path, CTLDR_NOTIFY_URLS=DISCORD)
    assert c == Config(notify_urls=[DISCORD])
    assert c.data_dir == Path("/data")
    assert c.web_port is None


def test_reports_every_problem_at_once(tmp_path):
    with pytest.raises(ConfigError) as error:
        config(
            tmp_path,
            CTLDR_LLM_PROVIDER="anthropic",
            CTLDR_LLM_NUM_CTX="8k",
            CTLDR_NOTIFY_REBUILDS="sometimes",
            CTLDR_INITIAL_RUN="all",
            CTLDR_SCHEDULE="every day at 4",
            TZ="Mars/Olympus_Mons",
        )
    problems = str(error.value).splitlines()
    assert len(problems) == 7
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
        CTLDR_WEB_PORT="8080",
    )
    assert (c.llm_provider, c.llm_url, c.notify_rebuilds) == ("openai_compat", "http://lmstudio:1234/v1", False)
    assert c.insecure_registries == ["registry:5000", "registry.lan:5000"]
    assert c.tz == "Europe/London"
    assert (c.web_host, c.web_port) == ("0.0.0.0", 8080)


@pytest.mark.parametrize("port", ["http", "0", "70000"])
def test_web_port_must_be_a_port(tmp_path, port):
    with pytest.raises(ConfigError, match="CTLDR_WEB_PORT should be"):
        config(tmp_path, CTLDR_NOTIFY_URLS=DISCORD, CTLDR_WEB_PORT=port)


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


def test_saved_settings_come_after_the_environment(tmp_path):
    saved = {"llm_url": "http://saved:11434", "llm_model": "llama3.2", "notify_rebuilds": "no", "tz": "Asia/Tokyo"}
    c = config(tmp_path, saved, CTLDR_NOTIFY_URLS=DISCORD, CTLDR_LLM_URL="http://env:11434", TZ="Europe/London")
    assert (c.llm_url, c.llm_model, c.notify_rebuilds) == ("http://env:11434", "llama3.2", False)
    assert c.tz == "Europe/London"  # plain TZ is an environment variable too
    assert c.schedule == "30 4 * * *"


def test_saved_secrets(tmp_path):
    (tmp_path / "token").write_text("ghp_from_file\n")
    saved = {"notify_urls": DISCORD, "github_token": "ghp_saved", "llm_api_key": "sk-saved"}
    c = config(tmp_path, saved, GITHUB_TOKEN_FILE=str(tmp_path / "token"))
    assert (c.notify_urls, c.github_token, c.llm_api_key) == ([DISCORD], "ghp_from_file", "sk-saved")


def test_saved_settings_are_checked_the_same_way(tmp_path):
    saved = {"llm_num_ctx": "8k", "llm_provider": "anthropic"}
    with pytest.raises(ConfigError) as error:
        config(tmp_path, saved, CTLDR_NOTIFY_URLS=DISCORD, CTLDR_LLM_PROVIDER="ollama")
    assert str(error.value) == "CTLDR_LLM_NUM_CTX (set in the web UI) should be a positive whole number, not '8k'"


def test_some_settings_only_come_from_the_environment(tmp_path):
    saved = {"data_dir": "/elsewhere", "web_port": "1", "web_host": "127.0.0.1", "web_password": "saved"}
    c = config(tmp_path, saved, CTLDR_NOTIFY_URLS=DISCORD)
    assert (c.data_dir, c.web_port, c.web_host, c.web_password) == (Path("/data"), None, "0.0.0.0", None)


def test_read_settings(tmp_path):
    assert read_settings(tmp_path) == {}
    (tmp_path / "settings.json").write_text(json.dumps({"version": 1, "settings": {"llm_model": "llama3.2"}}))
    assert read_settings(tmp_path) == {"llm_model": "llama3.2"}
    (tmp_path / "settings.json").write_text("{")
    with pytest.raises(ConfigError, match="can't read .*settings.json"):
        read_settings(tmp_path)


def test_repr_hides_secrets(tmp_path):
    secrets = {"GITHUB_TOKEN": "ghp_secret", "CTLDR_LLM_API_KEY": "sk-secret", "CTLDR_WEB_PASSWORD": "pw-secret"}
    c = config(tmp_path, CTLDR_NOTIFY_URLS=DISCORD, **secrets)
    assert not any(secret in repr(c) for secret in ("AbC-token", *secrets.values()))


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
