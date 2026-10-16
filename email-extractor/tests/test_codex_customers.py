"""#493 — the CODEX customer cards: the push endpoint, the layered customer list, the alerts.

The order engine's customer list = the frozen pre-CODEX base ← the board's edits ← CODEX
(`customer_layers.apply_codex`). These tests pin each layer rule against the real snapshot
machinery (`snapshot.import_snapshot` / `rebuild_from_overrides` / `customers_for_management`)
and the real endpoint. Synthetic data only (invented shops, addresses and EANs).
"""
import os
from datetime import UTC, datetime, timedelta

from app.board.services import audit
from app.board.services import customers as board_customers
from app.config import Config
from app.httpapi import create_app
from app.orders import codex_customers, snapshot

PG_DSN = os.environ.get("PG_TEST_DSN")
CATALOG_CSV = "GTIN,Sklad,Názov,doplnok\nG50,1,Rožok štandart 50g,\n"
BASE_CSV = (
    "Názov organizácie,EAN kód EDI,Obec,Ulica,Meno pre fakturáciu,Číslo mobilu,E-mail\n"
    "Stará karta Mestečko,2999000000301,,,,,stara@karta-test.sk\n"
    "Bez adresy s.r.o.,2999000000302,Mestečko,,,,bez@adresy-test.sk\n"
)
SHOP_A = {"ean_edi": "2999000000310", "name": "Tržnica Mestečko - Riadok 1",
          "trade_name": "Tržnica Mestečko", "emails": ["nakup@retazec-test.sk"],
          "city": "Mestečko", "street": "Riadok 1", "zip": "036 01", "codex_id": "1/"}
SHOP_B = {"ean_edi": "2999000000311", "name": "BISTRO Dolina Krátka 1",
          "trade_name": "BISTRO Dolina", "emails": ["nakup@retazec-test.sk"],
          "city": "Dolina", "street": "Krátka 1", "zip": "010 01", "codex_id": "2/"}
OLD = {"ean_edi": "2999000000301", "name": "Stará karta (CODEX)", "trade_name": "",
       "emails": ["codex@karta-test.sk"], "city": "Mestečko", "street": "Hlavná 5",
       "zip": "036 01", "codex_id": "3/"}
NOADDR = {"ean_edi": "2999000000302", "name": "Bez adresy (CODEX)", "trade_name": "",
          "emails": [], "city": "Iné mesto", "street": "Lipová 2", "zip": "", "codex_id": "4/"}


def _cfg():
    return Config(pg_dsn=PG_DSN, data_dir="/tmp", api_token="tok", dash_password="pw",
                  secret_key="t", ops_channel_id=7)


def _client():
    app = create_app(_cfg())
    app.testing = True
    return app.test_client()


def _push(c, customers, ambiguous=None, force=False, token="tok"):
    url = "/api/codex/customers" + ("?force=1" if force else "")
    return c.post(url, json={"source_as_of": "2026-10-16T12:00:00+00:00",
                             "customers": customers, "ambiguous": ambiguous or []},
                  headers={"X-Token": token} if token else {})


def _base(pg):
    return snapshot.import_snapshot(pg, CATALOG_CSV, BASE_CSV)


def _current(pg):
    return {c["ean_edi"]: c for c in snapshot.load_customers(pg, snapshot.latest_snapshot_id(pg))}


def _managed(pg):
    return {c["ean_edi"]: c for c in snapshot.customers_for_management(pg)}


# --- the endpoint ---------------------------------------------------------------------------

def test_the_push_needs_the_header_token(pg):
    _base(pg)
    c = _client()
    assert _push(c, [SHOP_A], token=None).status_code == 403
    assert _push(c, [SHOP_A], token="nope").status_code == 403
    assert c.post("/api/codex/customers?token=tok", json={"customers": [SHOP_A]}).status_code \
        == 403, "the query token is refused for a full-replace endpoint"
    assert pg.execute("SELECT count(*) FROM codex_customers").fetchone()[0] == 0


def test_a_bad_or_empty_body_is_refused(pg):
    c = _client()
    assert c.post("/api/codex/customers", json={"x": 1},
                  headers={"X-Token": "tok"}).status_code == 400
    assert _push(c, []).status_code == 400
    assert _push(c, [{"ean_edi": "abc", "name": "X"}]).status_code == 400


