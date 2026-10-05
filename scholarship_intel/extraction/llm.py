"""Free-tier LLM clients (Groq, Gemini, Ollama) behind one interface with automatic model discovery,
pacing, 429 back-off and provider fall-through. Plain `requests` – no SDKs needed.

The LLM is *only* used to propose {value, quote} pairs. It never produces a confidence number.
"""
from __future__ import annotations

import json
import os
import re
import time
from dataclasses import dataclass

import requests

from .. import config


class LLMError(Exception):
    pass


class LLMRateLimited(LLMError):
    pass


def parse_json_loose(s: str) -> dict:
    s = re.sub(r"<think>.*?</think>", "", s or "", flags=re.S).strip()
    s = re.sub(r"^```(?:json)?\s*|\s*```$", "", s.strip())
    try:
        return json.loads(s)
    except ValueError:
        a, b = s.find("{"), s.rfind("}")
        if a >= 0 and b > a:
            try:
                return json.loads(s[a:b + 1])
            except ValueError:
                pass
        # last resort: trim a truncated tail back to the last complete top-level field
        if a >= 0:
            frag = s[a:]
            for cut in range(len(frag), max(len(frag) - 4000, 0), -1):
                if frag[cut - 1] == "}":
                    try:
                        return json.loads(frag[:cut] + "}")
                    except ValueError:
                        continue
    raise LLMError("model did not return parseable JSON")


@dataclass
class Provider:
    name: str
    model: str

    def available(self) -> bool:  # pragma: no cover - overridden
        return False

    def generate_json(self, system: str, user: str, max_tokens: int = 3000) -> dict:  # pragma: no cover
        raise NotImplementedError

    @property
    def label(self) -> str:
        return f"{self.name}:{self.model}"


def _pick(preferred: str | None, available: list[str], ranking: list[str]) -> str | None:
    if preferred and preferred in available:
        return preferred
    for r in ranking:
        if r in available:
            return r
    return None


