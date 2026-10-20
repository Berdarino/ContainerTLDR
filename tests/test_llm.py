import json
from dataclasses import replace

import httpx
import pytest

from containertldr.config import Config
from containertldr.llm import UNAVAILABLE, Ollama, OpenAICompatible, Summary, llm_for, summarise

REPLY = "**Breaking / action needed:** None mentioned\n**Verdict:** Safe to update"


def answering(response, requests=None):
    """A transport that records each request and answers with response (or raises it)."""

    def handler(request):
        if requests is not None:
            requests.append(request)
        if isinstance(response, Exception):
            raise response
        return response

    return httpx.MockTransport(handler)


def test_ollama():
    requests = []
    # A thinking model can still leave an empty think block in the reply
    response = httpx.Response(200, json={"message": {"content": "<think>\n\n</think>\n\n" + REPLY}})
    llm = Ollama("http://ollama:11434", "qwen3:8b", transport=answering(response, requests))

    assert summarise(llm, "papra", "26.6.2", "26.7.0", "notes") == Summary(REPLY, "qwen3:8b")
    assert requests[0].url == "http://ollama:11434/api/chat"
    body = json.loads(requests[0].content)
    assert (body["model"], body["stream"], body["think"]) == ("qwen3:8b", False, False)
    assert body["options"]["num_ctx"] == 8192
    assert [m["role"] for m in body["messages"]] == ["system", "user"]


def test_openai_compatible():
    requests = []
    response = httpx.Response(200, json={"choices": [{"message": {"role": "assistant", "content": REPLY}}]})
    llm = OpenAICompatible("http://lmstudio:1234/v1", "qwen3-8b", "sk-test", transport=answering(response, requests))

    assert summarise(llm, "papra", "26.6.2", "26.7.0", "notes") == Summary(REPLY, "qwen3-8b")
    assert requests[0].url == "http://lmstudio:1234/v1/chat/completions"
    assert requests[0].headers["Authorization"] == "Bearer sk-test"


def test_openai_compatible_without_key():
    requests = []
    response = httpx.Response(200, json={"choices": [{"message": {"content": REPLY}}]})
    llm = OpenAICompatible("http://llama:8080/v1", "local", transport=answering(response, requests))
    summarise(llm, "a", "1", "2", "")
    assert "Authorization" not in requests[0].headers


@pytest.mark.parametrize(
    "response, error",
    [
        (httpx.ConnectError("Connection refused"), "ConnectError"),
        (httpx.ReadTimeout("timed out"), "ReadTimeout"),
        (httpx.Response(500, text="model failed to load"), "HTTPStatusError"),
        (httpx.Response(200, text="<html>not json</html>"), "JSONDecodeError"),
        (httpx.Response(200, json={"error": "model 'qwen3:8b' not found"}), "KeyError"),
        (httpx.Response(200, json={"message": {"content": ""}}), "empty reply"),
    ],
)
def test_failures_still_give_a_summary(response, error):
    summary = summarise(Ollama("http://ollama:11434", "qwen3:8b", transport=answering(response)), "a", "1", "2", "")
    assert (summary.text, summary.model) == (UNAVAILABLE, None)
    assert summary.error.startswith(error)


def test_llm_for():
    config = Config(notify_urls=["x"])
    assert isinstance(llm_for(config), Ollama)
    assert isinstance(llm_for(replace(config, llm_provider="openai_compat")), OpenAICompatible)
    assert llm_for(replace(config, llm_provider="none")) is None
