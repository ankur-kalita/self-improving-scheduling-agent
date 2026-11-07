"""Thin OpenAI-compatible client with rate limiting, backoff and a disk cache.

One adapter covers Gemini (OpenAI-compatible endpoint), OpenAI, Groq,
OpenRouter and Ollama by switching LLM_BASE_URL. The cache key includes the
full request, so a re-run with an unchanged policy replays identical calls for
free (useful on free-tier quotas), while any policy change misses the cache.
"""

from __future__ import annotations

import hashlib
import json
import os
import random
import re
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[2]
load_dotenv(ROOT / ".env")

GEMINI_BASE = "https://generativelanguage.googleapis.com/v1beta/openai/"


def _env(name: str, default: str | None = None) -> str | None:
    v = os.getenv(name)
    return v if v not in (None, "") else default


@dataclass
class LLMConfig:
    base_url: str | None
    api_key: str
    agent_model: str
    sim_model: str
    judge_model: str
    improver_model: str
    rpm: int
    cache: bool

    @classmethod
    def from_env(cls) -> "LLMConfig":
        key = _env("LLM_API_KEY") or _env("GEMINI_API_KEY") or _env("OPENAI_API_KEY")
        if not key:
            raise SystemExit("No API key. Copy .env.example to .env and set GEMINI_API_KEY (or LLM_API_KEY).")
        if _env("LLM_BASE_URL"):
            base = _env("LLM_BASE_URL")
        elif _env("LLM_API_KEY") or _env("GEMINI_API_KEY"):
            base = GEMINI_BASE
        else:
            base = None  # OpenAI default
        default_agent = "gemini-3.5-flash" if base == GEMINI_BASE else "gpt-4.1-mini"
        default_sim = "gemini-3.5-flash-lite" if base == GEMINI_BASE else "gpt-4.1-mini"
        return cls(
            base_url=base,
            api_key=key,
            agent_model=_env("AGENT_MODEL", default_agent),
            sim_model=_env("SIM_MODEL", default_sim),
            judge_model=_env("JUDGE_MODEL", default_agent),
            improver_model=_env("IMPROVER_MODEL", default_agent),
            rpm=int(_env("LLM_RPM", "10")),
            cache=_env("LLM_CACHE", "1") == "1",
        )


class _RateLimiter:
    def __init__(self, rpm: int):
        self.interval = 60.0 / max(rpm, 1)
        self.lock = threading.Lock()
        self.next_at = 0.0

    def wait(self) -> None:
        with self.lock:
            now = time.monotonic()
            at = max(now, self.next_at)
            self.next_at = at + self.interval
        if at > now:
            time.sleep(at - now)


class LLM:
    def __init__(self, cfg: LLMConfig | None = None):
        from openai import OpenAI

        self.cfg = cfg or LLMConfig.from_env()
        self.client = OpenAI(api_key=self.cfg.api_key, base_url=self.cfg.base_url, max_retries=0, timeout=90)
        self._limiters: dict[str, _RateLimiter] = {}
        self._lock = threading.Lock()
        self.cache_dir = ROOT / ".cache" / "llm"
        self.calls = 0
        self.cache_hits = 0

    def _limiter(self, model: str) -> _RateLimiter:
        with self._lock:
            if model not in self._limiters:
                self._limiters[model] = _RateLimiter(self.cfg.rpm)
            return self._limiters[model]

    def chat(self, model: str, messages: list[dict], tools: list[dict] | None = None,
             temperature: float = 0.0, json_mode: bool = False, cache_salt: str = "") -> dict:
        """Return {"content": str|None, "tool_calls": [{"id","name","arguments"}]}."""
        req = {"model": model, "messages": messages, "temperature": temperature}
        if tools:
            req["tools"] = tools
        if json_mode:
            req["response_format"] = {"type": "json_object"}

        key = hashlib.sha256((json.dumps(req, sort_keys=True, default=str) + cache_salt).encode()).hexdigest()
        path = self.cache_dir / key[:2] / f"{key}.json"
        if self.cfg.cache and path.exists():
            self.cache_hits += 1
            return json.loads(path.read_text())

        out = self._call_with_backoff(req)
        if self.cfg.cache:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(out))
        return out

    def _call_with_backoff(self, req: dict) -> dict:
        from openai import APIConnectionError, APIStatusError, APITimeoutError, RateLimitError

        delay = 4.0
        for attempt in range(8):
            self._limiter(req["model"]).wait()
            try:
                resp = self.client.chat.completions.create(**req)
                self.calls += 1
                msg = resp.choices[0].message
                calls = []
                for i, tc in enumerate(msg.tool_calls or []):
                    call = {"id": tc.id or f"call_{i}", "name": tc.function.name,
                            "arguments": tc.function.arguments or "{}"}
                    # Gemini 3 attaches a thought signature that must be echoed back on the next request.
                    extra = (tc.model_extra or {}).get("extra_content")
                    if extra:
                        call["extra_content"] = extra
                    calls.append(call)
                return {"content": _strip_thoughts(msg.content), "tool_calls": calls}
            except (RateLimitError, APITimeoutError, APIConnectionError) as e:
                err = e
            except APIStatusError as e:
                if e.status_code < 500:
                    raise
                err = e
            if attempt == 7:
                raise err
            time.sleep(delay + random.random())
            delay = min(delay * 2, 65)
        raise RuntimeError("unreachable")


def _strip_thoughts(text: str | None) -> str | None:
    if not text:
        return text
    return re.sub(r"<thought>.*?</thought>", "", text, flags=re.S).strip() or None


def parse_json(text: str | None) -> dict:
    """Lenient JSON extraction (models sometimes wrap JSON in code fences)."""
    if not text:
        raise ValueError("empty response")
    t = text.strip()
    if t.startswith("```"):
        t = t.split("\n", 1)[1] if "\n" in t else t
        t = t.rsplit("```", 1)[0]
    start, end = t.find("{"), t.rfind("}")
    if start == -1 or end == -1:
        raise ValueError(f"no JSON object in: {text[:200]}")
    return json.loads(t[start : end + 1])