class GroqProvider(Provider):
    URL = "https://api.groq.com/openai/v1"

    def __init__(self):
        self.key = os.getenv("GROQ_API_KEY", "").strip()
        model = os.getenv("GROQ_MODEL", "").strip() or "llama-3.3-70b-versatile"
        super().__init__("groq", model)
        self._checked = False

    def available(self) -> bool:
        if not self.key:
            return False
        if not self._checked:
            self._checked = True
            try:
                r = requests.get(f"{self.URL}/models", headers={"Authorization": f"Bearer {self.key}"}, timeout=15)
                if r.status_code == 200:
                    ids = [m["id"] for m in r.json().get("data", [])]
                    pick = _pick(os.getenv("GROQ_MODEL") or None, ids,
                                 ["llama-3.3-70b-versatile", "openai/gpt-oss-120b", "llama-3.1-70b-versatile", "llama-3.1-8b-instant"])
                    if pick:
                        self.model = pick
                elif r.status_code in (401, 403):
                    self.key = ""
                    return False
            except requests.RequestException:
                pass
        return bool(self.key)

    def generate_json(self, system: str, user: str, max_tokens: int = 3000) -> dict:
        body = {"model": self.model, "temperature": 0, "max_tokens": max_tokens,
                "response_format": {"type": "json_object"},
                "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}]}
        r = requests.post(f"{self.URL}/chat/completions", headers={"Authorization": f"Bearer {self.key}"}, json=body, timeout=120)
        if r.status_code == 429:
            raise LLMRateLimited(f"groq 429 retry-after={r.headers.get('retry-after')}")
        if r.status_code >= 400:
            raise LLMError(f"groq HTTP {r.status_code}: {r.text[:200]}")
        return parse_json_loose(r.json()["choices"][0]["message"]["content"])


class GeminiProvider(Provider):
    URL = "https://generativelanguage.googleapis.com/v1beta"

    def __init__(self):
        self.key = (os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY") or "").strip()
        model = os.getenv("GEMINI_MODEL", "").strip() or "gemini-3.1-flash-lite"
        super().__init__("gemini", model)
        self._checked = False

    def available(self) -> bool:
        if not self.key:
            return False
        if not self._checked:
            self._checked = True
            try:
                r = requests.get(f"{self.URL}/models", params={"key": self.key, "pageSize": 100}, timeout=15)
                if r.status_code == 200:
                    ids = [m["name"].split("/", 1)[1] for m in r.json().get("models", [])
                           if "generateContent" in m.get("supportedGenerationMethods", [])]
                    pick = _pick(os.getenv("GEMINI_MODEL") or None, ids,
                                 ["gemini-3.1-flash-lite", "gemini-flash-lite-latest", "gemini-flash-latest", "gemini-2.5-flash"])
                    if pick:
                        self.model = pick
                elif r.status_code in (400, 401, 403):
                    self.key = ""
                    return False
            except requests.RequestException:
                pass
        return bool(self.key)

    def generate_json(self, system: str, user: str, max_tokens: int = 3000) -> dict:
        gen = {"temperature": 0, "responseMimeType": "application/json", "maxOutputTokens": max(max_tokens, 4096)}
        if "flash" in self.model and "pro" not in self.model:
            gen["thinkingConfig"] = {"thinkingBudget": 0}
        body = {"systemInstruction": {"parts": [{"text": system}]},
                "contents": [{"role": "user", "parts": [{"text": user}]}], "generationConfig": gen}
        url = f"{self.URL}/models/{self.model}:generateContent"
        r = requests.post(url, params={"key": self.key}, json=body, timeout=180)
        if r.status_code == 400 and "thinking" in r.text.lower():
            gen.pop("thinkingConfig", None)
            r = requests.post(url, params={"key": self.key}, json=body, timeout=180)
        if r.status_code == 429:
            raise LLMRateLimited("gemini 429")
        if r.status_code >= 400:
            raise LLMError(f"gemini HTTP {r.status_code}: {r.text[:200]}")
        data = r.json()
        try:
            parts = data["candidates"][0]["content"]["parts"]
            return parse_json_loose("".join(p.get("text", "") for p in parts))
        except (KeyError, IndexError):
            raise LLMError(f"gemini empty response: {str(data)[:200]}")


class OllamaProvider(Provider):
    def __init__(self):
        self.host = os.getenv("OLLAMA_HOST", "http://localhost:11434").rstrip("/")
        super().__init__("ollama", os.getenv("OLLAMA_MODEL", "").strip())
        self._checked = False
        self._ok = False

    def available(self) -> bool:
        if not self._checked:
            self._checked = True
            try:
                r = requests.get(f"{self.host}/api/tags", timeout=3)
                names = [m["name"] for m in r.json().get("models", [])] if r.status_code == 200 else []
                if names:
                    self.model = self.model if self.model in names else names[0]
                    self._ok = True
            except (requests.RequestException, ValueError):
                self._ok = False
        return self._ok

    def generate_json(self, system: str, user: str, max_tokens: int = 3000) -> dict:
        body = {"model": self.model, "stream": False, "format": "json", "think": False,
                "options": {"temperature": 0, "num_ctx": 12288, "num_predict": max_tokens},
                "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}]}
        try:
            r = requests.post(f"{self.host}/api/chat", json=body, timeout=900)
        except requests.RequestException as exc:
            raise LLMError(f"ollama: {exc}")
        if r.status_code >= 400:
            raise LLMError(f"ollama HTTP {r.status_code}: {r.text[:200]}")
        return parse_json_loose(r.json().get("message", {}).get("content", ""))


class LLMRouter:
    """Ordered provider chain. `providers_for_extraction(n)` returns up to n *distinct* available providers so the
    verification engine can compare independent model opinions."""

    def __init__(self, chain: list[str] | None = None, enabled: bool = True):
        cfg = config.settings()["llm"]
        self.min_gap = float(cfg["min_seconds_between_calls"])
        self.max_chars = {"groq": int(cfg["max_chars_groq"]), "gemini": int(cfg["max_chars_gemini"]), "ollama": int(cfg["max_chars_ollama"])}
        registry = {"groq": GroqProvider, "gemini": GeminiProvider, "ollama": OllamaProvider}
        self.enabled = enabled
        self.providers: list[Provider] = []
        if enabled:
            for name in (chain or cfg["chain"]):
                if name in registry:
                    p = registry[name]()
                    if p.available():
                        self.providers.append(p)
        self._last = 0.0
        self._cooldown_until: dict[str, float] = {}
        self.calls = 0
        self.failures: list[str] = []

    def describe(self) -> str:
        return ", ".join(p.label for p in self.providers) or "none (rule-based extraction only)"

    def providers_for_extraction(self, n: int = 2) -> list[Provider]:
        ready = [p for p in self.providers if time.time() >= self._cooldown_until.get(p.name, 0)]
        # The local model is a fallback when no cloud extractor is available;
        # a free-tier cooldown should not silently turn every page into a
        # potentially minutes-long local inference call.
        cloud = [p for p in ready if p.name != "ollama"]
        return (cloud or ready)[:n]

    def seconds_until_ready(self, max_wait: float = 75.0) -> float | None:
        """Shortest wait (<= max_wait) until a cooled-down cloud provider is usable again, else None (nothing worth waiting for)."""
        now = time.time()
        waits = [self._cooldown_until[p.name] - now for p in self.providers
                 if p.name != "ollama" and self._cooldown_until.get(p.name, 0) > now]
        waits = [w for w in waits if w <= max_wait]
        return min(waits) if waits else None

    def call(self, provider: Provider, system: str, user: str, max_tokens: int = 3000) -> dict:
        wait = self._last + self.min_gap - time.time()
        if wait > 0 and provider.name != "ollama":
            time.sleep(wait)
        last_exc: Exception | None = None
        for attempt in range(3):
            try:
                self.calls += 1
                out = provider.generate_json(system, user, max_tokens)
                self._last = time.time()
                return out
            except LLMRateLimited as exc:
                last_exc = exc
                m = re.search(r"retry-after=(\d+(?:\.\d+)?)", str(exc))
                delay = float(m.group(1)) if m else 60.0
                if delay > 15 or attempt == 2:
                    self._cooldown_until[provider.name] = time.time() + delay
                    break
                time.sleep(delay + 1)
            except LLMError as exc:
                last_exc = exc
                if re.search(r"HTTP (502|503|504)", str(exc)):
                    self._cooldown_until[provider.name] = time.time() + 60
                    break
                if "parseable JSON" not in str(exc):
                    break
                user = user + "\n\nReminder: respond with ONE valid JSON object only."
        self.failures.append(f"{provider.label}: {last_exc}")
        raise LLMError(str(last_exc))
