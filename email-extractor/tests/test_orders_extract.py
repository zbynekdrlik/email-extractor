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


# --- 2b) the delivery DATE is citation-checked too (#163) -----------------
#
# Real incident: msg id 5679, subject "RE: catering 25.7. SL", arrived 2026-08-03. The
# body is a delivery-note follow-up quoting an order sent 2026-07-22 for "sobotu 25.7." —
# over a month in the past by the time the reply arrived. The model, applying the prompt's
# own "a delivery date can never be in the past" rule with nothing else to go on, invented
# 08.08.2026 (simply the next Saturday after "today") — a string that occurs NOWHERE in the
# source text. That fabricated date must never be treated as a real delivery date.

STALE_QUOTE_MAIL = (
    "Subject: RE: catering 25.7. SL\n"
    "From: ondus@example.com\n"
    "Body: uz ano, isla to prave kolegyna nahrat na dodak.\n\n"
    "> Dna 22.7.2026 napisal:\n"
    "> Dobry den, prosim objednat nasledovne na sobotu 25.7.:\n"
    "> 10x rozok 50g\n"
)


class _FakeStaleDateClient:
    """Mimics the real model's bug: returns a deliveryDate the source text never wrote."""
    last_prompt_hash = "abc123abc123"

    def json_call(self, system, user, schema, name="result"):
        return {"orders": [{"deliveryDate": "08.08.2026", "recipientGroup": "", "items": [
            {"name": "rozok 50g", "quantity": 10, "unit": "ks",
             "sourceQuote": "10x rozok 50g"},
        ]}]}


def test_a_stale_quoted_order_re_dated_by_the_model_is_not_shipped():
    result = extract.run(_FakeStaleDateClient(),
                         {"combined_text": STALE_QUOTE_MAIL, "today": "2026-08-03",
                          "subject": "RE: catering 25.7. SL"})
    assert result["orders"] == []
    # the invented date may still be NAMED (so a human can see what was rejected and why —
    # #163 review finding), but never ASSERTED as a real delivery date the way the live
    # incident's "objednávka je na 08.08.2026" did.
    assert "objednávka je na 08.08" not in json.dumps(result)
    assert "nenašiel" in result["notes"]


def test_date_grounded_accepts_an_explicit_day_that_is_actually_written():
    assert extract.date_grounded("25.07.2026", "prosim objednat na 25.7. dakujem")


def test_date_grounded_rejects_an_invented_day_absent_from_the_source():
    assert not extract.date_grounded("08.08.2026", "objednavka na sobotu 25.7. prosim")


def test_date_grounded_allows_a_relative_date_with_no_explicit_day_anywhere():
    """'na zajtra' names no day.month at all — nothing in the text to hold the computed
    date accountable against, so the ordinary relative-date order still ships."""
    assert extract.date_grounded("31.07.2026", FREE_TEXT_MAIL)


def test_a_weekly_range_broken_down_by_weekday_still_grounds_every_day():
    """'od 06.07. - 11.07.' with a per-weekday breakdown legitimately derives every day in
    between, even though the individual weekdays' digits are never repeated (#163 corpus
    regression: kcrealpoprad-2026-07-02-f2e65e / kcrealpoprad-2026-07-30-df487f)."""
    text = "Subject: Objednávka od 06.07. - 11.07. pre PNO Poprad\n\nBody: Pondelok:\nUtorok:\n"
    for d in ("06.07.2026", "07.07.2026", "08.07.2026", "09.07.2026", "10.07.2026",
             "11.07.2026"):
        assert extract.date_grounded(d, text), d
    assert not extract.date_grounded("15.07.2026", text)   # outside the written range


def test_date_grounded_accepts_an_announced_day_with_no_trailing_dot():
    """Real CÉDER incident wording (#190, messages.id=6091): customers routinely write
    'na 10.8 poprosím' with NO trailing dot after the month digits — this must ground the
    delivery date exactly like the dotted form ('na 10.8.') already does.

    A second, DOTTED date ("5.7.2026") is included so the text names an explicit day —
    without it, `date_grounded`'s own "nothing written at all -> accept" default would make
    this pass vacuously, whether or not the no-dot form is actually recognized.
    """
    text = "Objednávka 5.7.2026\n\nDobrý deň, na 10.8 poprosím: Rožok 70g : 5 x"
    assert extract.date_grounded("10.08.2026", text)


