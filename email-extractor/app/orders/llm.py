"""Cached LLM client for the order pipeline (#61) — OpenAI or Claude (#511).

Two things this owns:

- **Structured output.** Every call asks for JSON against a schema, so a malformed answer
  is a hard error instead of a string the next stage has to guess at.
- **A content-addressed cache.** The key is (model, effort, system prompt, input, schema),
  so the deterministic evaluation tier (#66) replays the golden corpus offline in seconds
  and a prompt edit is automatically a cache miss. In `offline=True` a miss RAISES rather
  than calling out — CI must never spend money or hang on a network call.

Model and effort come from the config. The provider follows the model id (#511): `claude-*`
runs on the Anthropic Messages API (paid from the Claude plan's included API credit), any
other id on OpenAI. Default `claude-sonnet-5-5`, effort `high` — matched gpt-5.4 on the
order + DL corpora; rollback is the `orders_model` option set back to `gpt-5.4`.
"""
from __future__ import annotations

import base64
import copy
import hashlib
import json
import logging
import urllib.error
import urllib.request
from pathlib import Path

log = logging.getLogger("orders.llm")

API_URL = "https://api.openai.com/v1/responses"
# The DL vision transcription (#201) needs `n` independent samples of ONE document in a
# single call (R41/R43's dual-transcript cross-check) — the Responses API has no
# equivalent of Chat Completions' `n`, so vision_call() is a genuinely separate transport.
CHAT_API_URL = "https://api.openai.com/v1/chat/completions"
DEFAULT_MODEL = "claude-sonnet-5-5"
DEFAULT_EFFORT = "high"
TIMEOUT = 300
# Streaming keeps a large ceiling safe from HTTP timeouts; the answers themselves are small.
ANTHROPIC_MAX_TOKENS = 64000


class CacheMiss(Exception):
    """Offline mode was asked for an answer that is not in the cache."""


class LlmError(Exception):
    """The model call failed or returned something unusable."""


# PRICES: per 1M tokens (input, cached input, output) — OpenAI from
# developers.openai.com/api/docs/pricing (fetched 2026-07-31), Claude from
# platform.claude.com pricing (2026-11-25).
# An unlisted model RAISES rather than being priced at zero: a silent 0 would make the
# €30/month tripwire (#89) permanently green, which is worse than no tripwire at all.
PRICES = {
    "gpt-5.4": (2.50, 0.25, 15.00),
    "gpt-5.4-mini": (0.75, 0.075, 4.50),
    "gpt-5.5": (5.00, 0.50, 30.00),
    "claude-sonnet-5-5": (2.00, 0.10, 10.00),
    "claude-opus-5-5": (4.00, 0.20, 20.00),
    "claude-haiku-5-5": (0.10, 0.01, 0.50),
}


def is_anthropic(model: str) -> bool:
    """A `claude-*` model id runs on the Anthropic Messages API (#511)."""
    return str(model or "").startswith("claude-")


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


def _http_transport_chat(payload: dict, api_key: str, timeout: int) -> tuple[list[str], dict | None]:
    req = urllib.request.Request(
        CHAT_API_URL, method="POST",
        data=json.dumps(payload).encode(),
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 (fixed host)
            body = json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        raise LlmError(f"{e.code}: {e.read().decode()[:400]}") from e
    except Exception as e:
        raise LlmError(repr(e)) from e
    if body.get("error"):
        raise LlmError(str(body["error"])[:400])
    choices = body.get("choices") or []
    texts = [str((c.get("message") or {}).get("content") or "") for c in choices]
    if not texts:
        raise LlmError(f"no choices in vision response: {json.dumps(body)[:300]}")
    return texts, body.get("usage")


def _chat_usage_as_responses(usage: dict | None) -> dict | None:
    """Normalize Chat Completions `usage` into the Responses-API shape `cost_usd`/`_tally`
    already read, so vision_call is priced through the exact same table as json_call —
    never a second, untested pricing path. Carries reasoning-token detail through too
    (`completion_tokens_details.reasoning_tokens`), not just cached input and totals —
    `_tally`'s `tokens_reasoning` diagnostic would otherwise silently under-report every
    vision call, even though cost_usd itself is unaffected (reasoning tokens are already
    folded into `output_tokens` for pricing)."""
    if not usage:
        return None
    details_in = usage.get("prompt_tokens_details") or {}
    details_out = usage.get("completion_tokens_details") or {}
    return {
        "input_tokens": usage.get("prompt_tokens", 0),
        "output_tokens": usage.get("completion_tokens", 0),
        "input_tokens_details": {"cached_tokens": details_in.get("cached_tokens", 0)},
        "output_tokens_details": {"reasoning_tokens": details_out.get("reasoning_tokens", 0)},
    }


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


# --- Anthropic (Claude) transports (#511) -------------------------------------------------
# They take the SAME payload `Client` builds for OpenAI and translate it, so the cache key,
# offline replay and spend tally stay one code path for both providers.

def _closed_schema(schema: dict) -> dict:
    """Structured outputs need every object closed (`additionalProperties: false`), nested
    ones included. A copy — the caller's schema (also an OpenAI request) stays untouched."""
    out = copy.deepcopy(schema)

    def walk(node):
        if isinstance(node, dict):
            if node.get("type") == "object" or "properties" in node:
                node.setdefault("additionalProperties", False)
            for v in node.values():
                walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)
    walk(out)
    return out


