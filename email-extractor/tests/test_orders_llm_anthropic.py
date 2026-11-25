"""The Claude (Anthropic) backend of `llm.Client` (#511).

The owner's Claude Ultra plan includes $200/month of Claude API credit, so the order and DL
engines run on `claude-sonnet-5-5`. `llm.Client` stays the ONE entry point (cache key,
offline replay and spend tally unchanged); a `claude-*` model id routes both calls to the
Anthropic Messages API. Only the external API is faked here (the SDK client factory), never
the Client itself.
"""
import base64
import json
from types import SimpleNamespace

import pytest

from app.config import Config
from app.orders import llm

SCHEMA = {"type": "object",
          "properties": {"gtin": {"type": "string"},
                         "items": {"type": "array",
                                   "items": {"type": "object",
                                             "properties": {"name": {"type": "string"}}}}},
          "required": ["gtin"]}


class FakeAnthropic:
    """Stands in for `anthropic.Anthropic(...)`: records every streamed request."""

    def __init__(self, texts, stop_reason="end_turn", usage=None):
        self.texts = list(texts)
        self.stop_reason = stop_reason
        self.usage = usage or SimpleNamespace(input_tokens=1000, output_tokens=200,
                                              cache_read_input_tokens=0,
                                              cache_creation_input_tokens=0)
        self.requests = []
        self.messages = SimpleNamespace(stream=self._stream)

    def _stream(self, **kw):
        self.requests.append(kw)
        text = self.texts.pop(0)
        msg = SimpleNamespace(content=[SimpleNamespace(type="text", text=text)],
                              stop_reason=self.stop_reason,
                              stop_details=SimpleNamespace(category="cyber"),
                              usage=self.usage)

        class _Ctx:
            def __enter__(self_inner):
                return SimpleNamespace(get_final_message=lambda: msg)

            def __exit__(self_inner, *a):
                return False
        return _Ctx()


def _client(tmp_path, fake, model="claude-sonnet-5-5", **kw):
    seen = {}

    def factory(api_key, timeout):
        seen["key"] = api_key
        return fake
    c = llm.Client(anthropic_api_key="sk-ant-test", cache_dir=str(tmp_path), model=model,
                   anthropic_factory=factory, **kw)
    return c, seen


def test_a_claude_model_sends_json_call_to_the_messages_api(tmp_path):
    fake = FakeAnthropic([json.dumps({"gtin": "48", "items": []})])
    c, seen = _client(tmp_path, fake)
    out = c.json_call("SYSTEM PROMPT", "USER TEXT", SCHEMA, name="product")
    assert out == {"gtin": "48", "items": []}
    assert seen["key"] == "sk-ant-test"
    req = fake.requests[0]
    assert req["model"] == "claude-sonnet-5-5"
    assert req["system"] == "SYSTEM PROMPT"
    assert req["messages"] == [{"role": "user", "content": "USER TEXT"}]
    assert req["output_config"]["effort"] == "high"
    fmt = req["output_config"]["format"]
    assert fmt["type"] == "json_schema"
    # structured outputs need every object closed — nested ones too
    assert fmt["schema"]["additionalProperties"] is False
    assert fmt["schema"]["properties"]["items"]["items"]["additionalProperties"] is False
    assert "additionalProperties" not in SCHEMA          # the caller's schema is untouched
    assert req["max_tokens"] >= 16000


def test_a_claude_answer_is_cached_and_replayed_offline(tmp_path):
    fake = FakeAnthropic([json.dumps({"gtin": "48"})])
    c, _ = _client(tmp_path, fake)
    c.json_call("S", "U", SCHEMA)
    off = llm.Client(cache_dir=str(tmp_path), model="claude-sonnet-5-5", offline=True)
    assert off.json_call("S", "U", SCHEMA) == {"gtin": "48"}
    assert len(fake.requests) == 1


def test_a_claude_refusal_is_an_error_never_an_empty_answer(tmp_path):
    fake = FakeAnthropic(["{}"], stop_reason="refusal")
    c, _ = _client(tmp_path, fake)
    with pytest.raises(llm.LlmError, match="refusal"):
        c.json_call("S", "U", SCHEMA)


def test_claude_usage_is_priced_through_the_shared_table(tmp_path):
    usage = SimpleNamespace(input_tokens=600_000, output_tokens=100_000,
                            cache_read_input_tokens=400_000, cache_creation_input_tokens=0)
    fake = FakeAnthropic([json.dumps({"gtin": "1"})], usage=usage)
    c, _ = _client(tmp_path, fake)
    c.json_call("S", "U", SCHEMA)
    p_in, p_cached, p_out = llm.PRICES["claude-sonnet-5-5"]
    expected = (600_000 * p_in + 400_000 * p_cached + 100_000 * p_out) / 1_000_000
    spend = c.spend()
    assert spend["tokens_in"] == 1_000_000 and spend["tokens_cached"] == 400_000
    assert spend["cost_usd"] == pytest.approx(expected)


