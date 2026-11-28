"""Model providers for Zorec. All optional: without a provider Zorec answers with rules.

Kinds:

* ``openai``: any OpenAI-compatible chat API (``/v1/chat/completions``): OpenAI,
  Mistral, Groq, OpenRouter, Google Gemini's compatible endpoint, and servers on your
  own hardware such as Ollama, vLLM, llama.cpp or LM Studio.
* ``ollama``: Ollama's native ``/api/chat``.
* ``anthropic``: Claude through the official Anthropic SDK (``opstend[anthropic]``).
* ``cli``: a subscription linked through the provider's own command line tool
  (OpenAI Codex, Gemini CLI). The tool keeps its own sign-in on the node; OpsTend
  never sees the credentials and only passes the prompt on stdin.

Providers only produce text. They never execute anything; actions Zorec suggests
become job previews that a person (or the policy) approves.
"""

from __future__ import annotations

import asyncio
import json
import os
import ssl
import tempfile
import time
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any, Protocol

import httpx
from pydantic import Field

from opstend.core.models import Model


class Kind(StrEnum):
    OPENAI = "openai"
    OLLAMA = "ollama"
    ANTHROPIC = "anthropic"
    CLI = "cli"


class ProviderConfig(Model):
    """One configured model source. Keys live in the vault; only their id is here."""

    id: str = Field(pattern=r"^[a-z0-9][a-z0-9-]{0,39}$")
    label: str
    kind: Kind
    preset: str | None = None
    url: str | None = None
    model: str | None = None
    secret_id: str | None = None  # API key in the vault
    ca_pem: str | None = None  # trust a private CA for a server in your network
    context_tokens: int = Field(default=8192, ge=1024, le=2_000_000)
    max_reply_tokens: int = Field(default=800, ge=64, le=32000)
    local: bool = False  # data stays in your network
    command: str | None = None  # cli: codex | gemini
    timeout_s: int = Field(default=180, ge=10, le=900)
    temperature: float | None = Field(default=None, ge=0, le=2)


@dataclass
class Reply:
    text: str
    model: str
    refused: bool = False
    seconds: float = 0.0
    tokens_in: int | None = None
    tokens_out: int | None = None


class Provider(Protocol):
    config: ProviderConfig

    @property
    def model(self) -> str: ...

    async def complete(
        self, system: str, messages: list[dict[str, str]], max_tokens: int | None = None
    ) -> Reply: ...

    async def models(self) -> list[str]: ...


class ProviderError(RuntimeError):
    pass


# --- presets shown when adding a provider ------------------------------------------


@dataclass(frozen=True)
class Preset:
    id: str
    label: str
    kind: Kind
    url: str | None = None
    needs_key: bool = True
    local: bool = False
    command: str | None = None
    context_tokens: int = 128000
    note: str = ""
    docs: str = ""
    model_patterns: tuple[str, ...] = field(default_factory=tuple)


PRESETS: dict[str, Preset] = {
    p.id: p
    for p in (
        Preset(
            "local",
            "Model server in my network (Ollama, vLLM, llama.cpp, LM Studio)",
            Kind.OPENAI,
            url="http://localhost:11434/v1",
            needs_key=False,
            local=True,
            context_tokens=8192,
            note="Nothing leaves your network.",
        ),
        Preset(
            "anthropic",
            "Claude (Anthropic API key)",
            Kind.ANTHROPIC,
            note=(
                "Create a key in the Claude Console. Anthropic does not allow signing in"
                " with a claude.ai subscription in other products, so Claude is linked"
                " with an API key."
            ),
            docs="https://platform.claude.com/docs/en/api/overview",
            model_patterns=(r"^claude-(opus|sonnet|haiku|fable)-[0-9][0-9a-z.-]*$",),
        ),
        Preset(
            "openai",
            "OpenAI API key",
            Kind.OPENAI,
            url="https://api.openai.com/v1",
            docs="https://platform.openai.com/docs/api-reference",
            model_patterns=(r"^gpt-[0-9][0-9a-z.-]*$", r"^o[0-9][0-9a-z.-]*$"),
        ),
        Preset(
            "gemini",
            "Google Gemini API key",
            Kind.OPENAI,
            url="https://generativelanguage.googleapis.com/v1beta/openai",
            docs="https://ai.google.dev/gemini-api/docs/openai",
            model_patterns=(r"^(models/)?gemini-[0-9][0-9a-z.-]*$",),
        ),
        Preset(
            "mistral",
            "Mistral API key",
            Kind.OPENAI,
            url="https://api.mistral.ai/v1",
            docs="https://docs.mistral.ai/api/",
        ),
        Preset(
            "openrouter",
            "OpenRouter API key",
            Kind.OPENAI,
            url="https://openrouter.ai/api/v1",
            docs="https://openrouter.ai/docs",
        ),
        Preset(
            "codex",
            "ChatGPT subscription (OpenAI Codex CLI)",
            Kind.CLI,
            needs_key=False,
            command="codex",
            note=(
                "Install the Codex CLI on the node and sign in once in its console with"
                " `codex login --device-auth`. Check that your plan allows this use."
            ),
            docs="https://developers.openai.com/codex/noninteractive",
        ),
        Preset(
            "gemini-cli",
            "Google account (Gemini CLI)",
            Kind.CLI,
            needs_key=False,
            command="gemini",
            note=(
                "Install the Gemini CLI on the node and sign in once in its console."
                " Check that your plan allows this use."
            ),
            docs="https://geminicli.com/docs/cli/headless/",
        ),
    )
}


