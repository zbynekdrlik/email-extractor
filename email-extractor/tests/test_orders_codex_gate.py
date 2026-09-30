"""#479 — an ORDER file never carries a card code CODEX has no stock card for.

The #467 CODEX stock-card list guarded only delivery notes. During the incident the warehouse
renumbered CODEX card 27 while our orders catalog still used the old code, so CODEX refused the
ORDER lines („nebralo do objednávky") and nothing on our side noticed. Now, LIVE only (shadow /
the e2e-orders corpus never loads the list):

  * AI orders: a matched line whose code CODEX lacks becomes a cardless `codex_missing` line —
    the EXISTING item question (only cards CODEX has) + `hold.place` hold the order; the answer
    releases it with the right card. A wording the sklad TAUGHT onto the dead card (the
    incident shape) is asked too — `teach.ask` must not stay silent for it.
  * the release re-checks (a code can go dead while the order waits), and `_ship_one` re-checks
    right before the claim — the deadline sweep ships without a dead line, never with it;
  * static orders: an unknown code sends the message to the AI pipeline (the #133 route that
    holds + asks), never a static ORDER file with that code;
  * a stale / never-pushed list FAILS OPEN (ships as before, with a warning);
  * the orders item answer refuses a card whose code CODEX lacks (409), like the #467 dl_item pick.

Real Postgres, scripted model answers only. All codes/names SYNTHETIC (public repo).
"""
import logging
import os
import re
from datetime import UTC, datetime, timedelta

import pytest
from psycopg.types.json import Json

from app.config import Config
from app.httpapi import create_app, sklad_key
from app.orders import codex_cards, edi, hold, memory, pipeline, snapshot, static_worker, teach

PG_DSN = os.environ.get("PG_TEST_DSN")

CUST = "2000000000001"
G_ROLL = "9990000000109"
G_DEAD = "3698"                  # our card — CODEX has no stock card with this EAN kód
G_VIA = "9990000000116"          # the card CODEX really has for the vianočka
G_TOR = "9990000000123"
G_TOR2 = "9990000000130"

CATALOG_CSV = (
    "GTIN,Sklad,Názov,doplnok\n"
    f"{G_ROLL},1,Rožok štandart 50g,\n"
    f"{G_DEAD},1,Vianočka 400g,\n"
    f"{G_VIA},1,Vianočka maslová 400g,\n"
    f"{G_TOR},1,Torta čokoládová,\n"
    f"{G_TOR2},1,Torta vanilková,\n"
)
CUSTOMER_CSV = (
    "Názov organizácie,EAN kód EDI,Obec,Ulica,Meno pre fakturáciu,Číslo mobilu,E-mail\n"
    f"Pekáreň Testovacia s.r.o.,{CUST},Martin,Košútka 1,,,sklad@pekaren.sk\n"
)
QTY = {"rožok 50g": 120, "vianočka 400g": 7, "vianočka maslová 400g": 4, "torta": 5,
       "vianočka": 3}


def _mail(*names, mid="m1"):
    text = "na 04.08.2026 prosím " + ", ".join(f"{QTY[n]}x {n}" for n in names)
    return {"message_id": mid, "subject": "Objednávka", "from_addr": "sklad@pekaren.sk",
            "from_name": "Sklad", "combined_text": text, "today": "2026-07-30"}


class NameClient:
    """orders (extraction) → customer → one product answer PER WORDING (keyed by the item's name in the
    prompt, so a line the no-model ladder settles for free never shifts the others)."""

    last_prompt_hash = "testprompt12"

    def __init__(self, names, picks):
        self.names, self.picks, self.asked = list(names), dict(picks), []

    def json_call(self, system, user, schema, name="result"):
        self.asked.append(name)
        if name == "orders":
            items = [{"name": n, "quantity": QTY[n], "unit": "ks",
                      "sourceQuote": f"{QTY[n]}x {n}"} for n in self.names]
            return {"senderName": "Sklad", "senderEmail": "sklad@pekaren.sk",
                    "companyName": "Pekáreň Testovacia s.r.o.", "isChangeRequest": False,
                    "notes": "", "orders": [{"orderNumber": "", "deliveryDate": "04.08.2026",
                                             "recipientGroup": "", "items": items}]}
        if name == "customer":
            return {"ean_edi": CUST, "confidence": 0.95}
        if name == "product":
            item = re.search(r"POLOŽKA Z OBJEDNÁVKY:\n„(.+?)“", user).group(1)
            gtin, conf = self.picks[item]
            return {"gtin": gtin or "NO_MATCH", "confidence": conf,
                    "matchedCatalogName": "", "reason": "scripted"}
        raise AssertionError(f"unscripted model call {name!r}")