def test_a_weight_after_na_is_not_read_as_a_written_date():
    """The '#187 quoted-text guard (a unit word right after "na D.M" is a weight, not a
    date) extended to the general "was this day written anywhere" scan (#190): 'na 3.5 kg'
    must not let an invented 03.05 delivery date pass as grounded, even though 3/5 are both
    individually valid calendar values."""
    text = "Objednávka na 5.7. a chlieb na 3.5 kg poprosím."
    assert not extract.date_grounded("03.05.2026", text)


TABLE_WITH_A_STALE_SUBJECT_DATE_MAIL = (
    "Subject: Objednávka 20.07.\n\nBody: dobrý deň\n\nAttachments:\n=====\n"
    "Č.mat.dodavat.,EAN kus,Název,Množství\n"
    "12345,8588001800013,Rožok štandart 50g,120\n"
    "=====\n"
)


def test_a_table_parsed_order_is_also_dropped_when_its_header_date_is_ungrounded():
    """The table branch takes ITEMS from the parsed grid but the header fields (including
    deliveryDate) still come from the model — so an invented date must be caught there too,
    not just on the free-text path."""
    class FakeTableStaleDateClient:
        last_prompt_hash = "abc123abc123"

        def json_call(self, system, user, schema, name="result"):
            return {"orders": [{"deliveryDate": "08.08.2026", "recipientGroup": "",
                                "items": []}]}

    result = extract.run(FakeTableStaleDateClient(),
                         {"combined_text": TABLE_WITH_A_STALE_SUBJECT_DATE_MAIL,
                          "today": "2026-08-03", "subject": "Objednávka 20.07."})
    assert result["source"] == "table"
    assert result["orders"] == []
    assert "objednávka je na 08.08" not in json.dumps(result)
    assert "nenašiel" in result["notes"]


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


# --- defects found by the first live run against real emails --------------

def test_the_prompt_input_carries_todays_date():
    """Without it the model dates "zajtra"/"pondelok" from its own training cutoff — a
    silently wrong delivery date. Found on the first live run of this stage."""
    sent = {}

    class FakeClient:
        last_prompt_hash = "abc123abc123"

        def json_call(self, system, user, schema, name="result"):
            sent["user"] = user
            return {"orders": []}

    extract.run(FakeClient(), {"combined_text": "na zajtra 10x rožok", "today": "2026-07-30"})
    assert "30.07.2026" in sent["user"]
    assert "štvrtok" in sent["user"], "the weekday matters for 'na pondelok'"


def test_a_price_list_with_no_quantities_filled_in_is_not_an_order():
    """Real email (Náš dvor PNO RK, 2026-07-30): the wholesale list is attached for
    reference and the actual order is one line in the BODY. If the parser returned the
    list's products, it would override the body and ship the entire catalog."""
    mail = (
        "Body: Dobrý deň, poprosím doložiť 3x slimák kakaový 90 g.\n\nAttachments:\n"
        "===== VO Pekarová žena.xls =====\n"
        "Názov  tovaru\t%DPH\tVO bez DPH\tVO  s DPH\tTrvanlivosť v dňoch\n"
        "Bageta kvásková 250g\t19\t1\t1.19\t2\n"
        "Rožok 70g\t5\t0.38\t0.399\t20\n"
    )
    assert extract.parse_table(mail) is None


# --- the subject and the body must agree about the day (#81.5) -------------

def test_a_single_day_in_the_subject_that_contradicts_the_body_is_refused():
    """Real mail: subject "Objednávka 29.6.2026", body "na 28.6.2026" — a Sunday. Guessing
    one of them either delivers on the wrong day or misses the right one, so it goes to a
    human. n8n produced nothing at all here and the customer got no bread."""
    problem = extract.date_conflict(
        subject="Objednávka 29.6.2026",
        dates=["28.06.2026"])
    assert problem and "29.6." in problem and "28.06.2026" in problem


def test_a_date_range_in_the_subject_is_not_a_contradiction():
    """"Objednávka od 06.07. - 11.07." is a range the body's days fall inside."""
    assert not extract.date_conflict("Objednávka od 06.07. - 11.07. pre PNO Poprad",
                                     ["06.07.2026", "08.07.2026", "11.07.2026"])


def test_a_subject_day_that_matches_one_of_the_ordered_days_is_fine():
    assert not extract.date_conflict("Objednávka 29.6.2026", ["29.06.2026"])
    assert not extract.date_conflict("Objednávka 29.6.", ["29.06.2026"])