# --- HTTP providers -------------------------------------------------------------------


def _client(cfg: ProviderConfig, transport: httpx.AsyncBaseTransport | None) -> httpx.AsyncClient:
    verify: ssl.SSLContext | bool = True
    if cfg.ca_pem:
        verify = ssl.create_default_context(cadata=cfg.ca_pem)
    return httpx.AsyncClient(timeout=cfg.timeout_s, transport=transport, verify=verify)


class OpenAICompatible:
    def __init__(
        self,
        cfg: ProviderConfig,
        api_key: str | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        if not cfg.url:
            raise ProviderError("an API address is required")
        self.config = cfg
        self.url = cfg.url.rstrip("/")
        self.headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
        self.client = _client(cfg, transport)

    @property
    def model(self) -> str:
        return self.config.model or ""

    async def models(self) -> list[str]:
        try:
            resp = await self.client.get(f"{self.url}/models", headers=self.headers)
        except httpx.HTTPError as exc:
            raise ProviderError(f"{self.url} is not reachable") from exc
        if resp.status_code != 200:
            raise ProviderError(f"listing models returned {resp.status_code}")
        return sorted(str(m["id"]).removeprefix("models/") for m in resp.json().get("data", []))

    async def complete(
        self, system: str, messages: list[dict[str, str]], max_tokens: int | None = None
    ) -> Reply:
        if not self.model:
            raise ProviderError("no model selected")
        body = {
            "model": self.model,
            "messages": [{"role": "system", "content": system}, *messages],
            "max_tokens": max_tokens or self.config.max_reply_tokens,
            "stream": False,
        }
        if self.config.temperature is not None:
            body["temperature"] = self.config.temperature
        start = time.monotonic()
        try:
            resp = await self.client.post(
                f"{self.url}/chat/completions", json=body, headers=self.headers
            )
        except httpx.TimeoutException as exc:
            raise ProviderError("the model did not answer in time") from exc
        except httpx.HTTPError as exc:
            raise ProviderError(f"{self.url} is not reachable") from exc
        if resp.status_code == 401:
            raise ProviderError("the API key is not valid")
        if resp.status_code == 429:
            raise ProviderError("the model is busy or rate limited")
        if resp.status_code != 200:
            raise ProviderError(f"the model returned {resp.status_code}")
        data = resp.json()
        choice = (data.get("choices") or [{}])[0]
        usage = data.get("usage") or {}
        return Reply(
            text=(choice.get("message") or {}).get("content") or "",
            model=data.get("model", self.model),
            refused=choice.get("finish_reason") == "content_filter",
            seconds=time.monotonic() - start,
            tokens_in=usage.get("prompt_tokens"),
            tokens_out=usage.get("completion_tokens"),
        )


class Ollama:
    def __init__(
        self,
        cfg: ProviderConfig,
        api_key: str | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.config = cfg
        self.url = (cfg.url or "http://localhost:11434").rstrip("/")
        self.headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
        self.client = _client(cfg, transport)

    @property
    def model(self) -> str:
        return self.config.model or ""

    async def models(self) -> list[str]:
        try:
            resp = await self.client.get(f"{self.url}/api/tags", headers=self.headers)
            resp.raise_for_status()
        except httpx.HTTPError as exc:
            raise ProviderError(f"{self.url} is not reachable") from exc
        return sorted(m["name"] for m in resp.json().get("models", []))

    async def complete(
        self, system: str, messages: list[dict[str, str]], max_tokens: int | None = None
    ) -> Reply:
        body: dict[str, Any] = {
            "model": self.model,
            "stream": False,
            "messages": [{"role": "system", "content": system}, *messages],
            "options": {
                "num_predict": max_tokens or self.config.max_reply_tokens,
                "num_ctx": self.config.context_tokens,
                **(
                    {"temperature": self.config.temperature}
                    if self.config.temperature is not None
                    else {}
                ),
            },
        }
        start = time.monotonic()
        try:
            resp = await self.client.post(f"{self.url}/api/chat", json=body, headers=self.headers)
        except httpx.HTTPError as exc:
            raise ProviderError(f"{self.url} is not reachable") from exc
        if resp.status_code != 200:
            raise ProviderError(f"the model returned {resp.status_code}")
        data = resp.json()
        return Reply(
            data.get("message", {}).get("content", ""),
            data.get("model", self.model),
            seconds=time.monotonic() - start,
            tokens_in=data.get("prompt_eval_count"),
            tokens_out=data.get("eval_count"),
        )


class Anthropic:
    """Claude via the Anthropic SDK."""

    def __init__(self, cfg: ProviderConfig, api_key: str | None = None) -> None:
        try:
            import anthropic
        except ImportError as exc:
            raise ProviderError("install opstend[anthropic] to use Claude") from exc
        if not api_key:
            raise ProviderError("an API key is required")
        self.config = cfg
        self._anthropic = anthropic
        self.client = anthropic.AsyncAnthropic(api_key=api_key, timeout=cfg.timeout_s)

    @property
    def model(self) -> str:
        return self.config.model or "claude-opus-5-5"

    async def models(self) -> list[str]:
        try:
            page = await self.client.models.list(limit=100)
        except self._anthropic.APIError as exc:
            raise ProviderError("listing Claude models failed") from exc
        return sorted(m.id for m in page.data)

    async def complete(
        self, system: str, messages: list[dict[str, str]], max_tokens: int | None = None
    ) -> Reply:
        a = self._anthropic
        start = time.monotonic()
        try:
            # Server-side fallback keeps answers flowing if a request is declined;
            # the final stop_reason still tells us when the whole chain refused.
            response = await self.client.beta.messages.create(
                model=self.model,
                max_tokens=max_tokens or self.config.max_reply_tokens,
                system=system,
                messages=messages,  # type: ignore[arg-type,unused-ignore]
                output_config={"effort": "medium"},
                betas=["server-side-fallback-2026-07-01"],
                fallbacks="default",
            )
        except a.RateLimitError as exc:
            raise ProviderError("Claude is rate limited; try again shortly") from exc
        except a.AuthenticationError as exc:
            raise ProviderError("the Anthropic API key is not valid") from exc
        except a.APIStatusError as exc:
            raise ProviderError(f"Claude returned {exc.status_code}") from exc
        except a.APIConnectionError as exc:
            raise ProviderError("Claude is not reachable from this node") from exc
        seconds = time.monotonic() - start
        usage = response.usage
        if response.stop_reason == "refusal":
            return Reply("", response.model, refused=True, seconds=seconds)
        text = "".join(b.text for b in response.content if b.type == "text")
        return Reply(
            text,
            response.model,
            seconds=seconds,
            tokens_in=usage.input_tokens,
            tokens_out=usage.output_tokens,
        )


# --- subscriptions through the provider's own CLI --------------------------------------


class Cli:
    """Runs the official CLI without tools: read-only, no repository, prompt on stdin."""

    COMMANDS = ("codex", "gemini")

    def __init__(self, cfg: ProviderConfig) -> None:
        if cfg.command not in self.COMMANDS:
            raise ProviderError("unknown command line tool")
        self.config = cfg

    @property
    def model(self) -> str:
        return self.config.model or ""

    async def models(self) -> list[str]:
        # The CLIs pick a default for the plan; a model can be named explicitly.
        return [self.config.model] if self.config.model else []

    def _argv(self, out: str) -> list[str]:
        model = ["-m", self.config.model] if self.config.model else []
        if self.config.command == "codex":
            return [
                "codex",
                "exec",
                "--skip-git-repo-check",
                "--sandbox",
                "read-only",
                *model,
                "-o",
                out,
                "-",
            ]
        return ["gemini", *model, "--output-format", "json"]

    async def complete(
        self, system: str, messages: list[dict[str, str]], max_tokens: int | None = None
    ) -> Reply:
        prompt = "\n\n".join(
            [system, *(f"{m['role'].upper()}: {m['content']}" for m in messages), "ASSISTANT:"]
        )
        start = time.monotonic()
        with tempfile.TemporaryDirectory(prefix="opstend-zorec-") as tmp:
            out = os.path.join(tmp, "reply.txt")
            try:
                proc = await asyncio.create_subprocess_exec(
                    *self._argv(out),
                    stdin=asyncio.subprocess.PIPE,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    cwd=tmp,
                )
            except FileNotFoundError as exc:
                raise ProviderError(f"{self.config.command} is not installed on this node") from exc
            try:
                stdout, stderr = await asyncio.wait_for(
                    proc.communicate(prompt.encode()), self.config.timeout_s
                )
            except TimeoutError as exc:
                proc.kill()
                raise ProviderError("the model did not answer in time") from exc
            if proc.returncode != 0:
                tail = stderr.decode(errors="replace").strip().splitlines()[-1:] or ["failed"]
                raise ProviderError(f"{self.config.command}: {tail[0][:200]}")
            if self.config.command == "codex":
                text = Path(out).read_text(encoding="utf-8") if os.path.exists(out) else ""
            else:
                try:
                    text = json.loads(stdout.decode()).get("response", "")
                except ValueError:
                    text = stdout.decode(errors="replace")
        return Reply(
            text.strip(), self.model or self.config.command or "", seconds=time.monotonic() - start
        )


def build(
    cfg: ProviderConfig,
    api_key: str | None = None,
    transport: httpx.AsyncBaseTransport | None = None,
) -> Provider:
    if cfg.kind == Kind.OPENAI:
        return OpenAICompatible(cfg, api_key, transport)
    if cfg.kind == Kind.OLLAMA:
        return Ollama(cfg, api_key, transport)
    if cfg.kind == Kind.ANTHROPIC:
        return Anthropic(cfg, api_key)
    return Cli(cfg)