class Recorder:
    def __init__(self):
        self.uploads, self.posts = [], []

    def upload(self, cfg, name, content):
        self.uploads.append((name, content))
        return True

    def post(self, cfg, html, transport=None):
        self.posts.append(html)
        return {"id": 1}


def _lin_codes(content: str) -> list[str]:
    return [line[12:37].strip() for line in content.splitlines() if line.startswith("LIN")]


def _cfg(**kw):
    base = dict(pg_dsn=PG_DSN, data_dir="/tmp", orders_shadow=False, odoo_url="",
                odoo_api_key="", orders_channel_id=0)
    base.update(kw)
    return Config(**base)


def _codex(pg, codes, hours_old=1, force=False):
    cards = [{"code": c, "card_code": str(100 + i), "stredisko": 1, "sklad": 1,
              "name": f"CODEX karta {c}", "inactive": False, "changed_at": None}
             for i, c in enumerate(codes)]
    codex_cards.replace_cards(pg, cards, force=force,
                              source_as_of=datetime.now(UTC) - timedelta(hours=hours_old))


ALL_BUT_DEAD = (G_ROLL, G_VIA, G_TOR, G_TOR2)


@pytest.fixture
def env(pg):
    sid = snapshot.import_snapshot(pg, CATALOG_CSV, CUSTOMER_CSV)
    pg.execute("INSERT INTO messages (message_id, category) VALUES ('m1', 'ai_orders')")
    return sid


def _run(pg, sid, names, picks, rec, **cfg):
    return pipeline.run(pg, _cfg(**cfg), _mail(*names), sid, client=NameClient(names, picks),
                        upload=rec.upload, post=rec.post)


ROLL_VIA = ("rožok 50g", "vianočka 400g")
PICKS = {"rožok 50g": (G_ROLL, 0.95), "vianočka 400g": (G_DEAD, 0.95),
         "vianočka maslová 400g": (G_VIA, 0.95), "torta": (None, 0.1),
         "vianočka": (None, 0.1)}


# --- AI orders: a dead code HOLDS the order with a question, never an ORDER file ----------

def test_a_line_whose_code_codex_lacks_holds_the_order_with_a_question(pg, env):
    _codex(pg, ALL_BUT_DEAD)
    rec = Recorder()
    result = _run(pg, env, ROLL_VIA, PICKS, rec)

    assert result["status"] == "held"
    assert rec.uploads == [], "never an ORDER file CODEX rejects"
    assert pg.execute("SELECT count(*) FROM edi_sent").fetchone()[0] == 0
    assert pg.execute("SELECT count(*) FROM held_orders WHERE status='held'").fetchone()[0] == 1
    line = next(i for i in result["items"] if i["name"] == "vianočka 400g")
    assert line["rule"] == "codex_missing" and line["gtin"] is None
    qs = teach.open_questions(pg)
    assert [q["wording"] for q in qs] == ["vianočka 400g"]
    assert G_DEAD in qs[0]["reason"] and "CODEX" in qs[0]["reason"]
    offered = [str(c["gtin"]) for c in qs[0]["candidates"]]
    assert G_DEAD not in offered, "the question offers only cards CODEX has"
    assert G_VIA in offered
    assert all(codex_cards.load(pg).has(g) for g in offered)


def test_answering_the_codex_question_ships_the_order_with_the_codex_card(pg, env):
    _codex(pg, ALL_BUT_DEAD)
    rec = Recorder()
    _run(pg, env, ROLL_VIA, PICKS, rec)
    qid = teach.open_questions(pg)[0]["id"]

    teach.answer(pg, qid, G_VIA, "Vianočka maslová 400g", by="sklad")
    released = hold.release_for_question(pg, _cfg(), qid, upload=rec.upload, post=rec.post)

    assert [r["status"] for r in released] == ["ok"]
    assert len(rec.uploads) == 1
    assert sorted(_lin_codes(rec.uploads[0][1])) == sorted([G_ROLL, G_VIA])