def _anthropic_json_request(payload: dict) -> dict:
    system = next(m["content"] for m in payload["input"] if m["role"] == "system")
    user = next(m["content"] for m in payload["input"] if m["role"] == "user")
    return {"model": payload["model"], "max_tokens": ANTHROPIC_MAX_TOKENS, "system": system,
            "output_config": {"effort": payload["reasoning"]["effort"],
                              "format": {"type": "json_schema",
                                         "schema": _closed_schema(
                                             payload["text"]["format"]["schema"])}},
            "messages": [{"role": "user", "content": user}]}


# Claude reads an image at most ~1568 px on its long edge and rejects one over 5 MB, so a
# 600-DPI scanner page is scaled down before sending: it buys nothing and would be a 400.
ANTHROPIC_IMAGE_MAX_EDGE = 1568
_IMAGE_MAGIC = ((b"\x89PNG", "image/png"), (b"GIF8", "image/gif"), (b"\xff\xd8", "image/jpeg"))


def _image_media_type(raw: bytes) -> str:
    """The real type of an image — the Messages API checks the bytes against it."""
    for magic, media in _IMAGE_MAGIC:
        if raw.startswith(magic):
            return media
    if raw[:4] == b"RIFF" and raw[8:12] == b"WEBP":
        return "image/webp"
    return "image/jpeg"


def _anthropic_image(raw: bytes) -> tuple[str, str]:
    """(base64 data, media type) for one page: the real type, scaled to the API's limits."""
    import io

    from PIL import Image
    try:
        img = Image.open(io.BytesIO(raw))
        too_big = max(img.size) > ANTHROPIC_IMAGE_MAX_EDGE
    except Exception:
        # not decodable here — send it as it is and let the API answer for it
        return base64.b64encode(raw).decode(), _image_media_type(raw)
    if not too_big:
        return base64.b64encode(raw).decode(), _image_media_type(raw)
    img.thumbnail((ANTHROPIC_IMAGE_MAX_EDGE, ANTHROPIC_IMAGE_MAX_EDGE))
    buf = io.BytesIO()
    img.convert("RGB").save(buf, "JPEG", quality=85)
    log.info("claude vision: page %sx%s scaled to %s (%d -> %d bytes)",
             *Image.open(io.BytesIO(raw)).size, img.size, len(raw), buf.tell())
    return base64.b64encode(buf.getvalue()).decode(), "image/jpeg"


def _anthropic_vision_request(payload: dict) -> tuple[dict, int]:
    """One request per sample: the Messages API has no `n`, so `n` independent calls."""
    blocks: list[dict] = []
    prompt = ""
    for part in payload["messages"][0]["content"]:
        if part["type"] == "text":
            prompt = part["text"]
        elif part["type"] == "image_url":
            raw = base64.b64decode(part["image_url"]["url"].split(",", 1)[1])
            data, media = _anthropic_image(raw)
            blocks.append({"type": "image", "source": {"type": "base64",
                                                       "media_type": media,
                                                       "data": data}})
        elif part["type"] == "file":
            data = part["file"]["file_data"].split(",", 1)[1]
            blocks.append({"type": "document", "source": {"type": "base64",
                                                          "media_type": "application/pdf",
                                                          "data": data}})
    blocks.append({"type": "text", "text": prompt})
    req = {"model": payload["model"], "max_tokens": ANTHROPIC_MAX_TOKENS,
           "output_config": {"effort": payload["reasoning_effort"]},
           "messages": [{"role": "user", "content": blocks}]}
    return req, int(payload.get("n", 1) or 1)


