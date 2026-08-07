"""The multimodal Vision capability on `llm.Client` (#201, DL migration F2).

`json_call` (Responses API) has no equivalent of the Chat Completions `n` parameter — the
DL vision transcription needs `n=2` independent samples of the SAME document in one call
(R41/R43's dual-transcript cross-check), so this is a genuinely separate transport, not a
variant of `json_call`. Pinned here:

1. it hits a DIFFERENT transport (`vision_transport`, never `transport`) with image/file
   content parts;
2. it is content-addressed-cached and offline-safe exactly like `json_call` (same
   CacheMiss/LlmError contract);
3. its usage is priced through the SAME `PRICES`/`cost_usd` table as every other call —
   never a second, untested pricing path.
"""
import base64
import json

import pytest

from app.orders import llm

JPEG_A = b"\xff\xd8\xff\xe0fake-page-one\xff\xd9"
JPEG_B = b"\xff\xd8\xff\xe0fake-page-two\xff\xd9"
PDF_BYTES = b"%PDF-1.4 fake whole document bytes"


def _chat_usage(prompt=1000, cached=0, completion=200, reasoning=0):
    return {"prompt_tokens": prompt, "completion_tokens": completion,
            "prompt_tokens_details": {"cached_tokens": cached},
            "completion_tokens_details": {"reasoning_tokens": reasoning}}


def _client(tmp_path, texts, usage=None, calls=None):
    calls = calls if calls is not None else {"n": 0, "payloads": []}

    def vision_transport(payload, api_key, timeout):
        calls["n"] += 1
        calls["payloads"].append(payload)
        return texts, usage

    c = llm.Client(api_key="k", cache_dir=str(tmp_path), vision_transport=vision_transport)
    return c, calls


# --- the call itself -------------------------------------------------------

def test_vision_call_returns_every_sample_the_transport_gave_back(tmp_path):
    c, _ = _client(tmp_path, ["prepis A", "prepis B"])
    got = c.vision_call("transcribe this", images=[JPEG_A, JPEG_B], n=2)
    assert got == ["prepis A", "prepis B"]


def test_vision_call_sends_one_image_content_part_per_page_in_order(tmp_path):
    c, calls = _client(tmp_path, ["a", "b"])
    c.vision_call("transcribe", images=[JPEG_A, JPEG_B], n=2)
    content = calls["payloads"][0]["messages"][0]["content"]
    image_parts = [p for p in content if p["type"] == "image_url"]
    assert len(image_parts) == 2
    assert image_parts[0]["image_url"]["detail"] == "high"

    def _decoded(part):
        url = part["image_url"]["url"]
        assert url.startswith("data:image/jpeg;base64,")
        return base64.b64decode(url.split(",", 1)[1])

    # order preserved — page 1 before page 2, never scrambled
    assert _decoded(image_parts[0]) == JPEG_A
    assert _decoded(image_parts[1]) == JPEG_B


def test_vision_call_sends_the_whole_pdf_as_a_file_part_when_no_images_given(tmp_path):
    c, calls = _client(tmp_path, ["digital transcript"])
    c.vision_call("transcribe", pdf_bytes=PDF_BYTES, n=1)
    content = calls["payloads"][0]["messages"][0]["content"]
    file_parts = [p for p in content if p["type"] == "file"]
    assert len(file_parts) == 1
    assert file_parts[0]["file"]["filename"] == "document.pdf"


def test_vision_call_asks_for_n_samples_in_one_call(tmp_path):
    c, calls = _client(tmp_path, ["x", "y"])
    c.vision_call("transcribe", images=[JPEG_A], n=2)
    assert calls["payloads"][0]["n"] == 2


def test_vision_call_never_downgrades_model_or_effort(tmp_path):
    """Standing directive: gpt-5.4 reasoning high, never downgraded."""
    c, calls = _client(tmp_path, ["x"])
    c.vision_call("transcribe", images=[JPEG_A], n=1)
    assert calls["payloads"][0]["model"] == "gpt-5.4"
    assert calls["payloads"][0]["reasoning_effort"] == "high"


def test_vision_call_needs_either_images_or_a_pdf(tmp_path):
    c, _ = _client(tmp_path, ["x"])
    with pytest.raises(llm.LlmError):
        c.vision_call("transcribe")


# --- caching -----------------------------------------------------------