def test_a_wording_taught_onto_the_dead_card_is_still_asked_the_incident_shape(pg, env):
    """Card 27 / code 3698: the sklad had TAUGHT the wording onto the card whose code CODEX then
    dropped. `teach.ask` stays silent for a human-taught wording — here it must ask, or the
    order ships without the line (or with the dead code)."""
    memory.remember(pg, CUST, "vianočka 400g", G_DEAD, "Vianočka 400g", "2026-07-20",
                    source="human")
    _codex(pg, ALL_BUT_DEAD)
    rec = Recorder()
    result = _run(pg, env, ROLL_VIA, PICKS, rec)

    assert result["status"] == "held" and rec.uploads == []
    qs = teach.open_questions(pg)
    assert [q["wording"] for q in qs] == ["vianočka 400g"]

    teach.answer(pg, qs[0]["id"], G_VIA, "Vianočka maslová 400g", by="sklad")
    hold.release_for_question(pg, _cfg(), qs[0]["id"], upload=rec.upload, post=rec.post)
    assert len(rec.uploads) == 1
    assert G_DEAD not in _lin_codes(rec.uploads[0][1])
    assert G_VIA in _lin_codes(rec.uploads[0][1]), "the newest human answer wins from now on"


def test_known_codes_ship_exactly_as_without_the_gate(pg, env):
    _codex(pg, ALL_BUT_DEAD + (G_DEAD,))
    shadow = pipeline.run(pg, _cfg(orders_shadow=True), _mail(*ROLL_VIA), env,
                          client=NameClient(ROLL_VIA, PICKS), upload=None, post=None)
    rec = Recorder()
    result = _run(pg, env, ROLL_VIA, PICKS, rec)

    assert result["status"] == "ok"
    assert len(rec.uploads) == 1
    assert sorted(_lin_codes(rec.uploads[0][1])) == sorted([G_ROLL, G_DEAD])
    assert edi.content_hash(rec.uploads[0][1]) == edi.content_hash(shadow["edi_preview"])
    assert teach.open_questions(pg) == []


def test_a_stale_codex_list_fails_open_ships_and_warns(pg, env, caplog):
    _codex(pg, ALL_BUT_DEAD, hours_old=codex_cards.STALE_HOURS + 5)
    rec = Recorder()
    with caplog.at_level(logging.WARNING, logger="orders.codex_cards"):
        result = _run(pg, env, ROLL_VIA, PICKS, rec)

    assert result["status"] == "ok"
    assert len(rec.uploads) == 1 and G_DEAD in _lin_codes(rec.uploads[0][1])
    assert any("stale" in r.getMessage() for r in caplog.records)


def test_shadow_never_consults_the_codex_list(pg, env):
    """The e2e-orders corpus runs forced-shadow — its verdicts must stay byte-identical."""
    _codex(pg, ALL_BUT_DEAD)
    result = pipeline.run(pg, _cfg(orders_shadow=True), _mail(*ROLL_VIA), env,
                          client=NameClient(ROLL_VIA, PICKS), upload=None, post=None)
    assert result["status"] == "ok"
    assert G_DEAD in _lin_codes(result["edi_preview"])
    assert teach.open_questions(pg) == []


# --- a code that goes dead WHILE the order waits ------------------------------------------

THREE = ("rožok 50g", "vianočka maslová 400g", "torta")


def _held_on_torta(pg, env, rec):
    _codex(pg, ALL_BUT_DEAD)
    result = _run(pg, env, THREE, PICKS, rec)
    assert result["status"] == "held"
    qs = teach.open_questions(pg)
    assert [q["wording"] for q in qs] == ["torta"]
    # CODEX renumbers the roll card while the order waits on the torta question
    _codex(pg, (G_VIA, G_TOR, G_TOR2), force=True)
    return qs[0]["id"]


def test_the_release_re_holds_an_order_whose_code_went_dead_while_it_waited(pg, env):
    rec = Recorder()
    qid = _held_on_torta(pg, env, rec)
    teach.answer(pg, qid, G_TOR, "Torta čokoládová", by="sklad")
    released = hold.release_for_question(pg, _cfg(), qid, upload=rec.upload, post=rec.post)

    assert [r["status"] for r in released] == ["held"]
    assert rec.uploads == []
    qs = teach.open_questions(pg)
    assert [q["wording"] for q in qs] == ["rožok 50g"]
    assert G_ROLL in qs[0]["reason"]


