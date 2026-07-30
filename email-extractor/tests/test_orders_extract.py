"""Extraction stage (#61): email -> orders, with the deterministic parts in charge.

Three responsibilities, in order of authority:

1. a recognized tabular attachment is parsed by CODE and OVERRIDES the model — the grid
   is deterministic, the model is not;
2. everything the model does return is citation-checked against the source text, so an
   invented item cannot reach the warehouse;
3. an unverifiable item is never dropped silently — it is reported.

The LLM itself is exercised through a cache so the whole stage is testable offline; the
live-model tier is #66.
"""
import json

from app.orders import extract, llm

KOSIK_MAIL = """Dobrý deň, posielam objednávku.

Attachments:
=====
Č.mat.dodavat.,EAN kus,Název,Množství
12345,8588001800013,Rožok štandart 50g,120
12346,8588001805889,Bábovka mini kakaová 200g,0
12347,8588001800020,Vianočka 400g,7
=====
"""

PRICELIST_MAIL = (
    "Objednávka na 30.7.\n\nAttachments:\n=====\n"
    "Názov tovaru\t%DPH\tVO bez DPH\tVO s DPH\tTrvanlivosť v dňoch\n"
    "Rožok štandart 50g\t5\t0.38\t0.40\t1\t24\n"
    "Vianočka 400g\t5\t1.00\t1.19\t3\t\n"
    "Bábovka mini kakaová 200g\t5\t1.50\t1.60\t3\t6\n"
    "=====\n"
)

FREE_TEXT_MAIL = """Dobrý deň,

na zajtra prosím 40x rožok na pacientov a 11 ks vianočka na zamestnancov.

Ďakujem
"""


# --- 1) tabular attachments are parsed by code ---------------------------

def test_a_kosik_table_is_parsed_by_code_and_zero_quantities_are_skipped():
    items = extract.parse_table(KOSIK_MAIL)
    assert [(i["name"], i["quantity"]) for i in items] == [
        ("Rožok štandart 50g", 120), ("Vianočka 400g", 7)]


def test_a_price_list_takes_the_quantity_from_the_column_after_the_headers():
    """The quantity column sits after the last labelled column, and the number of price
    columns differs per template — so the header row decides, never a fixed index."""
    items = extract.parse_table(PRICELIST_MAIL)
    assert [(i["name"], i["quantity"]) for i in items] == [
        ("Rožok štandart 50g", 24), ("Bábovka mini kakaová 200g", 6)]


def test_prices_are_never_read_as_quantities():
    items = extract.parse_table(PRICELIST_MAIL)
    assert all(i["quantity"] not in (0.4, 1.19, 1.6) for i in items)


def test_a_free_text_email_has_no_table_to_parse():
    assert extract.parse_table(FREE_TEXT_MAIL) is None


# --- 2) citation checking ------------------------------------------------

def test_an_invented_item_is_rejected_and_reported():
    """A phantom item is the one failure that must never reach ORION silently."""
    result = extract.verify(
        {"orders": [{"deliveryDate": "31.07.2026", "recipientGroup": "", "items": [
            {"name": "rožok", "quantity": 40, "unit": "ks", "sourceQuote": "40x rožok"},
            {"name": "torta", "quantity": 5, "unit": "ks", "sourceQuote": "5x torta"},
        ]}]},
        FREE_TEXT_MAIL)
    assert [i["name"] for i in result["orders"][0]["items"]] == ["rožok"]
    assert [i["name"] for i in result["unverified"]] == ["torta"]


def test_a_quote_survives_invisible_characters_and_respacing():
    """Some clients send zero-width spaces inside '45​ks', and the model re-spaces its
    quotes; neither may turn a real item into an unverified one (AGEL incident)."""
    source = "chlieb 45​ks na kuchyňu"
    result = extract.verify(
        {"orders": [{"deliveryDate": "31.07.2026", "recipientGroup": "", "items": [
            {"name": "chlieb", "quantity": 45, "unit": "ks", "sourceQuote": "chlieb 45 ks"},
        ]}]}, source)
    assert result["orders"][0]["items"][0]["quantity"] == 45
    assert result["unverified"] == []