def test_a_subject_without_a_day_never_conflicts():
    assert not extract.date_conflict("objednávka pečiva", ["30.06.2026"])
    assert not extract.date_conflict("Objednávky _ júl", ["01.07.2026", "06.07.2026"])


def test_several_ordered_days_with_one_subject_day_among_them_is_fine():
    """A multi-day order whose subject names the first day is normal."""
    assert not extract.date_conflict("Objednávka 23.7.", ["23.07.2026", "24.07.2026"])


def test_two_orders_where_the_body_names_only_one_day_go_to_a_human():
    """The corpus caught this flipping between live runs (2026-08-01, kuchyna AGEL Levoča).

    Subject says 29.6., the body says 28.6. — one order, two contradictory days. Sometimes
    the model refused (correct); sometimes it "solved" the contradiction by emitting BOTH
    days as separate orders, one item each. The subject rule alone cannot see that: 29.6. IS
    among the ordered days, so it returned no conflict and two invented orders shipped.

    The body is the evidence. When it names exactly ONE delivery day, a second ordered day
    was not read anywhere — same principle as an item needing its `sourceQuote`.
    """
    problem = extract.date_conflict(
        subject="Objednávka 29.6.2026",
        dates=["28.06.2026", "29.06.2026"],
        body="Dobrý deň, prosíme o dodanie na 28.6.2026. Ďakujeme.")
    assert problem and "28.6." in problem


def test_two_orders_where_the_body_names_one_announced_day_with_no_dot_go_to_a_human():
    """Same gap as #190's date_grounded fix, through date_conflict's body-day check: the
    body written the CÉDER way ('na 28.6 poprosím', no trailing dot) must still be seen as
    the one written day, not silently invisible to this check either."""
    problem = extract.date_conflict(
        subject="Objednávka 29.6.2026",
        dates=["28.06.2026", "29.06.2026"],
        body="Dobrý deň, prosíme o dodanie na 28.6 poprosím. Ďakujeme.")
    assert problem and "28.6." in problem


def test_a_body_naming_both_ordered_days_is_not_a_contradiction():
    """The normal multi-day order: the body itself lists every day it asks for."""
    assert not extract.date_conflict(
        "Objednávka 23.7.", ["23.07.2026", "24.07.2026"],
        body="Na 23.7. 10 ks rožkov, na 24.7. 12 ks rožkov.")


def test_a_body_with_no_explicit_day_still_relies_on_the_subject_rule():
    """Relative dates ("na pondelok") legitimately produce a day the body never spells out."""
    assert not extract.date_conflict("objednávka pečiva", ["30.06.2026"],
                                     body="Prosím na pondelok 10 ks rožkov.")


def test_the_subject_line_inside_the_stored_text_does_not_count_as_the_body():
    """`combined_text` begins with "Subject: … / From: … / Body: …" — the subject is IN it.

    Counting it as body text made the AGEL Levoča guard inert: the body says one day, the
    subject says another, so the naive scan saw two days and the check stood down — exactly
    the contradiction it exists to catch.
    """
    problem = extract.date_conflict(
        subject="Objednávka 29.6.2026",
        dates=["28.06.2026", "29.06.2026"],
        body=("Subject: Objednávka 29.6.2026\nFrom: kuchyna@nle.agel.sk\n"
              "Body: Dobrý deň, prosíme o dodanie na 28.6.2026. Ďakujeme."))
    assert problem and "28.6." in problem


def test_a_weekly_order_listing_weekdays_is_not_a_contradiction():
    """PNO Poprad's weekly mail names the week ONCE and then lists days by NAME.

    The body-day check must stand down there: the extra delivery days are derived from
    "Pondelok:" / "Utorok:" …, which is reading the email, not inventing days. Blocking
    these sent two real weekly orders to review (offline corpus, 2026-08-01).
    """
    body = ("Body: Dobrý deň, posielam objednávku na týždeň od 06.07.\n\n"
            "Pondelok:\n30 x Rožok 70g\n\nUtorok:\n20 x Rožok 70g\n\n"
            "Streda:\n25 x Rožok 70g")
    assert not extract.date_conflict(
        "Objednávka na týždeň", ["06.07.2026", "07.07.2026", "08.07.2026"], body=body)


def test_a_date_range_in_the_body_is_not_a_contradiction_either():
    body = "Body: objednávka od 06.07. - 11.07. pre PNO Poprad, denne 30 x Rožok 70g"
    assert not extract.date_conflict(
        "objednávka", ["06.07.2026", "08.07.2026", "11.07.2026"], body=body)