def test_the_deadline_sweep_ships_without_a_code_that_went_dead_never_with_it(pg, env):
    rec = Recorder()
    _held_on_torta(pg, env, rec)
    released = hold.release_due(pg, _cfg(), upload=rec.upload, post=rec.post,
                                today="2026-08-05")

    assert [r["status"] for r in released] == ["partial"]
    assert len(rec.uploads) == 1
    codes = _lin_codes(rec.uploads[0][1])
    assert G_ROLL not in codes, "a dead code never reaches an ORDER file"
    assert codes == [G_VIA]


def test_the_deadline_sweep_asks_about_the_line_it_shipped_without(pg, env):
    """Review 🟡3: the ship-time net must not drop the dead line quietly — the order ships
    without it (the deadline allows no more waiting) AND a board question names it."""
    rec = Recorder()
    _held_on_torta(pg, env, rec)
    hold.release_due(pg, _cfg(), upload=rec.upload, post=rec.post, today="2026-08-05")

    qs = {q["wording"]: q for q in teach.open_questions(pg)}
    assert "rožok 50g" in qs, "the dropped line gets its own board question"
    assert G_ROLL in qs["rožok 50g"]["reason"]
    assert G_ROLL not in [str(c["gtin"]) for c in qs["rožok 50g"]["candidates"]]


def test_a_re_hold_asks_about_a_human_taught_line_whose_code_died_while_it_waited(pg, env):
    """Review 🟡4 (M2): the release's re-ask must bypass the human-taught pre-check too, or the
    line is 'unaskable' and the order sits held with no question until its deadline."""
    memory.remember(pg, CUST, "rožok 50g", G_ROLL, "Rožok štandart 50g", "2026-07-20",
                    source="human")
    rec = Recorder()
    qid = _held_on_torta(pg, env, rec)
    teach.answer(pg, qid, G_TOR, "Torta čokoládová", by="sklad")
    released = hold.release_for_question(pg, _cfg(), qid, upload=rec.upload, post=rec.post)

    assert [r["status"] for r in released] == ["held"] and rec.uploads == []
    assert [q["wording"] for q in teach.open_questions(pg)] == ["rožok 50g"]


def test_re_teaching_a_card_taught_earlier_the_same_day_wins_again(pg, env):
    """Review 🟡1: the wording was taught onto the vianočka card in the morning, then onto the
    card whose code CODEX dropped. Answering the codex question with the morning card must make
    it the answer again — a swallowed same-day re-teach re-held the order in a loop."""
    today = pg.execute("SELECT current_date").fetchone()[0]
    memory.remember(pg, CUST, "vianočka 400g", G_VIA, "Vianočka maslová 400g", today,
                    source="human")
    memory.remember(pg, CUST, "vianočka 400g", G_DEAD, "Vianočka 400g", today, source="human")
    _codex(pg, ALL_BUT_DEAD)
    rec = Recorder()
    assert _run(pg, env, ROLL_VIA, PICKS, rec)["status"] == "held"
    qid = teach.open_questions(pg)[0]["id"]

    teach.answer(pg, qid, G_VIA, "Vianočka maslová 400g", by="sklad")
    released = hold.release_for_question(pg, _cfg(), qid, upload=rec.upload, post=rec.post)
    assert [r["status"] for r in released] == ["ok"]
    assert sorted(_lin_codes(rec.uploads[0][1])) == sorted([G_ROLL, G_VIA])
    assert teach.open_questions(pg) == []


def test_a_human_answer_is_never_swallowed_by_a_same_day_ship_row(pg):
    """The memory half of review 🟡1: a same-day ship row of the same card used to swallow the
    human answer (ON CONFLICT DO NOTHING) — the answer never reached the taught rung."""
    today = pg.execute("SELECT current_date").fetchone()[0]
    assert memory.remember(pg, CUST, "chlieb", G_VIA, "Chlieb", today, source="ship")
    assert memory.remember(pg, CUST, "chlieb", G_VIA, "Chlieb", today, source="human")
    rec = memory.resolve(pg, CUST, "chlieb")
    assert rec is not None and rec.human and rec.gtin == G_VIA
    # a machine duplicate still changes nothing
    assert memory.remember(pg, CUST, "chlieb", G_VIA, "Chlieb", today, source="ship") is False


