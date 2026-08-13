"""Pluggable LLM client abstraction.

Supports Ollama (local), OpenAI, Anthropic, and the local `claude` CLI as
providers. Each service that needs LLM calls uses this interface so the
provider can be swapped via config without changing service code.

The claude_code provider bills against a Pro/Max subscription instead of a
metered API key; see ClaudeCodeClient for when that is and is not a good fit.
"""

from __future__ import annotations

import asyncio
import json
import logging
import tempfile
from abc import ABC, abstractmethod

import httpx

from .config import LLMConfig
from .models import LLMProvider

logger = logging.getLogger(__name__)


class LLMClient(ABC):
    """Abstract LLM client — all providers implement this interface."""

    @abstractmethod
    async def complete(self, prompt: str, *, max_tokens: int = 2000) -> str:
        """Send a prompt and return the text response."""
        ...

    async def complete_json(self, prompt: str, *, max_tokens: int = 2000) -> dict | list | None:
        """Send a prompt and parse the response as JSON.

        Returns None if the response isn't valid JSON.
        """
        text = await self.complete(prompt, max_tokens=max_tokens)
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            pass

        # Try to extract JSON from markdown code blocks
        if "```" in text:
            for block in text.split("```"):
                block = block.strip()
                if block.startswith("json"):
                    block = block[4:].strip()
                try:
                    return json.loads(block)
                except json.JSONDecodeError:
                    continue

        # Unfenced prose around the payload ("Here is the JSON: [...]"). Anchor
        # on whichever bracket opens first so that an object containing arrays
        # is not mistaken for the inner array.
        openers = [(text.find(o), o, c) for o, c in (("{", "}"), ("[", "]")) if text.find(o) != -1]
        if openers:
            start, _, closer = min(openers)
            end = text.rfind(closer)
            if end > start:
                try:
                    return json.loads(text[start : end + 1])
                except json.JSONDecodeError:
                    pass

        logger.warning("LLM response was not valid JSON: %s...", text[:200])
        return None

    @abstractmethod
    async def close(self) -> None:
        """Clean up resources."""
        ...


class OllamaClient(LLMClient):
    """LLM client for local Ollama instance."""

    def __init__(self, base_url: str, model: str, timeout: float = 60.0) -> None:
        self.base_url = base_url.rstrip("/")
        self.model = model
        self._client = httpx.AsyncClient(timeout=timeout)

    async def complete(self, prompt: str, *, max_tokens: int = 2000) -> str:
        response = await self._client.post(
            f"{self.base_url}/api/generate",
            json={
                "model": self.model,
                "prompt": prompt,
                "stream": False,
                "format": "json",
                "options": {"num_predict": max_tokens},
            },
        )
        response.raise_for_status()
        return response.json().get("response", "")

    async def close(self) -> None:
        await self._client.aclose()


class OpenAIClient(LLMClient):
    """LLM client for OpenAI-compatible APIs."""

    def __init__(
        self,
        api_key: str,
        model: str = "gpt-4o-mini",
        base_url: str = "https://api.openai.com/v1",
        timeout: float = 60.0,
    ) -> None:
        self.model = model
        self._client = httpx.AsyncClient(
            base_url=base_url,
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=timeout,
        )

    async def complete(self, prompt: str, *, max_tokens: int = 2000) -> str:
        response = await self._client.post(
            "/chat/completions",
            json={
                "model": self.model,
                "messages": [{"role": "user", "content": prompt}],
                "max_tokens": max_tokens,
                "response_format": {"type": "json_object"},
            },
        )
        response.raise_for_status()
        return response.json()["choices"][0]["message"]["content"]

    async def close(self) -> None:
        await self._client.aclose()


