"""Cached OpenAI client for the order pipeline (#61).

Two things this owns:

- **Structured output.** Every call asks for JSON against a schema, so a malformed answer
  is a hard error instead of a string the next stage has to guess at.
- **A content-addressed cache.** The key is (model, effort, system prompt, input, schema),
  so the deterministic evaluation tier (#66) replays the golden corpus offline in seconds
  and a prompt edit is automatically a cache miss. In `offline=True` a miss RAISES rather
  than calling out — CI must never spend money or hang on a network call.

Model and effort come from the config; the standing decision is the top tier
(`gpt-5.4`, reasoning `high`), never downgraded to save cost.
"""
from __future__ import annotations

import hashlib
import json
import logging
import urllib.error
import urllib.request
from pathlib import Path

log = logging.getLogger("orders.llm")

API_URL = "https://api.openai.com/v1/responses"
DEFAULT_MODEL = "gpt-5.4"
DEFAULT_EFFORT = "high"
TIMEOUT = 300


class CacheMiss(Exception):
    """Offline mode was asked for an answer that is not in the cache."""


class LlmError(Exception):
    """The model call failed or returned something unusable."""


# PRICES: per 1M tokens, developers.openai.com/api/docs/pricing (fetched 2026-07-31).
# An unlisted model RAISES rather than being priced at zero: a silent 0 would make the
# €30/month tripwire (#89) permanently green, which is worse than no tripwire at all.
PRICES = {
    "gpt-5.4": (2.50, 0.25, 15.00),
    "gpt-5.4-mini": (0.75, 0.075, 4.50),
    "gpt-5.5": (5.00, 0.50, 30.00),
}


def cost_usd(usage: dict | None, model: str = DEFAULT_MODEL) -> float:
    """What one API answer cost, from the API's own token counts."""
    if not usage:
        return 0.0
    if model not in PRICES:
        raise LlmError(f"no price known for model {model} — add it to llm.PRICES")
    p_in, p_cached, p_out = PRICES[model]
    cached = int((usage.get("input_tokens_details") or {}).get("cached_tokens", 0) or 0)
    fresh = max(0, int(usage.get("input_tokens", 0) or 0) - cached)
    out = int(usage.get("output_tokens", 0) or 0)
    return (fresh * p_in + cached * p_cached + out * p_out) / 1_000_000


def _http_transport(payload: dict, api_key: str, timeout: int) -> tuple[dict, dict | None]:
    req = urllib.request.Request(
        API_URL, method="POST",
        data=json.dumps(payload).encode(),
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 (fixed host)
            body = json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        raise LlmError(f"{e.code}: {e.read().decode()[:400]}") from e
    except Exception as e:
        raise LlmError(repr(e)) from e
    return _parse_response(body), body.get("usage")


def _parse_response(body: dict) -> dict:
    """Pull the JSON payload out of a Responses API answer."""
    if body.get("error"):
        raise LlmError(str(body["error"])[:400])
    for item in body.get("output", []):
        for part in item.get("content", []) or []:
            text = part.get("text")
            if text:
                try:
                    return json.loads(text)
                except json.JSONDecodeError as e:
                    raise LlmError(f"model returned non-JSON: {text[:200]}") from e
    text = body.get("output_text")
    if text:
        return json.loads(text)
    raise LlmError(f"no output in response: {json.dumps(body)[:300]}")


class Client:
    def __init__(self, api_key: str = "", cache_dir: str = "", model: str = DEFAULT_MODEL,
                 reasoning_effort: str = DEFAULT_EFFORT, offline: bool = False,
                 transport=None, timeout: int = TIMEOUT):
        self.api_key = api_key
        self.cache_dir = Path(cache_dir) if cache_dir else None
        self.model = model or DEFAULT_MODEL
        self.reasoning_effort = reasoning_effort or DEFAULT_EFFORT
        self.offline = offline
        self.timeout = timeout
        self._transport = transport or _http_transport
        self.last_prompt_hash = ""
        self.last_cached = False
        # The running bill for this client, so a run can persist what it cost (#89).
        self._spend = {"calls": 0, "cached_calls": 0, "tokens_in": 0, "tokens_cached": 0,
                       "tokens_out": 0, "tokens_reasoning": 0, "cost_usd": 0.0}

    def spend(self) -> dict:
        return dict(self._spend, model=self.model)

    def _tally(self, usage: dict | None, cached: bool) -> None:
        self._spend["calls"] += 1
        if cached:
            # A replayed answer is free — counted, so the deterministic tier's saving shows.
            self._spend["cached_calls"] += 1
            return
        if not usage:
            return
        details_in = usage.get("input_tokens_details") or {}
        details_out = usage.get("output_tokens_details") or {}
        self._spend["tokens_in"] += int(usage.get("input_tokens", 0) or 0)
        self._spend["tokens_cached"] += int(details_in.get("cached_tokens", 0) or 0)
        self._spend["tokens_out"] += int(usage.get("output_tokens", 0) or 0)
        self._spend["tokens_reasoning"] += int(details_out.get("reasoning_tokens", 0) or 0)
        self._spend["cost_usd"] += cost_usd(usage, self.model)

    # --- cache -----------------------------------------------------------

    def _key(self, system: str, user: str, schema: dict) -> str:
        h = hashlib.sha256()
        for part in (self.model, self.reasoning_effort, system, user,
                     json.dumps(schema, sort_keys=True)):
            h.update(part.encode())
            h.update(b"\x00")
        return h.hexdigest()

    def _cache_path(self, key: str) -> Path | None:
        return self.cache_dir / f"{key}.json" if self.cache_dir else None

    # --- call ------------------------------------------------------------

    def json_call(self, system: str, user: str, schema: dict, name: str = "result") -> dict:
        self.last_prompt_hash = hashlib.sha256(system.encode()).hexdigest()[:12]
        key = self._key(system, user, schema)
        path = self._cache_path(key)
        if path and path.exists():
            self.last_cached = True
            self._tally(None, cached=True)
            return json.loads(path.read_text(encoding="utf-8"))
        if self.offline:
            raise CacheMiss(
                f"offline: no cached answer for {key[:12]} (prompt {self.last_prompt_hash}). "
                "Record it with the live tier before relying on it in CI.")
        if not self.api_key:
            raise LlmError("no OpenAI API key configured (option openai_api_key)")

        payload = {
            "model": self.model,
            "reasoning": {"effort": self.reasoning_effort},
            "input": [{"role": "system", "content": system},
                      {"role": "user", "content": user}],
            "text": {"format": {"type": "json_schema", "name": name,
                                "schema": schema, "strict": False}},
        }
        answer = self._transport(payload, self.api_key, self.timeout)
        # A transport may report the token usage alongside the answer; the test doubles (and
        # any older transport) return the answer alone, and then the run is simply unpriced.
        result, usage = answer if isinstance(answer, tuple) else (answer, None)
        self.last_cached = False
        self._tally(usage, cached=False)
        if path:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(result, ensure_ascii=False, indent=1),
                            encoding="utf-8")
        return result


def from_config(cfg, offline: bool = False) -> Client:
    return Client(api_key=getattr(cfg, "openai_api_key", ""),
                  cache_dir=getattr(cfg, "llm_cache_dir", "") or "",
                  model=getattr(cfg, "orders_model", DEFAULT_MODEL),
                  reasoning_effort=getattr(cfg, "orders_reasoning_effort", DEFAULT_EFFORT),
                  offline=offline)