def test_claude_vision_makes_n_independent_calls_with_image_blocks(tmp_path):
    fake = FakeAnthropic(["first transcript", "second transcript"])
    c, _ = _client(tmp_path, fake)
    out = c.vision_call("PREPÍŠ", images=[b"\xff\xd8page1", b"\xff\xd8page2"], n=2)
    assert out == ["first transcript", "second transcript"]
    assert len(fake.requests) == 2
    blocks = fake.requests[0]["messages"][0]["content"]
    assert [b["type"] for b in blocks] == ["image", "image", "text"]
    assert blocks[0]["source"] == {"type": "base64", "media_type": "image/jpeg",
                                   "data": base64.b64encode(b"\xff\xd8page1").decode()}
    assert blocks[2]["text"] == "PREPÍŠ"
    assert fake.requests[0]["output_config"] == {"effort": "high"}


def test_claude_vision_sends_a_whole_pdf_as_a_document_block(tmp_path):
    fake = FakeAnthropic(["only"])
    c, _ = _client(tmp_path, fake)
    c.vision_call("P", pdf_bytes=b"%PDF-1.4 x", n=1)
    block = fake.requests[0]["messages"][0]["content"][0]
    assert block["type"] == "document"
    assert block["source"]["media_type"] == "application/pdf"


def test_a_claude_model_without_an_anthropic_key_is_a_clear_error(tmp_path):
    c = llm.Client(api_key="sk-openai", cache_dir=str(tmp_path), model="claude-sonnet-5-5")
    with pytest.raises(llm.LlmError, match="anthropic_api_key"):
        c.json_call("S", "U", SCHEMA)


def test_a_gpt_model_still_uses_the_openai_transport(tmp_path):
    calls = []

    def transport(payload, api_key, timeout):
        calls.append(payload)
        return {"gtin": "9"}, None

    def factory(api_key, timeout):
        raise AssertionError("an OpenAI model must never reach the Anthropic API")
    c = llm.Client(api_key="sk-openai", cache_dir=str(tmp_path), model="gpt-5.4",
                   transport=transport, anthropic_factory=factory)
    assert c.json_call("S", "U", SCHEMA) == {"gtin": "9"}
    assert calls[0]["model"] == "gpt-5.4"


def test_from_config_hands_the_anthropic_key_and_the_claude_default_model():
    cfg = Config(pg_dsn="", data_dir="/tmp", anthropic_api_key="sk-ant-x")
    c = llm.from_config(cfg, offline=True)
    assert c.model == "claude-sonnet-5-5"
    assert c.anthropic_api_key == "sk-ant-x"


def test_config_reads_the_anthropic_key_from_the_environment(monkeypatch, tmp_path):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-env")
    cfg = Config.load()
    assert cfg.anthropic_api_key == "sk-ant-env"


# --- review findings (#511) -------------------------------------------------------------

def _png(w=10, h=10):
    import io

    from PIL import Image
    buf = io.BytesIO()
    Image.new("RGB", (w, h), "white").save(buf, "PNG")
    return buf.getvalue()


def test_claude_vision_declares_the_real_image_type(tmp_path):
    """The Messages API checks the bytes against `media_type`: a PNG labelled image/jpeg
    is a 400 — human_processing passes raw PNG/photo attachments through."""
    fake = FakeAnthropic(["t"])
    c, _ = _client(tmp_path, fake)
    c.vision_call("P", images=[_png()], n=1)
    assert fake.requests[0]["messages"][0]["content"][0]["source"]["media_type"] == "image/png"


def test_claude_vision_shrinks_a_huge_scan_below_the_api_image_limit(tmp_path):
    """A 600-DPI scanner JPEG can exceed the 5 MB per-image limit; Claude reads at most
    ~1568 px on the long edge anyway, so the page is scaled to that before sending."""
    import io

    from PIL import Image
    big = _png(5000, 7000)
    fake = FakeAnthropic(["t"])
    c, _ = _client(tmp_path, fake)
    c.vision_call("P", images=[big], n=1)
    src = fake.requests[0]["messages"][0]["content"][0]["source"]
    img = Image.open(io.BytesIO(base64.b64decode(src["data"])))
    assert max(img.size) <= 1568
    assert len(base64.b64decode(src["data"])) < 5 * 1024 * 1024


def test_a_dropped_connection_is_a_transient_failure():
    from app.orders import dl_retry
    assert dl_retry.TRANSIENT_RE.search("LlmError(\"APIConnectionError('Connection error.')\")")
