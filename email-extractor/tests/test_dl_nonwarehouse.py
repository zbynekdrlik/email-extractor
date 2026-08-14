"""#314 — non-warehouse supplier memory (app/orders/dl_nonwarehouse.py).

All fixtures are SYNTHETIC (this repo is public). Uses the shared `pg` fixture (real test
Postgres) so remember/resolve run against the real dl_nonwarehouse_supplier table + its
UNIQUE (identity) index, and record/seed/sweep run against real order_questions rows.
"""
from __future__ import annotations

from app.orders import dl_nonwarehouse, teach

# --- _name_key: conservative identity normalizer -----------------------------------

def test_name_key_strips_legal_suffix_and_normalizes():
    # the SAME company, written three ways, keys identically
    assert dl_nonwarehouse._name_key("Messer Tatragas, spol. s r.o.") == "messer tatragas"
    assert dl_nonwarehouse._name_key("MESSER TATRAGAS spol s r o") == "messer tatragas"
    assert dl_nonwarehouse._name_key("  Messer   Tatragas ") == "messer tatragas"
    # a.s. / plain names
    assert dl_nonwarehouse._name_key("Bardusch Slovakia s.r.o.") == "bardusch slovakia"
    assert dl_nonwarehouse._name_key("") == ""
    # genuinely different companies never fold together
    assert dl_nonwarehouse._name_key("Bardusch") != dl_nonwarehouse._name_key("EKVIA")


# --- remember + resolve: identity keys, email is a fallback only (req 3) ------------

def test_remember_and_resolve_by_registry_ean(pg):
    assert dl_nonwarehouse.remember(pg, "2000000000786", "Bardusch Slovakia s.r.o.", "")
    assert dl_nonwarehouse.resolve(pg, "2000000000786", "", "") is not None
    assert dl_nonwarehouse.resolve(pg, "2000000000999", "", "") is None


def test_resolve_matches_by_normalized_name_regardless_of_legal_suffix(pg):
    dl_nonwarehouse.remember(pg, "2000000000207", "Messer Tatragas, spol. s r.o.", "")
    # a later mail whose extracted name is written differently still matches on name_key
    assert dl_nonwarehouse.resolve(pg, "", "MESSER TATRAGAS spol s r o", "") is not None


def test_a_real_identity_row_never_matches_on_email_alone(pg):
    """Req 3: one sender (a tlaciaren@ that forwards everything) sends both warehouse and
    non-warehouse mail — a remembered supplier's OWN identity (ean/name) is the key, never
    the shared address. Remembering Bardusch by ean+name must NOT suppress a DIFFERENT
    supplier that happens to mail from the same address."""
    dl_nonwarehouse.remember(pg, "2000000000786", "Bardusch Slovakia s.r.o.", "shared@print.sk")
    # a genuinely different supplier from the SAME address is NOT remembered
    assert dl_nonwarehouse.resolve(pg, "2000000000999", "Pekáreň Skutočná", "shared@print.sk") is None
    # Bardusch itself still resolves by its own identity (ean or name), not the address
    assert dl_nonwarehouse.resolve(pg, "2000000000786", "", "") is not None
    assert dl_nonwarehouse.resolve(pg, "", "Bardusch Slovakia s. r. o.", "") is not None


def test_unregistered_supplier_is_remembered_by_email_only(pg):
    """The email is the SOLE match basis ONLY for a row that has no ean and no name (an
    unregistered supplier we could key only by address, e.g. david@grena.sk)."""
    assert dl_nonwarehouse.remember(pg, "", "", "david@grena.sk")
    assert dl_nonwarehouse.resolve(pg, "", "", "david@grena.sk") is not None
    assert dl_nonwarehouse.resolve(pg, "", "", "iny@inde.sk") is None


def test_remember_is_idempotent(pg):
    assert dl_nonwarehouse.remember(pg, "2000000000441", "Stavebniny KLEŠČ, s.r.o.", "")
    dl_nonwarehouse.remember(pg, "2000000000441", "Stavebniny KLEŠČ, s.r.o.", "")
    n = pg.execute("SELECT count(*) FROM dl_nonwarehouse_supplier "
                   "WHERE supplier_ean='2000000000441'").fetchone()[0]
    assert n == 1


def test_remember_with_no_identity_records_nothing(pg):
    assert dl_nonwarehouse.remember(pg, "", "", "") is False
    assert pg.execute("SELECT count(*) FROM dl_nonwarehouse_supplier").fetchone()[0] == 0


# --- record_for_message: from a message's own dl_item/dl_supplier questions ---------

def test_record_for_message_remembers_the_dl_item_supplier(pg):
    pg.execute("INSERT INTO messages (message_id, category, from_addr) "
               "VALUES ('m1', 'dodacie_listy', 'x@y.sk')")
    teach.ask_dl_item(pg, "m1", "2000000000527", "PRACOVNÉ ODEVY ZIGO, s.r.o.",
                      "Pracovný odev", 1, "ks", [], reason="test")
    n = dl_nonwarehouse.record_for_message(pg, "m1")
    assert n == 1
    assert dl_nonwarehouse.resolve(pg, "2000000000527", "", "") is not None