def test_a_push_replaces_the_copy_and_freezes_it_into_the_order_snapshot(pg):
    _base(pg)
    c = _client()
    r = _push(c, [SHOP_A, SHOP_B])
    assert r.status_code == 200
    body = r.get_json()
    assert body["stored"] == 2 and body["snapshot_id"] == snapshot.latest_snapshot_id(pg)
    cur = _current(pg)
    assert cur["2999000000310"]["trade_name"] == "Tržnica Mestečko"
    assert cur["2999000000311"]["emails"] == ["nakup@retazec-test.sk"]
    # a card removed from CODEX leaves the order engine too (nothing baked in for good)
    assert _push(c, [SHOP_A]).status_code == 200
    assert "2999000000311" not in _current(pg)
    assert pg.execute("SELECT count(*) FROM codex_customer_syncs").fetchone()[0] == 2


def test_the_shrink_guard_refuses_a_half_loaded_push_unless_forced(pg):
    _base(pg)
    c = _client()
    many = [dict(SHOP_A, ean_edi=f"29990000004{i:02d}", codex_id=f"{i}/") for i in range(10)]
    assert _push(c, many).status_code == 200
    assert _push(c, many[:3]).status_code == 409
    assert pg.execute("SELECT count(*) FROM codex_customers").fetchone()[0] == 10
    assert _push(c, many[:3], force=True).status_code == 200
    assert pg.execute("SELECT count(*) FROM codex_customers").fetchone()[0] == 3


def test_an_ean_twice_in_the_body_is_stored_for_neither_and_reported(pg):
    _base(pg)
    r = _push(_client(), [SHOP_A, SHOP_B, dict(SHOP_B, codex_id="9/", name="Iná firma")])
    assert r.get_json()["stored"] == 1 and r.get_json()["ambiguous"] == 1
    assert "2999000000311" not in _current(pg)


def test_the_ambiguous_eans_alert_ops_once_per_change(pg):
    _base(pg)
    c = _client()
    amb = [{"ean_edi": "2999000000399", "firms": [{"name": "Predajňa X", "city": "Mestečko"},
                                                  {"name": "Iná firma", "city": "Dolina"}]}]
    _push(c, [SHOP_A], ambiguous=amb)
    _push(c, [SHOP_A], ambiguous=amb)

    def alerts():
        return pg.execute("SELECT body_html FROM pending_alerts WHERE kind = %s",
                          (codex_customers.AMBIGUOUS_KIND,)).fetchall()
    assert len(alerts()) == 1, "an unchanged set alerts once"
    assert "2999000000399" in alerts()[0][0] and "Iná firma" in alerts()[0][0]
    _push(c, [SHOP_A], ambiguous=amb + [{"ean_edi": "2999000000398", "firms": []}])
    assert len(alerts()) == 2, "a changed set alerts again"
    _push(c, [SHOP_A], ambiguous=[])
    assert len(alerts()) == 2, "an empty set never alerts"


def test_a_stale_copy_alerts_once_per_episode_but_stays_in_use(pg):
    _base(pg)
    _push(_client(), [SHOP_A])
    later = datetime(2026, 10, 16, 12, 0, tzinfo=UTC) + timedelta(hours=31)
    assert codex_customers.stale_sweep(pg, _cfg(), now=later) is True
    assert codex_customers.stale_sweep(pg, _cfg(), now=later + timedelta(minutes=5)) is False
    # fail-open: the old copy is still layered in
    snapshot.rebuild_from_overrides(pg)
    assert "2999000000310" in _current(pg)


# --- the layers -----------------------------------------------------------------------------

def test_codex_unions_addresses_and_fills_only_blank_fields(pg):
    _base(pg)
    _push(_client(), [OLD, NOADDR])
    cur = _current(pg)
    old = cur["2999000000301"]
    assert old["emails"] == ["stara@karta-test.sk", "codex@karta-test.sk"]
    assert old["name"] == "Stará karta Mestečko", "a name the base has wins"
    assert (old["city"], old["street"], old["zip"]) == ("Mestečko", "Hlavná 5", "036 01")
    noaddr = cur["2999000000302"]
    assert noaddr["city"] == "Mestečko", "a non-blank city is never overwritten"
    assert noaddr["street"] == "Lipová 2"