class AnthropicClient(LLMClient):
    """LLM client for Anthropic Claude API."""

    def __init__(
        self,
        api_key: str,
        model: str = "claude-sonnet-5",
        timeout: float = 60.0,
    ) -> None:
        self.model = model
        self._client = httpx.AsyncClient(
            base_url="https://api.anthropic.com/v1",
            headers={
                "x-api-key": api_key,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            timeout=timeout,
        )

    async def complete(self, prompt: str, *, max_tokens: int = 2000) -> str:
        # No response_format flag here, and no assistant prefill either: the
        # two callers disagree on shape (relevance wants an object, extraction
        # wants an array), so there is no single opening token to prefill.
        # complete_json() below tolerates fences and surrounding prose.
        response = await self._client.post(
            "/messages",
            json={
                "model": self.model,
                "max_tokens": max_tokens,
                "messages": [{"role": "user", "content": prompt}],
            },
        )
        response.raise_for_status()
        content = response.json()["content"]
        return content[0]["text"] if content else ""

    async def close(self) -> None:
        await self._client.aclose()


class ClaudeCodeClient(LLMClient):
    """Runs prompts through the local `claude` CLI in non-interactive mode.

    Unlike AnthropicClient this needs no API key: the CLI carries its own
    Pro/Max subscription OAuth login, so calls draw against the subscription's
    rate limits rather than per-token billing. `total_cost_usd` in the CLI's
    output still reports an API-equivalent figure, but no money moves per call.

    The trade-off is process-per-call. Each invocation is a cold Node start
    (~3.5s) plus a fixed system-prompt overhead, so this suits incremental
    refreshes — a few dozen changed pages — far better than a full backfill.
    The flags below strip the coding harness (tools, MCP servers, project
    context) that a data-extraction prompt has no use for; leaving them on
    costs about 25k input tokens per call instead of about 6.5k.
    """

    # The CLI's own tools are dead weight here and their schemas are most of
    # the fixed overhead, so every built-in is switched off.
    _DISALLOWED_TOOLS = (
        "Bash", "Read", "Write", "Edit", "Glob", "Grep", "WebFetch", "WebSearch",
        "Task", "TodoWrite", "NotebookEdit", "BashOutput", "KillShell",
        "SlashCommand", "ExitPlanMode",
    )

    def __init__(
        self,
        model: str = "claude-sonnet-5",
        bin_path: str = "claude",
        timeout: float = 180.0,
    ) -> None:
        self.model = model
        self.bin_path = bin_path
        self.timeout = timeout

    async def complete(self, prompt: str, *, max_tokens: int = 2000) -> str:
        """Send a prompt via stdin and return the assistant's text.

        `max_tokens` is accepted for interface compatibility and ignored — the
        CLI exposes no equivalent flag.
        """
        args = [
            self.bin_path,
            "-p",
            "--output-format", "json",
            "--model", self.model,
            # One turn: this is a single completion, not an agent loop.
            "--max-turns", "1",
            "--system-prompt",
            "You are a data extraction engine. Reply with JSON only — no prose, "
            "no explanation, no markdown fences.",
            "--exclude-dynamic-system-prompt-sections",
            "--strict-mcp-config",
            "--mcp-config", '{"mcpServers":{}}',
            "--disallowed-tools", *self._DISALLOWED_TOOLS,
        ]

        process = await asyncio.create_subprocess_exec(
            *args,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            # Run from a neutral directory. Started inside this repo the CLI
            # loads its CLAUDE.md as project context — 4KB of instructions about
            # crawler architecture, prepended to every extraction call, which is
            # both wasted tokens and a prompt the model may try to act on.
            cwd=tempfile.gettempdir(),
        )
        # Prompts carry whole pages of HTML, well past the 1MB ARG_MAX ceiling
        # an argv-passed prompt would hit, so the prompt goes over stdin.
        try:
            stdout, stderr = await asyncio.wait_for(
                process.communicate(prompt.encode("utf-8")), timeout=self.timeout
            )
        except asyncio.TimeoutError:
            process.kill()
            await process.wait()
            raise RuntimeError(f"claude CLI timed out after {self.timeout}s")

        if process.returncode != 0:
            raise RuntimeError(
                f"claude CLI exited {process.returncode}: {stderr.decode('utf-8', 'replace')[:300]}"
            )

        try:
            payload = json.loads(stdout.decode("utf-8"))
        except json.JSONDecodeError:
            logger.warning("claude CLI did not return JSON: %s...", stdout[:200])
            return ""

        if payload.get("is_error"):
            raise RuntimeError(f"claude CLI reported an error: {str(payload.get('result'))[:300]}")

        return payload.get("result") or ""

    async def close(self) -> None:
        """Nothing to release — each call is its own short-lived process."""


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------


def create_llm_client(config: LLMConfig) -> LLMClient:
    """Create an LLM client based on config.

    This is the entry point — services call this and get back whichever
    provider is configured, without knowing the implementation details.
    """
    match config.llm_provider:
        case LLMProvider.OLLAMA:
            return OllamaClient(
                base_url=config.llm_base_url,
                model=config.llm_model,
                timeout=config.llm_timeout,
            )
        case LLMProvider.OPENAI:
            if not config.llm_api_key:
                raise ValueError("LLM_API_KEY required for OpenAI provider")
            return OpenAIClient(
                api_key=config.llm_api_key,
                model=config.llm_model,
                base_url=config.llm_base_url
                if "openai" not in config.llm_base_url
                else "https://api.openai.com/v1",
                timeout=config.llm_timeout,
            )
        case LLMProvider.ANTHROPIC:
            if not config.llm_api_key:
                raise ValueError("LLM_API_KEY required for Anthropic provider")
            return AnthropicClient(
                api_key=config.llm_api_key,
                model=config.llm_model,
                timeout=config.llm_timeout,
            )
        case LLMProvider.CLAUDE_CODE:
            return ClaudeCodeClient(
                model=config.llm_model,
                bin_path=config.claude_bin,
                timeout=config.llm_timeout,
            )
        case _:
            raise ValueError(f"Unknown LLM provider: {config.llm_provider}")