def test_an_item_line_in_the_source_proves_the_item_even_without_a_usable_quote():
    """VERIFY sometimes glues a section header onto the item line, so the 'quote' does
    not exist contiguously (ČSB 2026-07-23: an order shipped with 1 of 9 items)."""
    result = extract.verify(
        {"orders": [{"deliveryDate": "31.07.2026", "recipientGroup": "", "items": [
            {"name": "vianočka", "quantity": 11, "unit": "ks",
             "sourceQuote": "Objednávka na zamestnancov 11 ks vianočka"},
        ]}]}, FREE_TEXT_MAIL)
    assert result["orders"][0]["items"][0]["name"] == "vianočka"
    assert result["unverified"] == []


def test_an_order_with_no_verifiable_item_is_not_kept():
    result = extract.verify(
        {"orders": [{"deliveryDate": "31.07.2026", "recipientGroup": "", "items": [
            {"name": "torta", "quantity": 5, "unit": "ks", "sourceQuote": "5x torta"},
        ]}]}, FREE_TEXT_MAIL)
    assert result["orders"] == []
    assert len(result["unverified"]) == 1


# --- 3) sanity guards ----------------------------------------------------

def test_a_price_list_read_as_an_order_is_refused():
    """A template read as an order shows up as many items all with quantity 1."""
    items = [{"name": f"produkt {i}", "quantity": 1, "unit": "ks"} for i in range(12)]
    guard = extract.sanity({"orders": [{"deliveryDate": "", "recipientGroup": "",
                                       "items": items}]})
    assert guard is not None and "cenník" in guard.lower()


def test_a_genuine_small_order_of_ones_is_not_refused():
    items = [{"name": f"produkt {i}", "quantity": 1, "unit": "ks"} for i in range(3)]
    assert extract.sanity({"orders": [{"deliveryDate": "", "recipientGroup": "",
                                       "items": items}]}) is None


# --- the LLM client ------------------------------------------------------

def test_the_cache_serves_a_repeated_call_without_touching_the_network(tmp_path):
    """The deterministic evaluation tier (#66) depends on this: same prompt + same input
    must never hit OpenAI twice."""
    calls = []

    def fake_transport(payload, api_key, timeout):
        calls.append(payload)
        return {"orders": [{"deliveryDate": "31.07.2026", "items": []}]}

    client = llm.Client(api_key="k", cache_dir=str(tmp_path), transport=fake_transport)
    first = client.json_call("system prompt", "user input", schema={"type": "object"})
    second = client.json_call("system prompt", "user input", schema={"type": "object"})
    assert first == second
    assert len(calls) == 1


def test_a_changed_prompt_is_a_different_cache_entry(tmp_path):
    calls = []
    client = llm.Client(api_key="k", cache_dir=str(tmp_path),
                        transport=lambda p, a, t: calls.append(p) or {"orders": []})
    client.json_call("prompt A", "input", schema={"type": "object"})
    client.json_call("prompt B", "input", schema={"type": "object"})
    assert len(calls) == 2


def test_the_prompt_hash_is_recorded_so_a_run_says_which_prompt_produced_it(tmp_path):
    client = llm.Client(api_key="k", cache_dir=str(tmp_path),
                        transport=lambda p, a, t: {"orders": []})
    client.json_call("system prompt", "user input", schema={"type": "object"})
    assert len(client.last_prompt_hash) == 12


def test_offline_mode_refuses_a_cache_miss_instead_of_calling_out(tmp_path):
    """CI runs offline: a missing cache entry must fail loudly, never silently spend
    money or hang on a network call."""
    client = llm.Client(api_key="", cache_dir=str(tmp_path), offline=True,
                        transport=lambda p, a, t: {"orders": []})
    try:
        client.json_call("system", "input", schema={"type": "object"})
    except llm.CacheMiss as e:
        assert "offline" in str(e).lower()
    else:
        raise AssertionError("a cache miss in offline mode must raise")


def test_the_model_and_effort_come_from_the_config(tmp_path):
    seen = {}
    client = llm.Client(api_key="k", cache_dir=str(tmp_path), model="gpt-5.4",
                        reasoning_effort="high",
                        transport=lambda p, a, t: seen.update(p) or {"orders": []})
    client.json_call("system", "input", schema={"type": "object"})
    assert seen["model"] == "gpt-5.4"
    assert seen["reasoning"]["effort"] == "high"
    assert json.dumps(seen["text"])   # a structured-output request, not free text
