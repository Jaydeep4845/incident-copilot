"""Minimal OpenAI-compatible chat client (Groq by default).

Tool calling, retries and token counts.
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass

import httpx

PROVIDERS = {
    "groq": ("https://api.groq.com/openai/v1/chat/completions", "GROQ_API_KEY"),
    "cerebras": ("https://api.cerebras.ai/v1/chat/completions", "CEREBRAS_API_KEY"),
}


class QuotaExhausted(RuntimeError):
    """The model's free daily allowance is used up; try another model or tomorrow."""


@dataclass
class Usage:
    prompt_tokens: int = 0
    completion_tokens: int = 0
    calls: int = 0

    def add(self, u: dict) -> None:
        self.prompt_tokens += u.get("prompt_tokens", 0)
        self.completion_tokens += u.get("completion_tokens", 0)
        self.calls += 1

    @property
    def total(self) -> int:
        return self.prompt_tokens + self.completion_tokens


class ChatClient:
    def __init__(self, model: str, provider: str = "groq", temperature: float = 0.2):
        url, key_env = PROVIDERS[provider]
        self.url, self.key = url, os.environ[key_env]
        self.model, self.temperature = model, temperature
        self.usage = Usage()
        self.http = httpx.Client(timeout=120)

    def chat(self, messages: list[dict], tools: list[dict] | None = None, tool_choice=None) -> dict:
        body = {"model": self.model, "messages": messages, "temperature": self.temperature}
        if tools:
            body["tools"] = [{"type": "function", "function": t} for t in tools]
            body["tool_choice"] = tool_choice or "auto"
        if self.model.startswith("openai/gpt-oss"):
            body["reasoning_effort"] = "medium"
        for attempt in range(6):
            try:
                r = self.http.post(
                    self.url, json=body, headers={"Authorization": f"Bearer {self.key}"}
                )
            except httpx.TransportError:
                time.sleep(5 * (attempt + 1))
                continue
            if r.status_code == 429:
                if "per day" in r.text:
                    raise QuotaExhausted(f"{self.model}: {r.text[:200]}")
                time.sleep(min(float(r.headers.get("retry-after", 10)), 60))
                continue
            if r.status_code >= 500:
                time.sleep(5 * (attempt + 1))
                continue
            if r.status_code == 400 and "tool" in r.text and attempt < 2:
                # occasional malformed tool call from the model: retry once
                time.sleep(1)
                continue
            if r.status_code >= 400:
                raise RuntimeError(f"{self.model}: HTTP {r.status_code}: {r.text[:300]}")
            data = r.json()
            self.usage.add(data.get("usage", {}))
            return data["choices"][0]["message"]
        raise RuntimeError(
            f"{self.model}: request failed after retries ({r.status_code} {r.text[:200]})"
        )
