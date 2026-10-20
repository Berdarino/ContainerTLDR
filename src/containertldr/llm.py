from dataclasses import dataclass

import httpx

from .config import Config
from .prompt import build_messages, clean_reply

MAX_TOKENS = 600  # the reply format needs about 250; this stops a model that rambles
UNAVAILABLE = "Summary unavailable: the model couldn't be reached or gave no usable answer. See the release notes."


@dataclass(frozen=True)
class Summary:
    text: str
    model: str | None = None  # None when no model wrote the text
    error: str | None = None


class Ollama:
    def __init__(
        self,
        url: str,
        model: str,
        num_ctx: int = 8192,
        timeout: float = 600,
        transport: httpx.BaseTransport | None = None,
    ):
        self.model = model
        self.num_ctx = num_ctx
        # The first request waits while Ollama loads the model, so only the connect timeout is short
        self.http = httpx.Client(base_url=url, timeout=httpx.Timeout(timeout, connect=10), transport=transport)

    def complete(self, messages: list[dict[str, str]]) -> str:
        r = self.http.post(
            "/api/chat",
            json={
                "model": self.model,
                "messages": messages,
                "stream": False,
                "think": False,  # qwen3 and other thinking models would otherwise spend the budget reasoning
                "options": {"num_ctx": self.num_ctx, "temperature": 0.2, "num_predict": MAX_TOKENS},
            },
        )
        r.raise_for_status()
        return r.json()["message"]["content"]


class OpenAICompatible:
    """OpenAI's chat completions API, as served by LM Studio, vLLM, llama.cpp, LocalAI and others.

    url is the base URL including /v1, e.g. http://lmstudio:1234/v1.
    """

    def __init__(
        self,
        url: str,
        model: str,
        api_key: str | None = None,
        timeout: float = 600,
        transport: httpx.BaseTransport | None = None,
    ):
        self.model = model
        headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
        self.http = httpx.Client(
            base_url=url, headers=headers, timeout=httpx.Timeout(timeout, connect=10), transport=transport
        )

    def complete(self, messages: list[dict[str, str]]) -> str:
        r = self.http.post(
            "/chat/completions",
            json={"model": self.model, "messages": messages, "temperature": 0.2, "max_tokens": MAX_TOKENS},
        )
        r.raise_for_status()
        return r.json()["choices"][0]["message"]["content"]


def llm_for(config: Config, transport: httpx.BaseTransport | None = None) -> Ollama | OpenAICompatible | None:
    """The model the settings name, or None when CTLDR_LLM_PROVIDER is none."""
    if config.llm_provider == "ollama":
        return Ollama(config.llm_url, config.llm_model, config.llm_num_ctx, transport=transport)
    if config.llm_provider == "openai_compat":
        return OpenAICompatible(config.llm_url, config.llm_model, config.llm_api_key, transport=transport)
    return None


def summarise(llm: Ollama | OpenAICompatible, app: str, current: str, new: str, notes: str) -> Summary:
    """Summary of the notes. Never raises: if the model is down the update still gets reported, without a summary."""
    try:
        reply = llm.complete(build_messages(app, current, new, notes))
    except (httpx.HTTPError, LookupError, TypeError, ValueError) as e:  # down, timed out, or an odd response
        return Summary(UNAVAILABLE, error=f"{type(e).__name__}: {e}")
    text = clean_reply(reply or "")
    if not text:
        return Summary(UNAVAILABLE, error="empty reply")
    return Summary(text, llm.model)