def test_a_board_edit_wins_over_codex(pg):
    _base(pg)
    rid = snapshot.upsert_customer(pg, override_id=None, orig_ean_edi="2999000000301",
                                   orig_street="", ean_edi="2999000000301",
                                   name="Stará karta — tabuľa", emails=["stara@karta-test.sk"],
                                   city="Iné", street="Tabuľová 1", zip_="")
    _push(_client(), [OLD])
    old = _current(pg)["2999000000301"]
    assert (old["name"], old["city"], old["street"]) == ("Stará karta — tabuľa", "Iné",
                                                         "Tabuľová 1")
    assert old["zip"] == "036 01", "only the blank field comes from CODEX"
    assert rid


def test_a_card_deleted_on_the_board_stays_deleted(pg):
    _base(pg)
    snapshot.retire_customer(pg, override_id=None, orig_ean_edi="2999000000301",
                             orig_street="")
    _push(_client(), [OLD, SHOP_A])
    assert "2999000000301" not in _current(pg)
    assert "2999000000301" not in _managed(pg)


def test_an_address_removed_on_the_board_never_comes_back_until_added_again(pg):
    _base(pg)
    _push(_client(), [OLD])
    assert snapshot.forget_customer_email(pg, "2999000000301", "codex@karta-test.sk")
    snapshot.rebuild_from_overrides(pg)
    assert "codex@karta-test.sk" not in _current(pg)["2999000000301"]["emails"]
    _push(_client(), [OLD])            # the next CODEX push must not resurrect it
    assert "codex@karta-test.sk" not in _current(pg)["2999000000301"]["emails"]
    assert snapshot.remember_customer_email(pg, "2999000000301", "codex@karta-test.sk")
    snapshot.rebuild_from_overrides(pg)
    assert "codex@karta-test.sk" in _current(pg)["2999000000301"]["emails"]


def test_remember_on_a_codex_only_card_stores_only_the_boards_own_address(pg):
    _base(pg)
    _push(_client(), [SHOP_A])
    assert snapshot.remember_customer_email(pg, "2999000000310", "iny@retazec-test.sk")
    row = pg.execute("SELECT emails, city, street FROM customer_overrides "
                     "WHERE ean_edi = '2999000000310'").fetchone()
    assert row == (["iny@retazec-test.sk"], "", ""), "CODEX keeps owning the rest"
    snapshot.rebuild_from_overrides(pg)
    shop = _current(pg)["2999000000310"]
    assert shop["emails"] == ["iny@retazec-test.sk", "nakup@retazec-test.sk"]
    assert shop["street"] == "Riadok 1"


def test_an_unchanged_board_save_keeps_a_hand_added_cards_identity(pg):
    """The form shows CODEX-filled values; saving it unchanged must not move a hand-added
    card's stored street (that left the old baked copy behind as a ghost duplicate)."""
    _base(pg)
    rid = snapshot.upsert_customer(pg, override_id=None, orig_ean_edi=None, orig_street=None,
                                   ean_edi="2999000000310", name="Tržnica (otázka)",
                                   emails=["preposiela@nasa-firma-test.sk"], city="",
                                   street="", zip_="")
    snapshot.rebuild_from_overrides(pg)
    _push(_client(), [SHOP_A])
    shown = _managed(pg)["2999000000310"]
    assert shown["street"] == "Riadok 1"
    board_customers.save_customer(pg, _cfg(), "admin", {
        "override_id": rid, "orig_ean_edi": None, "orig_street": None,
        "ean_edi": "2999000000310", "name": shown["name"],
        "emails": "nakup@retazec-test.sk",          # the forwarder removed, CODEX's kept
        "city": shown["city"], "street": shown["street"], "zip": shown["zip"]})
    stored = pg.execute("SELECT emails, street, removed_emails FROM customer_overrides "
                        "WHERE id = %s", (rid,)).fetchone()
    assert stored == ([], "", []), "only the board's own data is stored"
    rows = [c for c in snapshot.load_customers(pg, snapshot.latest_snapshot_id(pg))
            if c["ean_edi"] == "2999000000310"]
    assert len(rows) == 1, "no ghost duplicate"
    assert rows[0]["emails"] == ["nakup@retazec-test.sk"]