def test_a_codex_question_never_offers_an_unrelated_card_as_its_only_button(pg, env):
    """Review 🟡2: a codex_missing line has no engine proposal — only CODEX cards that clear the
    relevance floor are offered (here none: both vianočka cards are dead), never the top scorer
    of the unrelated rest shown like a proposal."""
    _codex(pg, (G_ROLL, G_TOR, G_TOR2))
    rec = Recorder()
    assert _run(pg, env, ROLL_VIA, PICKS, rec)["status"] == "held"
    qs = teach.open_questions(pg)
    assert [q["wording"] for q in qs] == ["vianočka 400g"]
    assert qs[0]["candidates"] == []


def test_a_codex_question_shows_at_most_six_buttons():
    """Review 2 🟡: the floor-only codex branch lost #160's cap — a generic wording („chlieb")
    over a big catalog got 24 buttons. Same cap as every other orders question."""
    from app.orders import card_guard, match
    catalog = [{"gtin": f"99900000001{i:02d}", "name": f"Chlieb pšeničný {i}00g",
                "alias": ""} for i in range(1, 13)]
    codex = codex_cards.CodexCards(names={c["gtin"]: (c["name"],) for c in catalog},
                                   as_of=datetime.now(UTC), synced_at=datetime.now(UTC),
                                   stale=False)
    line = match.Decision(item_name="chlieb pšeničný", gtin=None, card="", confidence=0.0,
                          rule=card_guard.CODEX_MISSING, note="")
    shown = card_guard.order_question_candidates("chlieb pšeničný", [], catalog, line, codex)
    assert 0 < len(shown) <= card_guard.QUESTION_BUTTONS == 6
    assert all(c["score"] >= match.PLAUSIBLE_CANDIDATE_SCORE for c in shown)


def test_a_human_answer_revives_its_own_soft_deleted_same_day_row(pg):
    """Review 2 🟡: the re-teach upsert must also revive a row the sklad soft-deleted (Naučené
    / Kôš) the same day — else the new answer is swallowed by the non-partial UNIQUE key (the
    DL #465 lesson)."""
    today = pg.execute("SELECT current_date").fetchone()[0]
    assert memory.remember(pg, CUST, "chlieb", G_VIA, "Chlieb", today, source="human")
    pg.execute("UPDATE item_memory SET deleted_at = now()")
    assert memory.resolve(pg, CUST, "chlieb") is None
    assert memory.remember(pg, CUST, "chlieb", G_VIA, "Chlieb nový", today, source="human")
    rec = memory.resolve(pg, CUST, "chlieb")
    assert rec is not None and rec.human and rec.gtin == G_VIA and rec.card == "Chlieb nový"


def test_the_net_question_is_announced_when_nothing_is_left_to_ship(pg, env):
    """Review 2 🔵: every shippable code died while the order waited → the deadline sweep ships
    nothing (review) — the Odoo summary must still announce the net's new questions (and link
    the board), not only the ok/partial path."""
    rec = Recorder()
    _held_on_torta(pg, env, rec)
    _codex(pg, (G_TOR, G_TOR2), force=True)
    released = hold.release_due(pg, _cfg(), upload=rec.upload, post=rec.post,
                                today="2026-08-05")
    assert [r["status"] for r in released] == ["review"] and rec.uploads == []
    assert {q["wording"] for q in teach.open_questions(pg)} >= {"rožok 50g",
                                                              "vianočka maslová 400g"}
    assert "&#10067; 2" in rec.posts[-1]


def test_any_item_question_offers_only_cards_codex_has(pg, env):
    """Review 🟡4 (M1): an ordinary unmatched line's candidates are filtered too — a dead card
    is never a button, whatever the question's reason."""
    _codex(pg, ALL_BUT_DEAD)
    rec = Recorder()
    assert _run(pg, env, ("rožok 50g", "vianočka"), PICKS, rec)["status"] == "held"
    qs = teach.open_questions(pg)
    assert [q["wording"] for q in qs] == ["vianočka"]
    offered = [str(c["gtin"]) for c in qs[0]["candidates"]]
    assert G_DEAD not in offered and G_VIA in offered