def _anthropic_message(factory, api_key: str, timeout: int, request: dict):
    try:
        client = factory(api_key, timeout)
        with client.messages.stream(**request) as stream:
            msg = stream.get_final_message()
    except Exception as e:
        # rate limit / overload / timeout texts keep their wording for the transient checks
        raise LlmError(repr(e)) from e
    if msg.stop_reason == "refusal":
        raise LlmError(f"refusal: {getattr(msg, 'stop_details', None)}")
    if msg.stop_reason == "max_tokens":
        raise LlmError("Claude answer cut off at max_tokens")
    return msg


def _anthropic_text(msg) -> str:
    return "".join(b.text for b in msg.content if getattr(b, "type", "") == "text")


def _anthropic_tokens(usage) -> tuple[int, int, int]:
    """(all input incl. cache reads/writes, cache reads, output) from Anthropic usage."""
    read = int(getattr(usage, "cache_read_input_tokens", 0) or 0)
    write = int(getattr(usage, "cache_creation_input_tokens", 0) or 0)
    return int(usage.input_tokens or 0) + read + write, read, int(usage.output_tokens or 0)


def _default_anthropic_factory(api_key: str, timeout: int):   # pragma: no cover - network
    import anthropic
    return anthropic.Anthropic(api_key=api_key, timeout=timeout, max_retries=4)


def _anthropic_json_transport(factory):
    def transport(payload: dict, api_key: str, timeout: int):
        msg = _anthropic_message(factory, api_key, timeout, _anthropic_json_request(payload))
        text = _anthropic_text(msg)
        try:
            result = json.loads(text)
        except json.JSONDecodeError as e:
            raise LlmError(f"model returned non-JSON: {text[:200]}") from e
        tin, cached, tout = _anthropic_tokens(msg.usage)
        return result, {"input_tokens": tin, "output_tokens": tout,
                        "input_tokens_details": {"cached_tokens": cached},
                        "output_tokens_details": {"reasoning_tokens": 0}}
    return transport


def _anthropic_vision_transport(factory):
    def transport(payload: dict, api_key: str, timeout: int):
        request, n = _anthropic_vision_request(payload)
        texts, tin, cached, tout = [], 0, 0, 0
        for _ in range(n):
            msg = _anthropic_message(factory, api_key, timeout, request)
            texts.append(_anthropic_text(msg))
            a, b, c = _anthropic_tokens(msg.usage)
            tin, cached, tout = tin + a, cached + b, tout + c
        # Chat-Completions-shaped, so vision_call prices it through the same normaliser
        return texts, {"prompt_tokens": tin, "completion_tokens": tout,
                       "prompt_tokens_details": {"cached_tokens": cached},
                       "completion_tokens_details": {"reasoning_tokens": 0}}
    return transport