# --- an email whose WHOLE body is quoted is still an order (#155) ------------

# The real 2026-08-03 mail from CÉDER (msg 5596), shortened: the customer's client quoted
# every single line, so the prompt's "ignore lines starting with >" rule emptied the email
# and a four-day order was reported as "AI nenašla v e-maile žiadnu objednávku".
CEDER_FULLY_QUOTED = (
    "Subject: objednávka 5.-8.8.\n\nFrom: info@resortceder.sk\n\n"
    "Body: > Dobrý deň,\n> \n> na 5.8. streda poprosím:\n> \n> Rožok 70g : 30 x\n> \n"
    "> Chlieb multicereálny : 2 x\n> \n> Vianočka - 1 x\n> \n"
    "> na 6.8. štvrtok poprosím:\n> \n> Rožok 70g : 30 x\n> \n> Vianočka - 1 x\n"
)


def test_a_fully_quoted_email_body_is_unquoted_so_the_order_is_read():
    out = extract.unquote_fully_quoted(CEDER_FULLY_QUOTED)
    assert not [ln for ln in out.splitlines() if ln.lstrip().startswith(">")]
    assert "na 5.8. streda poprosím:" in out
    assert "na 6.8. štvrtok poprosím:" in out
    assert "Rožok 70g : 30 x" in out
    # the envelope the add-on itself adds must survive intact
    assert "From: info@resortceder.sk" in out
    assert "Subject: objednávka 5.-8.8." in out


def test_a_reply_that_adds_fresh_lines_keeps_its_quote_markers():
    """Only a body with NO unquoted text of its own is unquoted.

    A normal reply — fresh order on top, last week's thread below — must keep the markers,
    otherwise the old thread's items would be read as part of today's order.
    """
    mail = ("Body: Dobrý deň, na 12.8. poprosím:\n\nRožok 70g : 10 x\n\n"
            "> na 5.8. streda poprosím:\n> Rožok 70g : 30 x\n")
    assert extract.unquote_fully_quoted(mail) == mail


def test_nested_quoting_is_unquoted_too():
    mail = "Body: >> Dobrý deň,\n>> na 5.8. poprosím:\n>> Rožok 70g : 30 x\n"
    out = extract.unquote_fully_quoted(mail)
    assert "na 5.8. poprosím:" in out
    assert not [ln for ln in out.splitlines() if ln.lstrip().startswith(">")]


def test_a_normal_unquoted_email_is_returned_unchanged():
    assert extract.unquote_fully_quoted(KOSIK_MAIL) == KOSIK_MAIL


def test_a_quoted_body_whose_days_have_all_passed_is_left_alone():
    """The guard on the fix: an empty reply that quotes an OLD thread must not become an order.

    A past delivery date is not refused downstream — `pipeline` only skips the hold and
    ships — so last week's quoted order would go to ORION a second time. Such a message
    keeps its old outcome instead: no order found, enter by hand.
    """
    stale = ("Subject: Re: objednávka\n\nFrom: info@resortceder.sk\n\n"
             "Body: > Dobrý deň,\n> \n> na 5.8. streda poprosím:\n> Rožok 70g : 30 x\n")
    assert extract.unquote_fully_quoted(stale, today="20.08.2026") == stale
    # …while the same mail read on the day it was sent IS the order
    assert ">" not in extract.unquote_fully_quoted(stale, today="03.08.2026")


def test_a_quoted_order_with_no_written_day_is_still_read():
    """"na pondelok" names no day to compare, so the guard must not block it."""
    mail = "Body: > Dobrý deň,\n> na pondelok poprosím:\n> Rožok 70g : 30 x\n"
    assert "na pondelok poprosím:" in extract.unquote_fully_quoted(mail, today="03.08.2026")


def test_the_extraction_source_is_unquoted_so_citations_can_be_verified():
    """`run()` must feed the UNQUOTED text to the model AND to the citation check.

    Both read the same `source`, so an item quoted by the model as `Rožok 70g : 30 x`
    (without the `>`) has to be findable in it — otherwise a real item would be dropped as
    unverifiable, the AGEL zero-width failure all over again.
    """
    source = extract.unquote_fully_quoted(extract.clean_text(CEDER_FULLY_QUOTED))
    assert extract.quote_in_source("Rožok 70g : 30 x", source)
    assert extract.quote_in_source("Chlieb multicereálny : 2 x", source)


# --- #187: a genuine second order hiding in quoted text must be surfaced, not dropped --