def test_a_confirmed_quantity_is_a_float_so_a_merge_and_a_re_hold_dump_never_crash(pg, env):
    """Review 🔵7e: the #360 confirmed quantity comes back from NUMERIC as a Decimal — a
    decision carrying it crashed `merge_same_card`'s sum with a float sibling and the Json dump
    of a re-hold. `edi.build` reads float(quantity), so no shipped byte changes."""
    import json

    from app.orders import hold_place, match
    qid = teach.ask(pg, message_id="m1", customer_ean=CUST, customer_name="P",
                    wording="rožok 50g", quantity=12, unit="ks", candidates=[])
    pg.execute("UPDATE order_questions SET quantity = 12.5, status = 'answered' WHERE id = %s",
               (qid,))
    ds = [match.Decision(item_name="rožok 50g", gtin=G_ROLL, card="R", confidence=1.0,
                         rule="human_taught", note="", quantity=12, unit="ks"),
          match.Decision(item_name="rožok 50g", gtin=G_ROLL, card="R", confidence=1.0,
                         rule="human_taught", note="", quantity=2.5, unit="ks")]
    hold_place._apply_confirmed_quantities(pg, ds, [qid])
    merged = match.merge_same_card(ds)
    assert merged[0].quantity == 15.0
    json.dumps(hold_place._dump_decisions(merged))


# --- static orders: an unknown code goes to the AI pipeline (which holds + asks) ----------

KARMEN_TEXT = (
    "Vyšlá objednávka č.: 12345/2026\n"
    "KARMEN 7, Prešov\n"
    "Prev.:7\n"
    "Dátum vystavenia: 01.08.2026\n"
    "Termín dodávky: 03.08.2026\n"
    "Množstvo\n"
    "8588001800013 Rožok štandart 50g 10,000 ks 0,50\n"
    "Nákupná cena spolu\n"
)


def _static_env(pg):
    pg.execute(
        """INSERT INTO messages (message_id, category, subject, combined_text, has_attachments,
                                 processed)
           VALUES ('s1', 'static_orders', 'Vyšlá objednávka', %s, false, false)""",
        (KARMEN_TEXT,))
    return snapshot.import_snapshot(
        pg, "GTIN,Sklad,Názov,doplnok\n8588001800013,1,Rožok štandart 50g,\n",
        "Názov organizácie,EAN kód EDI,Obec,Ulica,Meno pre fakturáciu,Číslo mobilu,E-mail\n"
        "Pekáreň s.r.o.,2000000000864,Martin,Košútka 1,,,sklad@pekaren.sk\n")


def _static_cfg():
    return Config(pg_dsn="", data_dir="/tmp", static_orders_engine="python",
                  static_orders_shadow=False)


def test_a_static_order_with_a_code_codex_lacks_goes_to_the_ai_pipeline(pg):
    _static_env(pg)
    _codex(pg, ("8588001800099",))
    seen, uploads = {}, []

    def fake_pipeline(conn, cfg, message, snapshot_id):
        seen["message_id"] = message["message_id"]
        return {"status": "held", "items": []}

    assert static_worker.tick(pg, _static_cfg(), pipeline=fake_pipeline,
                              upload=lambda c, n, b: uploads.append(n)) == 1
    assert seen["message_id"] == "s1"
    assert uploads == [] and pg.execute("SELECT count(*) FROM edi_sent").fetchone()[0] == 0
    note = pg.execute("SELECT result->>'static_fallback' FROM order_runs").fetchone()[0]
    assert "8588001800013" in note and "CODEX" in note


def test_a_static_order_with_known_codes_ships_as_before(pg):
    _static_env(pg)
    _codex(pg, ("8588001800013",))
    uploads = []

    def no_pipeline(*a, **k):
        raise AssertionError("a clean static order never falls back")

    assert static_worker.tick(pg, _static_cfg(), pipeline=no_pipeline,
                              upload=lambda c, n, b: uploads.append(n) or True) == 1
    assert uploads == ["KARMEN_12345_2026_007.txt"]


# --- the orders item answer refuses a card whose code CODEX lacks -------------------------