class Client:
    def __init__(self, api_key: str = "", cache_dir: str = "", model: str = DEFAULT_MODEL,
                 reasoning_effort: str = DEFAULT_EFFORT, offline: bool = False,
                 transport=None, timeout: int = TIMEOUT, vision_transport=None,
                 anthropic_api_key: str = "", anthropic_factory=None):
        self.api_key = api_key
        self.anthropic_api_key = anthropic_api_key
        self.cache_dir = Path(cache_dir) if cache_dir else None
        self.model = model or DEFAULT_MODEL
        self.reasoning_effort = reasoning_effort or DEFAULT_EFFORT
        self.offline = offline
        self.timeout = timeout
        if is_anthropic(self.model):
            factory = anthropic_factory or _default_anthropic_factory
            self._transport = transport or _anthropic_json_transport(factory)
            self._vision_transport = vision_transport or _anthropic_vision_transport(factory)
        else:
            self._transport = transport or _http_transport
            self._vision_transport = vision_transport or _http_transport_chat
        self.last_prompt_hash = ""
        self.last_cached = False
        # The running bill for this client, so a run can persist what it cost (#89).
        self._spend = {"calls": 0, "cached_calls": 0, "tokens_in": 0, "tokens_cached": 0,
                       "tokens_out": 0, "tokens_reasoning": 0, "cost_usd": 0.0}

    def _key_for_model(self) -> str:
        """The API key of the model's provider, or a clear error naming the missing option."""
        if is_anthropic(self.model):
            if not self.anthropic_api_key:
                raise LlmError("no Anthropic API key configured (option anthropic_api_key)")
            return self.anthropic_api_key
        if not self.api_key:
            raise LlmError("no OpenAI API key configured (option openai_api_key)")
        return self.api_key

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
        api_key = self._key_for_model()

        payload = {
            "model": self.model,
            "reasoning": {"effort": self.reasoning_effort},
            "input": [{"role": "system", "content": system},
                      {"role": "user", "content": user}],
            "text": {"format": {"type": "json_schema", "name": name,
                                "schema": schema, "strict": False}},
        }
        answer = self._transport(payload, api_key, self.timeout)
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

    # --- vision (multimodal, #201) ----------------------------------------

    def _vision_key(self, prompt: str, images: list[bytes] | None,
                     pdf_bytes: bytes | None, n: int) -> str:
        h = hashlib.sha256()
        for part in (self.model, self.reasoning_effort, prompt, str(n)):
            h.update(part.encode())
            h.update(b"\x00")
        for img in images or []:
            h.update(hashlib.sha256(img).digest())
        if pdf_bytes:
            h.update(b"pdf:")
            h.update(hashlib.sha256(pdf_bytes).digest())
        return h.hexdigest()

    def vision_call(self, prompt: str, images: list[bytes] | None = None,
                     pdf_bytes: bytes | None = None, n: int = 2) -> list[str]:
        """`n` independent transcripts of ONE document — either its scanned page
        images (R40/R43) or the whole PDF as a file part (the digital-PDF vision
        fallback, R42). A raw Chat Completions call, content-addressed-cached and
        offline-safe exactly like `json_call`, priced through the same table.
        """
        if not images and not pdf_bytes:
            raise LlmError("vision_call needs images or pdf_bytes")
        key = self._vision_key(prompt, images, pdf_bytes, n)
        path = self._cache_path(key)
        if path and path.exists():
            self.last_cached = True
            self._tally(None, cached=True)
            return json.loads(path.read_text(encoding="utf-8"))
        if self.offline:
            raise CacheMiss(
                f"offline: no cached vision answer for {key[:12]}. "
                "Record it with the live tier before relying on it in CI.")
        api_key = self._key_for_model()

        content: list[dict] = [{"type": "text", "text": prompt}]
        for img in images or []:
            b64 = base64.b64encode(img).decode()
            content.append({"type": "image_url",
                            "image_url": {"url": f"data:image/jpeg;base64,{b64}",
                                         "detail": "high"}})
        if pdf_bytes:
            b64 = base64.b64encode(pdf_bytes).decode()
            content.append({"type": "file",
                            "file": {"filename": "document.pdf",
                                    "file_data": f"data:application/pdf;base64,{b64}"}})
        payload = {
            "model": self.model,
            "reasoning_effort": self.reasoning_effort,
            "n": n,
            "messages": [{"role": "user", "content": content}],
        }
        answer = self._vision_transport(payload, api_key, self.timeout)
        texts, usage = answer if isinstance(answer, tuple) else (answer, None)
        self.last_cached = False
        self._tally(_chat_usage_as_responses(usage), cached=False)
        if path:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(texts, ensure_ascii=False, indent=1),
                            encoding="utf-8")
        return texts


def from_config(cfg, offline: bool = False) -> Client:
    return Client(api_key=getattr(cfg, "openai_api_key", ""),
                  anthropic_api_key=getattr(cfg, "anthropic_api_key", "") or "",
                  cache_dir=getattr(cfg, "llm_cache_dir", "") or "",
                  model=getattr(cfg, "orders_model", DEFAULT_MODEL),
                  reasoning_effort=getattr(cfg, "orders_reasoning_effort", DEFAULT_EFFORT),
                  offline=offline)