def test_a_quoted_second_order_with_a_still_ahead_date_is_surfaced_not_dropped():
    """A mixed body — fresh order on top, a genuine SECOND order quoted below for a later
    still-ahead day. Synthetic fixture, but reproducing the EXACT wording shape of the
    real 2026-08-06 incident mail (messages.id=6091): 'na 10.8 poprosím' / '>>-quoted na
    11.8 poprosím' — NO trailing dot after the day.month, which real customers routinely
    omit. The model (mimicked here) correctly ignores the quoted '>' lines per the prompt
    and returns only the fresh order — `run()` must still notice the quoted day was never
    turned into an order and surface it in `notes`, so a human decides whether it is a
    real second order or a stale quoted template."""
    mail = ("Subject: objednávka\n\nFrom: synthetic@example.test\n\n"
            "Body: Dobrý deň,\n\nna 10.8 poprosím:\n\n"
            "Rožok 70g : 5 x\n\n"
            ">> Dobrý deň ,\n>> \n>> na 11.8 poprosím:\n>> \n>> Rožok 70g : 5 x\n"
            ">> Ďakujeme Testovacia Zákazníčka\n")

    class FakeClient:
        last_prompt_hash = "abc123abc123"

        def json_call(self, system, user, schema, name="result"):
            return {"orders": [{"deliveryDate": "10.08.2026", "recipientGroup": "",
                                "items": [{"name": "Rožok 70g", "quantity": 5, "unit": "ks",
                                           "sourceQuote": "Rožok 70g : 5 x"}]}]}

    result = extract.run(FakeClient(), {"combined_text": mail, "today": "2026-08-06",
                                       "subject": "objednávka"})
    assert [o["deliveryDate"] for o in result["orders"]] == ["10.08.2026"]
    assert "11.8" in result["notes"]


def test_a_quoted_date_with_a_trailing_dot_is_also_recognized():
    """Both wordings ('na 11.8' and 'na 11.8.') must be caught — some customers do write
    the trailing dot, only the incident mail itself happened not to."""
    quoted = "Body: Dobrý deň.\n> na 11.8. poprosím: Rožok 70g : 5 x\n"
    assert extract.quoted_future_dates_uncovered(quoted, "2026-08-06", set()) == [(11, 8)]


def test_a_quoted_date_already_covered_by_a_produced_order_is_not_flagged():
    """The same day named both fresh and quoted (e.g. quoted just for context, not a real
    second order) must not be reported as dropped — an order for it already exists."""
    quoted = ("Body: Dobrý deň, na 10.8 poprosím:\nRožok 70g : 5 x\n"
              "> na 10.8 poprosím: Rožok 70g : 5 x\n")
    assert extract.quoted_future_dates_uncovered(quoted, "2026-08-06", {(10, 8)}) == []


def test_a_quoted_stale_date_is_not_flagged():
    """A quoted day already in the past must not be flagged — same 'still ahead' guard
    `unquote_fully_quoted`'s own stale-thread check already applies."""
    quoted = "Body: Dobrý deň.\n> na 1.8. poprosím: Rožok 70g : 5 x\n"
    assert extract.quoted_future_dates_uncovered(quoted, "2026-08-06", set()) == []


def test_a_quoted_order_with_no_written_day_names_nothing_to_surface():
    """'na pondelok' names no day.month at all — nothing to compare, so nothing to flag."""
    quoted = "Body: Dobrý deň.\n> na pondelok poprosím: Rožok 70g : 5 x\n"
    assert extract.quoted_future_dates_uncovered(quoted, "2026-08-06", set()) == []


def test_a_weight_after_na_is_not_read_as_a_date():
    """Review finding: 'na' precedes a WEIGHT too ('chlieb na 3.5 kg'), not only a date —
    day=3/month=5 are both individually plausible, so the calendar-range check alone
    would not catch this; the immediately-following unit word must."""
    quoted = "Body: Dobrý deň.\n> chlieb na 3.5 kg poprosím: Rožok 70g : 5 x\n"
    assert extract.quoted_future_dates_uncovered(quoted, "2026-08-06", set()) == []


def test_a_price_after_na_is_not_read_as_a_date():
    """'na 1.50 eur' — month=50 is not a real month; the calendar-range check alone
    catches this one."""
    quoted = "Body: Dobrý deň.\n> cena na 1.50 eur poprosím: Rožok 70g : 5 x\n"
    assert extract.quoted_future_dates_uncovered(quoted, "2026-08-06", set()) == []
