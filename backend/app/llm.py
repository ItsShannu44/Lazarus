"""LLM access with structured JSON output.

Primary: Gemini (REST, responseSchema). Fallback: any OpenAI-compatible API
(Groq / OpenRouter) when Gemini is rate-limited, overloaded or down.

Model names change often, so if the configured model is missing (404) Lazarus
asks the provider which models exist and picks the best one automatically.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from typing import Any

import httpx

log = logging.getLogger("lazarus.llm")


class LLMError(Exception):
    pass


class _Retryable(Exception):
    pass


class _ModelMissing(Exception):
    pass


class _RateLimited(Exception):
    def __init__(self, wait: float, detail: str = ""):
        super().__init__(f"rate limited, retry in {wait:.0f}s {detail[:120]}")
        self.wait = wait


def retry_after(resp: httpx.Response, default: float = 20.0) -> float:
    """Seconds the provider asks us to wait (Retry-After header, Groq/OpenAI text, Gemini retryDelay)."""
    h = resp.headers.get("retry-after")
    if h:
        try:
            return min(90.0, float(h))
        except ValueError:
            pass
    text = resp.text[:2000]
    m = re.search(r"try again in (?:(\d+)m)?\s*([\d.]+)\s*(ms|s)\b", text)
    if m:
        sec = float(m.group(2)) / (1000 if m.group(3) == "ms" else 1) + 60 * int(m.group(1) or 0)
        return min(90.0, sec + 0.5)
    m = re.search(r'"retryDelay"\s*:\s*"([\d.]+)s"', text)
    if m:
        return min(90.0, float(m.group(1)) + 0.5)
    return default


def _err(e: Exception) -> str:
    msg = str(e).strip()
    return f"{type(e).__name__}: {msg}" if msg else type(e).__name__


def to_gemini_schema(schema: dict) -> dict:
    """JSON-Schema subset -> Gemini OpenAPI schema (uppercase types, no extras)."""
    out: dict[str, Any] = {}
    for key, val in schema.items():
        if key == "type":
            out["type"] = str(val).upper()
        elif key == "properties":
            out["properties"] = {k: to_gemini_schema(v) for k, v in val.items()}
        elif key == "items":
            out["items"] = to_gemini_schema(val)
        elif key in ("required", "enum", "description", "nullable", "minimum", "maximum"):
            out[key] = val
    return out


def parse_json(text: str) -> dict:
    text = text.strip()
    fence = re.match(r"^```(?:json)?\s*(.*?)\s*```$", text, re.S)
    if fence:
        text = fence.group(1)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        start, end = text.find("{"), text.rfind("}")
        if start != -1 and end > start:
            return json.loads(text[start : end + 1])
        raise


# ----------------------------------------------------------------- model discovery
def _version(name: str) -> float:
    m = re.search(r"gemini-(\d+(?:\.\d+)?)", name)
    return float(m.group(1)) if m else 0.0


def pick_gemini_model(models: list[dict]) -> str | None:
    """Newest stable 'flash' model that supports generateContent."""
    bad = ("image", "tts", "audio", "live", "embedding", "vision", "thinking-exp", "robotics")
    cands = []
    for m in models:
        name = m.get("name", "").removeprefix("models/")
        if "generateContent" not in m.get("supportedGenerationMethods", ["generateContent"]):
            continue
        if not name.startswith("gemini-") or "flash" not in name or any(b in name for b in bad):
            continue
        score = (
            "preview" not in name and "exp" not in name,  # stable first
            "lite" not in name,                           # full flash over lite
            _version(name),
            "latest" in name,
        )
        cands.append((score, name))
    return max(cands)[1] if cands else None


FALLBACK_PREFERENCE = [
    "gpt-oss-120b", "llama-4-maverick", "kimi-k2", "qwen3", "llama-3.3-70b",
    "deepseek", "llama-4-scout", "gpt-oss-20b", "llama",
]
FALLBACK_SKIP = ("whisper", "tts", "guard", "orpheus", "playai", "embed", "vision", "compound", "audio")


def pick_fallback_model(ids: list[str]) -> str | None:
    usable = [i for i in ids if not any(s in i.lower() for s in FALLBACK_SKIP)]
    for pref in FALLBACK_PREFERENCE:
        for i in usable:
            if pref in i.lower():
                return i
    return usable[0] if usable else None


class LLM:
    def __init__(self, settings):
        self.s = settings
        self.gemini_model = settings.gemini_model
        self.fallback_model = settings.fallback_model
        self._http = httpx.AsyncClient(timeout=httpx.Timeout(150.0, connect=15.0))
        self._lock = asyncio.Lock()
        self._pool: list[str] | None = None        # fallback models to rotate through
        self._limited_until: dict[str, float] = {}  # model -> time its rate limit resets
        self.patience_s = float(getattr(settings, "llm_patience_s", 150))
        self.gemini_cooldown_s = float(getattr(settings, "gemini_cooldown_s", 300))
        self._gemini_off_until = 0.0  # circuit breaker: skip a failing Gemini instead of paying for it every call

    async def close(self) -> None:
        await self._http.aclose()

    # ------------------------------------------------------------- public
    async def json(self, system: str, prompt: str, schema: dict, agent: str = "") -> dict:
        """Structured answer from the first provider that can give one.

        Rate limits are normal on free tiers: we wait exactly as long as the provider asks,
        rotate to another fallback model (each has its own limit), and only give up after
        `patience_s` seconds.
        """
        errors: list[str] = []
        rate_limited = 0
        deadline = time.monotonic() + self.patience_s
        has_fallback = bool(self.s.fallback_api_key)

        use_gemini = bool(self.s.gemini_api_key) and (not has_fallback or time.monotonic() >= self._gemini_off_until)
        if use_gemini:
            # with a fallback available, give Gemini one quick chance; without one, keep retrying
            for attempt in range(4 if not has_fallback else 1):
                try:
                    t0 = time.monotonic()
                    out = await self._gemini(system, prompt, schema)
                    log.info("[%s] Gemini %s answered in %.1fs", agent, self.gemini_model, time.monotonic() - t0)
                    return out
                except _ModelMissing as e:
                    errors.append(f"gemini: {e}")
                    if not await self._rediscover_gemini():
                        break
                except _RateLimited as e:
                    rate_limited += 1
                    errors.append(f"gemini: {e}")
                    if has_fallback:
                        log.warning("[%s] Gemini rate-limited; using the fallback provider", agent)
                        break
                    await self._sleep(e.wait, deadline, agent, "Gemini")
                except _Retryable as e:
                    errors.append(f"gemini: {e}")
                    log.warning("[%s] Gemini busy (%s)", agent, e)
                    if has_fallback:
                        break  # don't wait on Gemini when Groq can answer now
                    await self._sleep(3 * (attempt + 1), deadline, agent, "Gemini")
                except Exception as e:
                    errors.append(f"gemini: {_err(e)}")
                    log.warning("[%s] Gemini failed: %s", agent, _err(e))
                    break
            if has_fallback:
                # Gemini didn't answer: stop asking it for a while so every call isn't slowed down
                self._gemini_off_until = time.monotonic() + self.gemini_cooldown_s
                log.warning("[%s] skipping Gemini for %.0f min, using %s", agent, self.gemini_cooldown_s / 60,
                            self.fallback_model)

        if has_fallback:
            tries = 0
            while tries < 10 and time.monotonic() < deadline:
                tries += 1
                try:
                    t0 = time.monotonic()
                    out = await self._openai_compat(system, prompt, schema)
                    log.info("[%s] %s answered in %.1fs", agent, self.fallback_model, time.monotonic() - t0)
                    return out
                except _RateLimited as e:
                    rate_limited += 1
                    errors.append(f"fallback {self.fallback_model}: {e}")
                    self._limited_until[self.fallback_model] = time.monotonic() + e.wait
                    alt = await self._next_fallback_model()
                    if alt:
                        log.warning("[%s] %s rate-limited; switching to %s", agent, self.fallback_model, alt)
                        self.fallback_model = alt
                        continue
                    await self._sleep(e.wait, deadline, agent, self.fallback_model)
                except _ModelMissing as e:
                    errors.append(f"fallback: {e}")
                    if not await self._rediscover_fallback():
                        break
                except Exception as e:
                    errors.append(f"fallback: {_err(e)}")
                    log.warning("[%s] fallback failed: %s", agent, _err(e))
                    await self._sleep(2 * tries, deadline, agent, "fallback")

        if not errors:
            raise LLMError("No LLM configured: set GEMINI_API_KEY and/or FALLBACK_API_KEY")
        if rate_limited and rate_limited >= len(errors) - 1:
            raise LLMError(
                "Every LLM model hit its free-tier rate limit. Wait a minute and press Fix latest failure, "
                "or add a second provider (GEMINI_API_KEY) so Lazarus can switch."
            )
        raise LLMError(" | ".join(e[:250] for e in errors[-3:]))

    async def _sleep(self, wait: float, deadline: float, agent: str, who: str) -> None:
        wait = max(0.5, min(wait, deadline - time.monotonic()))
        log.warning("[%s] waiting %.0fs for %s", agent, wait, who)
        await asyncio.sleep(wait)

    async def _next_fallback_model(self) -> str | None:
        """Another fallback model whose rate limit is not currently hit."""
        if self._pool is None:
            try:
                ids = await self.list_fallback_models()
                usable = [i for i in ids if not any(s in i.lower() for s in FALLBACK_SKIP)]
                ranked = []
                for pref in FALLBACK_PREFERENCE:
                    ranked += [i for i in usable if pref in i.lower() and i not in ranked]
                self._pool = ranked[:6]
            except Exception:
                self._pool = []
        now = time.monotonic()
        for m in self._pool:
            if m != self.fallback_model and self._limited_until.get(m, 0) <= now:
                return m
        return None

    # ------------------------------------------------------------- Gemini
    async def list_gemini_models(self) -> list[dict]:
        r = await self._http.get(
            "https://generativelanguage.googleapis.com/v1beta/models",
            params={"pageSize": 200},
            headers={"x-goog-api-key": self.s.gemini_api_key},
        )
        r.raise_for_status()
        return r.json().get("models", [])

    async def _rediscover_gemini(self) -> bool:
        async with self._lock:
            try:
                new = pick_gemini_model(await self.list_gemini_models())
            except Exception as e:
                log.warning("Could not list Gemini models: %s", _err(e))
                return False
            if not new or new == self.gemini_model:
                return False
            log.warning("Gemini model %s unavailable; switching to %s "
                        "(set GEMINI_MODEL=%s in .env to skip this)", self.gemini_model, new, new)
            self.gemini_model = new
            return True

    async def _gemini(self, system: str, prompt: str, schema: dict) -> dict:
        url = (
            "https://generativelanguage.googleapis.com/v1beta/models/"
            f"{self.gemini_model}:generateContent"
        )
        body = {
            "systemInstruction": {"parts": [{"text": system}]},
            "contents": [{"role": "user", "parts": [{"text": prompt}]}],
            "generationConfig": {
                "temperature": 0.2,
                "responseMimeType": "application/json",
                "responseSchema": to_gemini_schema(schema),
            },
        }
        try:
            r = await self._http.post(url, json=body, headers={"x-goog-api-key": self.s.gemini_api_key},
                                      timeout=60.0 if self.s.fallback_api_key else 150.0)
        except (httpx.TimeoutException, httpx.TransportError) as e:
            raise _Retryable(_err(e))
        if r.status_code == 404:
            raise _ModelMissing(f"model {self.gemini_model} not found")
        if r.status_code == 429:
            raise _RateLimited(retry_after(r), "Gemini quota")
        if r.status_code in (500, 502, 503, 504):
            raise _Retryable(f"HTTP {r.status_code}")
        if r.status_code >= 400:
            raise LLMError(f"Gemini HTTP {r.status_code}: {r.text[:300]}")
        data = r.json()
        try:
            parts = data["candidates"][0]["content"]["parts"]
            text = "".join(p.get("text", "") for p in parts if not p.get("thought"))
        except (KeyError, IndexError):
            raise LLMError(f"Gemini returned no content: {str(data)[:300]}")
        return parse_json(text)

    # ------------------------------------------------------------- OpenAI-compatible fallback
    async def list_fallback_models(self) -> list[str]:
        r = await self._http.get(
            f"{self.s.fallback_base_url.rstrip('/')}/models",
            headers={"Authorization": f"Bearer {self.s.fallback_api_key}"},
        )
        r.raise_for_status()
        return [m["id"] for m in r.json().get("data", [])]

    async def _rediscover_fallback(self) -> bool:
        async with self._lock:
            try:
                new = pick_fallback_model(await self.list_fallback_models())
            except Exception as e:
                log.warning("Could not list fallback models: %s", _err(e))
                return False
            if not new or new == self.fallback_model:
                return False
            log.warning("Fallback model %s unavailable; switching to %s "
                        "(set FALLBACK_MODEL=%s in .env to skip this)", self.fallback_model, new, new)
            self.fallback_model = new
            return True

    async def _openai_compat(self, system: str, prompt: str, schema: dict) -> dict:
        sys_msg = (
            f"{system}\n\nRespond with ONLY a JSON object matching this JSON schema:\n"
            f"{json.dumps(schema)}"
        )
        try:
            r = await self._http.post(
                f"{self.s.fallback_base_url.rstrip('/')}/chat/completions",
                headers={"Authorization": f"Bearer {self.s.fallback_api_key}"},
                json={
                    "model": self.fallback_model,
                    "temperature": 0.2,
                    "max_tokens": 2048,
                    **({"reasoning_effort": "low"} if "gpt-oss" in self.fallback_model else {}),
                    "response_format": {"type": "json_object"},
                    "messages": [
                        {"role": "system", "content": sys_msg},
                        {"role": "user", "content": prompt},
                    ],
                },
            )
        except (httpx.TimeoutException, httpx.TransportError) as e:
            raise LLMError(_err(e))
        if r.status_code == 404 or "model_not_found" in r.text[:500] or "decommissioned" in r.text[:500]:
            raise _ModelMissing(f"model {self.fallback_model} not found")
        if r.status_code == 429:
            raise _RateLimited(retry_after(r), r.text[:200])
        if r.status_code in (500, 502, 503, 504):
            raise LLMError(f"fallback HTTP {r.status_code}")
        if r.status_code >= 400:
            raise LLMError(f"fallback HTTP {r.status_code}: {r.text[:300]}")
        return parse_json(r.json()["choices"][0]["message"]["content"])