def test_removing_a_codex_address_on_the_board_keeps_it_off(pg):
    _base(pg)
    _push(_client(), [OLD])
    shown = _managed(pg)["2999000000301"]
    board_customers.save_customer(pg, _cfg(), "admin", {
        "override_id": None, "orig_ean_edi": shown["orig_ean_edi"],
        "orig_street": shown["orig_street"], "ean_edi": "2999000000301",
        "name": shown["name"], "emails": "stara@karta-test.sk",
        "city": shown["city"], "street": shown["street"], "zip": shown["zip"]})
    _push(_client(), [OLD])
    assert _current(pg)["2999000000301"]["emails"] == ["stara@karta-test.sk"]


def test_a_rebuild_layers_on_the_pre_codex_base_and_is_stable(pg):
    base = _base(pg)
    _push(_client(), [SHOP_A])
    sid = snapshot.latest_snapshot_id(pg)
    assert snapshot.customer_base_id(pg, sid) == base
    assert snapshot.rebuild_from_overrides(pg) == sid, "unchanged content reuses the snapshot"
    assert snapshot.customer_base_id(pg, sid) == base


def test_without_codex_data_the_snapshot_content_is_unchanged(pg):
    """The e2e-orders corpus has no CODEX rows: the frozen content (and its hash) must be
    byte-identical to the pre-#493 shape."""
    sid = _base(pg)
    assert snapshot.rebuild_from_overrides(pg) == sid
    assert snapshot.customer_base_id(pg, sid) == sid
    assert all(c["trade_name"] == "" for c in snapshot.load_customers(pg, sid))


def test_codex_customer_writes_are_restorable_like_any_override(pg):
    """A board delete of a CODEX-only card is a normal audited soft delete (Kôš)."""
    _base(pg)
    _push(_client(), [SHOP_A])
    shown = _managed(pg)["2999000000310"]
    board_customers.delete_customer(pg, _cfg(), "admin", {
        "override_id": None, "orig_ean_edi": shown["orig_ean_edi"],
        "orig_street": shown["orig_street"]})
    assert "2999000000310" not in _current(pg)
    aid = pg.execute("SELECT id FROM audit_log WHERE table_name = 'customer_overrides' "
                     "AND action = 'delete' ORDER BY id DESC LIMIT 1").fetchone()[0]
    audit.restore(pg, aid)
    assert "2999000000310" in _current(pg)


def test_editing_a_hand_added_cards_street_leaves_no_ghost_of_the_baked_copy(pg):
    """Before #493 the base was the latest snapshot, which carries every board card baked in
    under the street it had THEN; a street edit excluded only the NEW street, so the baked copy
    survived as a second card with the old addresses. A hand-added card owns its EAN."""
    _base(pg)
    rid = snapshot.upsert_customer(pg, override_id=None, orig_ean_edi=None, orig_street=None,
                                   ean_edi="2999000000310", name="Tržnica (otázka)",
                                   emails=["preposiela@nasa-firma-test.sk"], city="",
                                   street="", zip_="")
    sid = snapshot.rebuild_from_overrides(pg)
    # a pre-#493 snapshot: its own customer base, the card baked in with street ""
    pg.execute("UPDATE order_snapshots SET customer_base_id = NULL WHERE id = %s", (sid,))
    snapshot.upsert_customer(pg, override_id=rid, orig_ean_edi=None, orig_street=None,
                             ean_edi="2999000000310", name="Tržnica Mestečko",
                             emails=["nakup@retazec-test.sk"], city="Mestečko",
                             street="Riadok 1", zip_="")
    snapshot.rebuild_from_overrides(pg)
    rows = [c for c in snapshot.load_customers(pg, snapshot.latest_snapshot_id(pg))
            if c["ean_edi"] == "2999000000310"]
    assert len(rows) == 1, "no ghost of the old street"
    assert rows[0]["emails"] == ["nakup@retazec-test.sk"]