def test_record_for_message_remembers_a_dl_supplier_by_extracted_name(pg):
    pg.execute("INSERT INTO messages (message_id, category, from_addr) "
               "VALUES ('m2', 'dodacie_listy', 'unknown@supplier.sk')")
    # a dl_supplier question now stores the extracted supplier_name (#314)
    teach.ask_dl_supplier(pg, "m2", "unknown@supplier.sk", [],
                          supplier_name="Nový Neskladový Dodávateľ")
    n = dl_nonwarehouse.record_for_message(pg, "m2")
    assert n == 1
    # keyed on the extracted NAME — req 3: when a real supplier identity exists, the email
    # is NOT a match key (a bare-address match is reserved for a supplier with no name).
    assert dl_nonwarehouse.resolve(pg, "", "Nový Neskladový Dodávateľ", "") is not None
    assert dl_nonwarehouse.resolve(pg, "", "", "unknown@supplier.sk") is None


# --- seed_from_history: backfill from #307's already-closed not_warehouse questions --

def test_seed_from_history_backfills_closed_not_warehouse(pg):
    pg.execute("INSERT INTO messages (message_id, category, from_addr) "
               "VALUES ('h1', 'dodacie_listy', 'z@z.sk')")
    qid = teach.ask_dl_item(pg, "h1", "2000000000264", "EKVIA s.r.o.",
                            "Neznáma položka", 1, "ks", [])
    pg.execute("UPDATE order_questions SET status='not_warehouse' WHERE id=%s", (qid,))
    assert dl_nonwarehouse.resolve(pg, "2000000000264", "", "") is None   # not yet seeded
    n = dl_nonwarehouse.seed_from_history(pg)
    assert n >= 1
    assert dl_nonwarehouse.resolve(pg, "2000000000264", "", "") is not None


# --- sweep_open_questions: close open questions of an already-remembered supplier ---

def test_sweep_closes_open_questions_of_remembered_suppliers(pg):
    dl_nonwarehouse.remember(pg, "2000000000207", "Messer Tatragas, spol. s r.o.", "")
    pg.execute("INSERT INTO messages (message_id, category, from_addr, processed) "
               "VALUES ('s1', 'dodacie_listy', 'm@m.sk', false)")
    qid = teach.ask_dl_item(pg, "s1", "2000000000207", "Messer Tatragas, spol. s r.o.",
                            "Technický plyn", 1, "ks", [])
    assert pg.execute("SELECT status FROM order_questions WHERE id=%s",
                      (qid,)).fetchone()[0] == "open"

    closed = dl_nonwarehouse.sweep_open_questions(pg, None)
    assert closed == 1
    assert pg.execute("SELECT status FROM order_questions WHERE id=%s",
                      (qid,)).fetchone()[0] == "not_warehouse"
    assert pg.execute("SELECT processed FROM messages WHERE message_id='s1'"
                      ).fetchone()[0] is True


def test_sweep_leaves_a_non_remembered_supplier_untouched(pg):
    # nobody is remembered
    pg.execute("INSERT INTO messages (message_id, category, from_addr, processed) "
               "VALUES ('s2', 'dodacie_listy', 'hk@loan.eu', false)")
    qid = teach.ask_dl_item(pg, "s2", "2000000000111", "HK LOAN (skutočný dodávateľ)",
                            "Múka T650", 1, "kg", [])
    closed = dl_nonwarehouse.sweep_open_questions(pg, None)
    assert closed == 0
    assert pg.execute("SELECT status FROM order_questions WHERE id=%s",
                      (qid,)).fetchone()[0] == "open"


def test_bootstrap_seeds_then_sweeps(pg):
    # one historical not_warehouse closure (seeds the memory) ...
    pg.execute("INSERT INTO messages (message_id, category, from_addr) "
               "VALUES ('b1', 'dodacie_listy', 'a@a.sk')")
    q_hist = teach.ask_dl_item(pg, "b1", "2000000000786", "Bardusch Slovakia s.r.o.",
                               "Odev A", 1, "ks", [])
    pg.execute("UPDATE order_questions SET status='not_warehouse' WHERE id=%s", (q_hist,))
    # ... and a still-OPEN question from the SAME (now-remembered) supplier
    pg.execute("INSERT INTO messages (message_id, category, from_addr, processed) "
               "VALUES ('b2', 'dodacie_listy', 'a@a.sk', false)")
    q_open = teach.ask_dl_item(pg, "b2", "2000000000786", "Bardusch Slovakia s.r.o.",
                               "Odev B", 1, "ks", [])

    result = dl_nonwarehouse.bootstrap(pg, None)
    assert result["seeded"] >= 1
    assert result["swept"] == 1
    assert pg.execute("SELECT status FROM order_questions WHERE id=%s",
                      (q_open,)).fetchone()[0] == "not_warehouse"