def test_a_repeat_vision_call_is_served_from_cache_not_the_transport(tmp_path):
    c, calls = _client(tmp_path, ["first answer"])
    c.vision_call("transcribe", images=[JPEG_A], n=2)
    assert calls["n"] == 1
    got = c.vision_call("transcribe", images=[JPEG_A], n=2)
    assert got == ["first answer"]
    assert calls["n"] == 1   # never called the transport a second time
    assert c.last_cached is True


def test_a_different_image_is_a_different_cache_key(tmp_path):
    c, calls = _client(tmp_path, ["irrelevant — same fake transport for both calls"])
    c.vision_call("transcribe", images=[JPEG_A], n=1)
    c.vision_call("transcribe", images=[JPEG_B], n=1)
    assert calls["n"] == 2


def test_a_different_n_is_a_different_cache_key(tmp_path):
    c, calls = _client(tmp_path, ["x"])
    c.vision_call("transcribe", images=[JPEG_A], n=1)
    c.vision_call("transcribe", images=[JPEG_A], n=2)
    assert calls["n"] == 2


def test_offline_mode_raises_cache_miss_instead_of_calling_the_transport(tmp_path):
    calls = {"n": 0}

    def vision_transport(payload, api_key, timeout):
        calls["n"] += 1
        return ["should never be reached"], None

    c = llm.Client(api_key="k", cache_dir=str(tmp_path), offline=True,
                    vision_transport=vision_transport)
    with pytest.raises(llm.CacheMiss):
        c.vision_call("transcribe", images=[JPEG_A], n=2)
    assert calls["n"] == 0


def test_offline_mode_still_serves_an_already_cached_vision_answer(tmp_path):
    live = llm.Client(api_key="k", cache_dir=str(tmp_path),
                       vision_transport=lambda p, a, t: (["cached answer"], None))
    live.vision_call("transcribe", images=[JPEG_A], n=2)

    offline = llm.Client(api_key="k", cache_dir=str(tmp_path), offline=True,
                          vision_transport=lambda p, a, t: (_ for _ in ()).throw(
                              AssertionError("must not call the transport")))
    got = offline.vision_call("transcribe", images=[JPEG_A], n=2)
    assert got == ["cached answer"]


def test_no_api_key_raises_before_ever_touching_the_transport(tmp_path):
    calls = {"n": 0}

    def vision_transport(payload, api_key, timeout):
        calls["n"] += 1
        return ["x"], None

    c = llm.Client(api_key="", cache_dir=str(tmp_path), vision_transport=vision_transport)
    with pytest.raises(llm.LlmError):
        c.vision_call("transcribe", images=[JPEG_A], n=1)
    assert calls["n"] == 0


# --- spend, priced through the SAME table as json_call --------------------

def test_vision_usage_is_tallied_through_the_shared_pricing_table(tmp_path):
    c, _ = _client(tmp_path, ["a", "b"], usage=_chat_usage(prompt=1_000_000, cached=0,
                                                            completion=1_000_000))
    c.vision_call("transcribe", images=[JPEG_A], n=2)
    spend = c.spend()
    assert spend["tokens_in"] == 1_000_000
    assert spend["tokens_out"] == 1_000_000
    assert spend["cost_usd"] == pytest.approx(2.50 + 15.00)


def test_vision_reasoning_tokens_are_tallied_too(tmp_path):
    c, _ = _client(tmp_path, ["a"], usage=_chat_usage(reasoning=350))
    c.vision_call("transcribe", images=[JPEG_A], n=1)
    assert c.spend()["tokens_reasoning"] == 350


def test_a_cached_vision_hit_costs_nothing(tmp_path):
    c, _ = _client(tmp_path, ["a"], usage=_chat_usage(prompt=1_000_000, completion=1_000_000))
    c.vision_call("transcribe", images=[JPEG_A], n=1)
    c.vision_call("transcribe", images=[JPEG_A], n=1)   # cache hit, second time
    spend = c.spend()
    assert spend["calls"] == 2
    assert spend["cached_calls"] == 1
    # only ONE call's worth of tokens/cost was ever tallied
    assert spend["tokens_in"] == 1_000_000


def test_vision_cache_file_is_plain_json_on_disk(tmp_path):
    c, _ = _client(tmp_path, ["prepis"])
    c.vision_call("transcribe", images=[JPEG_A], n=1)
    files = list(tmp_path.glob("*.json"))
    assert len(files) == 1
    assert json.loads(files[0].read_text(encoding="utf-8")) == ["prepis"]