def _app_client():
    app = create_app(Config(pg_dsn=PG_DSN, data_dir="/tmp", api_token="tok",
                            dash_password="secret", secret_key="test-secret", odoo_url="",
                            odoo_api_key="", orders_channel_id=0, orders_shadow=False))
    app.testing = True
    c = app.test_client()
    c.get("/sklad/" + sklad_key("test-secret"))
    return c


def _item_question(pg):
    snapshot.import_snapshot(pg, CATALOG_CSV, CUSTOMER_CSV)
    pg.execute("INSERT INTO messages (message_id, category) VALUES ('mq', 'ai_orders')")
    return pg.execute(
        """INSERT INTO order_questions (message_id, customer_ean, customer_name, wording,
                                        item_key, quantity, unit, candidates, delivery_date,
                                        reason)
           VALUES ('mq', %s, 'Pekáreň Testovacia', 'vianočka', 'vianocka', 7, 'ks', %s,
                   '04.08.2026', 'test') RETURNING id""",
        (CUST, Json([{"gtin": G_DEAD, "name": "Vianočka 400g"},
                     {"gtin": G_VIA, "name": "Vianočka maslová 400g"}]))).fetchone()[0]


def test_an_item_answer_refuses_a_card_whose_code_codex_lacks(pg):
    qid = _item_question(pg)
    _codex(pg, ALL_BUT_DEAD)
    c = _app_client()
    r = c.post(f"/api/orders/question/{qid}/answer",
               json={"gtin": G_DEAD, "card": "Vianočka 400g"})
    assert r.status_code == 409, r.get_json()
    body = r.get_json()
    assert body["codex"]["missing"] is True and G_DEAD in body["error"]
    assert "objednávk" in body["error"]
    assert teach.get(pg, qid)["status"] == "open"
    assert pg.execute("SELECT count(*) FROM item_memory").fetchone()[0] == 0

    ok = c.post(f"/api/orders/question/{qid}/answer",
                json={"gtin": G_VIA, "card": "Vianočka maslová 400g"})
    assert ok.status_code == 200, ok.get_json()
    assert teach.get(pg, qid)["status"] == "answered"


def test_an_item_answer_passes_when_the_codex_list_is_stale(pg):
    qid = _item_question(pg)
    _codex(pg, ALL_BUT_DEAD, hours_old=codex_cards.STALE_HOURS + 5)
    r = _app_client().post(f"/api/orders/question/{qid}/answer",
                           json={"gtin": G_DEAD, "card": "Vianočka 400g"})
    assert r.status_code == 200, r.get_json()


# --- the stale-list ops alert now covers orders too ---------------------------------------

def test_the_stale_list_alert_names_orders_too(pg):
    _codex(pg, ALL_BUT_DEAD, hours_old=codex_cards.STALE_HOURS + 2)
    cfg = Config(pg_dsn="", data_dir="/tmp", ops_channel_id=77)
    assert codex_cards.stale_sweep(pg, cfg) is True
    body = pg.execute("SELECT body_html FROM pending_alerts WHERE kind = %s",
                      (codex_cards.ALERT_KIND,)).fetchone()[0]
    assert "objednáv" in body and "dodac" in body


def test_the_worker_runs_the_stale_sweep_for_an_orders_only_install(pg):
    """Before #479 the sweep ran only with the DL engine live — an orders-only install whose
    list went stale would have run with the ORDER gate OFF and nobody told. One real loop of
    the real worker (orders engine only) must leave the stale-list ops alert in the outbox."""
    import threading

    from app.orders import worker
    _codex(pg, ALL_BUT_DEAD, hours_old=codex_cards.STALE_HOURS + 2)
    stop = threading.Event()

    def sleep(_s):
        stop.set()

    cfg = Config(pg_dsn=PG_DSN, data_dir="/tmp", ai_orders_engine="python",
                 orders_shadow=False, delivery_notes_engine="n8n", ops_channel_id=77)
    worker.run_forever(pg, cfg, stop=stop, sleep=sleep, pipeline=lambda *a, **k: {})
    rows = pg.execute("SELECT channel_id FROM pending_alerts WHERE kind = %s",
                      (codex_cards.ALERT_KIND,)).fetchall()
    assert rows == [(77,)]
