"""#476 — the carryover import alert tells the warehouse EXACTLY what is still waiting in ORION,
and why CODEX will never take a file when that is knowable.

Live incident (30.9.): the delivery-notes channel got „⚠️ Stále 4 dodacie listy neprevzatých
v ORIONe (od 18:03)" twice a day while 3 of those 4 had been imported two days earlier — the
reminder counted every file that EVER joined the incident, not the ones still waiting. The one
really waiting (DL in `in_DL`) never imports because one of its LIN lines carries a code CODEX has
no stock card for (#467), and nothing told the warehouse that.

Pinned here:
- a reminder counts + lists ONLY the members still waiting (no terminal status AND still in the
  queued folder of this sweep's listing), with the document number and the supplier;
- a waiting DESADV whose LIN code CODEX lacks gets an explicit plain-Slovak line;
- all imported → no reminder, one all-clear, the incident closes;
- a stale / never-pushed CODEX list or an unreadable file → no code line (fail-open), the
  reminder itself still goes out.

All fixtures SYNTHETIC (made-up EANs, codes, names, numbers) — public repo.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from unittest.mock import MagicMock, patch

from test_orders_confirm import (
    MON_EVENING,
    TUE_MORNING,
    TZ,
    PostRecorder,
    _cfg,
    _desadv_status,
    _insert_at,
    _insert_desadv_at,
)

from app.config import Config
from app.orders import (
    codex_cards,
    confirm,
    desadv_edi,
    dl_alerts,
    dl_snapshot,
    question_alerts,
    report,
    snapshot,
    upload,
)

SUP_A = "2000000000017"
SUP_A_NAME = "Pekáreň Testovacia s.r.o."
SUP_B = "2000000000024"
SUP_B_NAME = "Mliekareň Vzorová a.s."
SUPPLIERS_CSV = ("Názov organizácie,EAN kód EDI,Obec,Ulica,Meno pre fakturáciu,"
                 "Číslo mobilu,E-mail\n"
                 f"{SUP_A_NAME},{SUP_A},Testovo,Hlavná 1,,,dl@testovacia.example\n"
                 f"{SUP_B_NAME},{SUP_B},Vzorovo,Dlhá 2,,,dl@vzorova.example\n")
DL_CATALOG_CSV = ("GTIN,Názov,doplnok,hmotnost,Sklad,Cena\n"
                  "9990000000017,Rožok testovací 50g,,0.05,1,0.10\n")

GOOD_A = "9990000000017"
GOOD_B = "9990000000024"
DEAD = "4711"                      # a code no CODEX stock card has
CODEX = [
    {"code": GOOD_A, "card_code": "901", "stredisko": 1, "sklad": 1,
     "name": "Rožok testovací 50g", "inactive": False, "changed_at": None},
    {"code": GOOD_B, "card_code": "902", "stredisko": 1, "sklad": 1,
     "name": "Chlieb testovací 500g", "inactive": False, "changed_at": None},
]

DOCS = [("126000001", SUP_A), ("126000002", SUP_A), ("P2600003", SUP_B), ("126000004", SUP_A)]


def _fname(doc: str, sup: str) -> str:
    return f"DESADV_{sup[-6:]}_{doc}_20260803_120000000.txt"


def _wire(doc: str, sup: str) -> str:
    return f"Z-{_fname(doc, sup)}"


def _suppliers(pg):
    dl_snapshot.import_snapshot(pg, DL_CATALOG_CSV, "GTIN,Sklad,Názov,doplnok\n",
                                SUPPLIERS_CSV)


def _codex(pg, as_of):
    codex_cards.replace_cards(pg, CODEX, source_as_of=as_of)


def _desadv_content(doc: str, codes: list[str]) -> str:
    """A real DESADV body built by the production generator (the exact LIN layout ORION
    receives), so the parser is tested against what we really upload."""
    items = [{"gtin": c, "name": f"Položka {i}", "supplierName": f"Položka {i}",
              "quantity": 2, "unit": "ks", "unitPrice": 1.5}
             for i, c in enumerate(codes, 1)]
    return desadv_edi.generate(
        {"customerEanEdi": SUP_A, "customerName": SUP_A_NAME, "docNumber": doc,
         "deliveryDate": "03.08.2026", "items": items}, {}, {}).content


class Reader:
    """Stand-in for `upload.read_files` — records every call (a stale CODEX list must never
    even trigger a read)."""

    def __init__(self, files: dict[str, str] | None = None, error: Exception | None = None):
        self.files = files or {}
        self.error = error
        self.calls: list[list[str]] = []

    def __call__(self, names):
        self.calls.append(list(names))
        if self.error:
            raise self.error
        return {n: self.files[n] for n in names if n in self.files}


def _dirs(in_dl=(), arch=()):
    return {"in": set(), "in_DL": set(in_dl), "archCodex": set(arch), "unconfirmed": set()}


def _make_due(pg):
    pg.execute("UPDATE desadv_sent SET import_checked_at = now() - interval '10 minutes'")


def _four_member_incident(pg, posts, **kw):
    """Opens ONE carryover incident with all 4 DLs (uploaded Monday evening, still waiting on
    Tuesday morning), then imports the first 3 half an hour later."""
    ids = [_insert_desadv_at(pg, sup, _fname(doc, sup), uploaded_at=MON_EVENING,
                             doc_number=doc) for doc, sup in DOCS]
    all_wire = [_wire(d, s) for d, s in DOCS]
    confirm.sweep(pg, _cfg(), listdir=lambda: _dirs(in_dl=all_wire), post=posts,
                  now=TUE_MORNING, **kw)
    assert len(posts.calls) == 1, "one grouped opening alert for the 4 carryover files"
    _make_due(pg)
    n = confirm.sweep(pg, _cfg(),
                      listdir=lambda: _dirs(in_dl=all_wire[3:], arch=all_wire[:3]),
                      post=posts, now=TUE_MORNING.replace(minute=30), **kw)
    assert n == 3
    assert [_desadv_status(pg, i) for i in ids] == ["imported"] * 3 + [None]
    assert len(posts.calls) == 1, "3 imported, 1 still waiting: no all-clear, no reminder yet"
    return ids, all_wire


# --- the count + the list --------------------------------------------------------------

def test_reminder_counts_and_lists_only_the_file_still_waiting(pg):
    """The live incident: 4 members, 3 imported → the reminder says 1 and names it."""
    _suppliers(pg)
    posts = PostRecorder()
    _ids, all_wire = _four_member_incident(pg, posts)

    _make_due(pg)
    confirm.sweep(pg, _cfg(), listdir=lambda: _dirs(in_dl=all_wire[3:], arch=all_wire[:3]),
                  post=posts, now=TUE_MORNING.replace(hour=16))
    assert len(posts.calls) == 2, "exactly one reminder once the 4h threshold passed"
    html = posts.calls[1][0]
    assert "Stále 1 dodací list neprevzatý v ORIONe" in html, html
    assert "4 dodac" not in html and "dodacie listy" not in html
    assert "126000004" in html and SUP_A_NAME in html, "the waiting DL + its supplier"
    assert "(od 3.8. 18:00)" in html, "since the waiting file's upload, day included"
    for doc in ("126000001", "126000002", "P2600003"):
        assert doc not in html, f"already imported {doc} must not be listed"
    assert SUP_B_NAME not in html


def test_reminder_skips_a_member_already_in_archcodex_before_its_own_recheck(pg):
    """'Still waiting' is read off THIS sweep's listing, not only the ledger status: a member
    whose own throttled re-check has not run yet but whose file already sits in archCodex is
    imported — it must not be counted as waiting."""
    _suppliers(pg)
    a = _insert_desadv_at(pg, SUP_A, _fname("126000011", SUP_A), uploaded_at=MON_EVENING,
                          doc_number="126000011")
    _insert_desadv_at(pg, SUP_A, _fname("126000012", SUP_A), uploaded_at=MON_EVENING,
                      doc_number="126000012")
    w1, w2 = _wire("126000011", SUP_A), _wire("126000012", SUP_A)
    posts = PostRecorder()
    confirm.sweep(pg, _cfg(), listdir=lambda: _dirs(in_dl=[w1, w2]), post=posts,
                  now=TUE_MORNING)
    assert len(posts.calls) == 1

    # only B is due this sweep; A was checked a moment ago but has meanwhile been imported
    _make_due(pg)
    pg.execute("UPDATE desadv_sent SET import_checked_at = now() WHERE id = %s", (a,))
    confirm.sweep(pg, _cfg(), listdir=lambda: _dirs(in_dl=[w2], arch=[w1]), post=posts,
                  now=TUE_MORNING.replace(hour=16))
    assert len(posts.calls) == 2
    html = posts.calls[1][0]
    assert "Stále 1 dodací list" in html, html
    assert "126000012" in html and "126000011" not in html


def test_opening_alert_lists_every_waiting_file_with_its_supplier(pg):
    _suppliers(pg)
    for doc, sup in DOCS[:3]:
        _insert_desadv_at(pg, sup, _fname(doc, sup), uploaded_at=MON_EVENING, doc_number=doc)
    posts = PostRecorder()
    confirm.sweep(pg, _cfg(),
                  listdir=lambda: _dirs(in_dl=[_wire(d, s) for d, s in DOCS[:3]]),
                  post=posts, now=TUE_MORNING)
    assert len(posts.calls) == 1
    html = posts.calls[0][0]
    assert "3 dodacie listy sú stále neprevzaté" in html, html
    assert f"DL 126000001 ({SUP_A_NAME})" in html
    assert f"DL P2600003 ({SUP_B_NAME})" in html


def test_a_dl_whose_supplier_is_unknown_is_still_listed_by_number(pg):
    _insert_desadv_at(pg, "2000000000031", _fname("777", "2000000000031"),
                      uploaded_at=MON_EVENING, doc_number="777")
    posts = PostRecorder()
    confirm.sweep(pg, _cfg(), listdir=lambda: _dirs(in_dl=[_wire("777", "2000000000031")]),
                  post=posts, now=TUE_MORNING)
    assert "<li>DL 777</li>" in posts.calls[0][0]


def test_order_carryover_lists_delivery_date_and_customer(pg):
    """The ORDER ledger shares the same builder: an order is named by its customer + day."""
    snapshot.import_snapshot(
        pg, "GTIN,Sklad,Názov,doplnok\nG50,1,Rožok 50g,\n",
        "Názov organizácie,EAN kód EDI,Obec,Ulica,Meno pre fakturáciu,Číslo mobilu,E-mail\n"
        "Bistro Skúšobné,8580000000019,Testovo,Hlavná 1,,,bistro@example.sk\n")
    _insert_at(pg, "8580000000019", "ORDER_476.txt", uploaded_at=MON_EVENING)
    posts = PostRecorder()
    confirm.sweep(pg, _cfg(),
                  listdir=lambda: {"in": {"ORDER_476.txt"}, "archCodex": set(),
                                   "unconfirmed": set()},
                  post=posts, now=TUE_MORNING)
    html = posts.calls[0][0]
    assert "1 objednávka je stále neprevzatá" in html, html
    assert "objednávka na 04.08.2026 (Bistro Skúšobné)" in html


def test_a_long_list_is_capped_with_a_remainder_line(pg):
    for i in range(18):
        doc = f"5000{i:02d}"
        _insert_desadv_at(pg, SUP_A, _fname(doc, SUP_A), uploaded_at=MON_EVENING,
                          doc_number=doc)
    posts = PostRecorder()
    confirm.sweep(pg, _cfg(),
                  listdir=lambda: _dirs(in_dl=[_wire(f"5000{i:02d}", SUP_A)
                                               for i in range(18)]),
                  post=posts, now=TUE_MORNING)
    html = posts.calls[0][0]
    assert "18 dodacích listov je stále neprevzatých" in html
    assert html.count("<li>") == confirm.LIST_LIMIT
    assert "a ešte 3 ďalšie." in html, "the remainder word agrees with N"


# --- all imported -> no reminder, the incident closes ------------------------------------

def test_all_imported_sends_no_reminder_one_all_clear_and_closes_the_incident(pg):
    """Characterization pin (true before #476 too): once nothing waits there is no carryover
    group at all, so no reminder — the incident closes with its one all-clear."""
    _suppliers(pg)
    posts = PostRecorder()
    _ids, all_wire = _four_member_incident(pg, posts)

    _make_due(pg)
    confirm.sweep(pg, _cfg(), listdir=lambda: _dirs(arch=all_wire), post=posts,
                  now=TUE_MORNING.replace(hour=16))
    assert len(posts.calls) == 2, "the all-clear only — never a reminder for 0 files"
    assert "Stále" not in posts.calls[1][0]
    assert "prijaté" in posts.calls[1][0]
    open_n = pg.execute("SELECT count(*) FROM import_alert_incidents "
                        "WHERE closed_at IS NULL").fetchone()[0]
    assert open_n == 0

    _make_due(pg)
    confirm.sweep(pg, _cfg(), listdir=lambda: _dirs(arch=all_wire), post=posts,
                  now=TUE_MORNING.replace(hour=20))
    assert len(posts.calls) == 2, "nothing more after the incident closed"


# --- why CODEX will never take it (#467 card list) ---------------------------------------

def test_reminder_says_codex_will_not_take_the_dl_with_an_unknown_code(pg):
    _suppliers(pg)
    _codex(pg, as_of=TUE_MORNING - timedelta(hours=1))
    doc, sup = DOCS[3]
    reader = Reader({_wire(doc, sup): _desadv_content(doc, [GOOD_A, DEAD, GOOD_B])})
    posts = PostRecorder()
    _ids, all_wire = _four_member_incident(pg, posts, read_files=reader)

    _make_due(pg)
    confirm.sweep(pg, _cfg(), listdir=lambda: _dirs(in_dl=all_wire[3:], arch=all_wire[:3]),
                  post=posts, now=TUE_MORNING.replace(hour=16), read_files=reader)
    html = posts.calls[-1][0]
    assert "Stále 1 dodací list" in html, html
    assert (f"DL 126000004 ({SUP_A_NAME}): CODEX ho neprevezme — kód {DEAD} v CODEXe "
            "neexistuje. Zadajte ho ručne.") in html, html
    assert reader.calls and reader.calls[-1] == [_wire(doc, sup)], \
        "only the file still waiting in in_DL is read"


def test_opening_alert_names_the_unknown_code_and_leaves_clean_files_plain(pg):
    _suppliers(pg)
    _codex(pg, as_of=TUE_MORNING - timedelta(hours=1))
    reader = Reader({
        _wire("126000021", SUP_A): _desadv_content("126000021", [GOOD_A, DEAD, "4712"]),
        _wire("126000022", SUP_A): _desadv_content("126000022", [GOOD_A, GOOD_B]),
    })
    # the CLEAN file is the older upload (and the lower id), so upload order alone would list
    # it first — only the dead-first rule puts 126000021 on top (review round 3: the earlier
    # fixture had the dead file first anyway, so the assertion proved nothing)
    for doc, at in (("126000022", MON_EVENING.replace(hour=9)), ("126000021", MON_EVENING)):
        _insert_desadv_at(pg, SUP_A, _fname(doc, SUP_A), uploaded_at=at, doc_number=doc)
    posts = PostRecorder()
    confirm.sweep(pg, _cfg(),
                  listdir=lambda: _dirs(in_dl=list(reader.files)),
                  post=posts, now=TUE_MORNING, read_files=reader)
    html = posts.calls[0][0]
    assert (f"DL 126000021 ({SUP_A_NAME}): CODEX ho neprevezme — kódy {DEAD}, 4712 v CODEXe "
            "neexistujú. Zadajte ho ručne.") in html, html
    assert f"<li>DL 126000022 ({SUP_A_NAME})</li>" in html
    assert html.index("126000021") < html.index("126000022"), \
        "a file CODEX will never take is listed first, ahead of an older clean one"


def test_a_stale_codex_list_drops_the_code_line_and_reads_nothing(pg):
    _suppliers(pg)
    now = datetime(2026, 8, 4, 11, 0, tzinfo=TZ)
    _codex(pg, as_of=now - timedelta(hours=codex_cards.STALE_HOURS + 10))
    _insert_desadv_at(pg, SUP_A, _fname("126000031", SUP_A), uploaded_at=MON_EVENING,
                      doc_number="126000031")
    reader = Reader({_wire("126000031", SUP_A): _desadv_content("126000031", [DEAD])})
    posts = PostRecorder()
    confirm.sweep(pg, _cfg(), listdir=lambda: _dirs(in_dl=list(reader.files)), post=posts,
                  now=now, read_files=reader)
    html = posts.calls[0][0]
    assert "126000031" in html
    assert "CODEX ho neprevezme" not in html
    assert reader.calls == [], "fail-open: a stale list never even reads ORION"


def test_a_never_pushed_codex_list_drops_the_code_line(pg):
    _suppliers(pg)
    _insert_desadv_at(pg, SUP_A, _fname("126000041", SUP_A), uploaded_at=MON_EVENING,
                      doc_number="126000041")
    reader = Reader({_wire("126000041", SUP_A): _desadv_content("126000041", [DEAD])})
    posts = PostRecorder()
    confirm.sweep(pg, _cfg(), listdir=lambda: _dirs(in_dl=list(reader.files)), post=posts,
                  now=TUE_MORNING, read_files=reader)
    assert "CODEX ho neprevezme" not in posts.calls[0][0]
    assert reader.calls == []


def test_an_unreadable_file_still_sends_the_alert_without_a_code_line(pg):
    _suppliers(pg)
    _codex(pg, as_of=TUE_MORNING - timedelta(hours=1))
    _insert_desadv_at(pg, SUP_A, _fname("126000051", SUP_A), uploaded_at=MON_EVENING,
                      doc_number="126000051")
    reader = Reader(error=OSError("sftp down"))
    posts = PostRecorder()
    confirm.sweep(pg, _cfg(), listdir=lambda: _dirs(in_dl=[_wire("126000051", SUP_A)]),
                  post=posts, now=TUE_MORNING, read_files=reader)
    assert len(posts.calls) == 1, "a read failure never blocks the alert"
    html = posts.calls[0][0]
    assert "126000051" in html and "CODEX ho neprevezme" not in html


def test_opening_alert_names_every_waiting_file_not_only_the_rows_due_this_sweep(pg):
    """#476 review: each row has its own 5-minute re-check cycle, so the first sweep past the
    morning hour often has only 1 of N waiting files due. The alert must still name all N
    (and the incident record all of them) — a partial list reads as complete."""
    _suppliers(pg)
    ids = [_insert_desadv_at(pg, sup, _fname(doc, sup), uploaded_at=MON_EVENING,
                             doc_number=doc) for doc, sup in DOCS]
    pg.execute("UPDATE desadv_sent SET import_checked_at = now() WHERE id = ANY(%s)",
               (ids[:3],))                       # 3 re-checked a moment ago, 1 due
    all_wire = [_wire(d, s) for d, s in DOCS]
    posts = PostRecorder()
    confirm.sweep(pg, _cfg(), listdir=lambda: _dirs(in_dl=all_wire), post=posts,
                  now=TUE_MORNING)
    assert len(posts.calls) == 1
    html = posts.calls[0][0]
    assert "4 dodacie listy sú stále neprevzaté" in html, html
    for doc, _sup in DOCS:
        assert f"DL {doc} " in html
    members = pg.execute("SELECT count(*) FROM import_alert_incident_desadv_members"
                         ).fetchone()[0]
    assert members == 4


def test_a_file_not_due_but_already_imported_is_not_pulled_into_the_opening_alert(pg):
    _suppliers(pg)
    ids = [_insert_desadv_at(pg, SUP_A, _fname(doc, SUP_A), uploaded_at=MON_EVENING,
                             doc_number=doc) for doc in ("126000081", "126000082")]
    pg.execute("UPDATE desadv_sent SET import_checked_at = now() WHERE id = %s", (ids[0],))
    posts = PostRecorder()
    confirm.sweep(pg, _cfg(),
                  listdir=lambda: _dirs(in_dl=[_wire("126000082", SUP_A)],
                                        arch=[_wire("126000081", SUP_A)]),
                  post=posts, now=TUE_MORNING)
    html = posts.calls[0][0]
    assert "1 dodací list je stále neprevzatý" in html, html
    assert "126000082" in html and "126000081" not in html


def test_a_failing_name_or_codex_lookup_never_loses_the_alert(pg):
    """#476 review: the detail is fail-open end to end — a DB error in the supplier-name or
    the CODEX-list lookup (a REAL failure here: the tables are renamed away) only drops the
    names / the code lines; the alert itself still goes out, listed by number."""
    _suppliers(pg)
    _codex(pg, as_of=TUE_MORNING - timedelta(hours=1))
    _insert_desadv_at(pg, SUP_A, _fname("126000071", SUP_A), uploaded_at=MON_EVENING,
                      doc_number="126000071")
    reader = Reader({_wire("126000071", SUP_A): _desadv_content("126000071", [DEAD])})
    posts = PostRecorder()
    pg.execute("ALTER TABLE codex_card_syncs RENAME TO codex_card_syncs_476")
    pg.execute("ALTER TABLE dl_supplier_overrides RENAME TO dl_supplier_overrides_476")
    try:
        confirm.sweep(pg, _cfg(), listdir=lambda: _dirs(in_dl=list(reader.files)),
                      post=posts, now=TUE_MORNING, read_files=reader)
    finally:
        pg.execute("ALTER TABLE codex_card_syncs_476 RENAME TO codex_card_syncs")
        pg.execute("ALTER TABLE dl_supplier_overrides_476 RENAME TO dl_supplier_overrides")
    assert len(posts.calls) == 1, "the alert is never lost to a detail lookup"
    html = posts.calls[0][0]
    assert "<li>DL 126000071</li>" in html, html
    assert reader.calls == [], "no ORION read when the CODEX list cannot even be loaded"


def test_the_code_line_says_how_old_the_codex_card_list_is(pg):
    """A card created in CODEX after the last push is not in our list yet — the alert says
    as of when the list is, so „neexistuje" is never read as final (review nit)."""
    _suppliers(pg)
    _codex(pg, as_of=datetime(2026, 8, 4, 8, 10, tzinfo=TZ))
    reader = Reader({_wire("126000091", SUP_A): _desadv_content("126000091", [DEAD])})
    _insert_desadv_at(pg, SUP_A, _fname("126000091", SUP_A), uploaded_at=MON_EVENING,
                      doc_number="126000091")
    posts = PostRecorder()
    confirm.sweep(pg, _cfg(), listdir=lambda: _dirs(in_dl=list(reader.files)), post=posts,
                  now=TUE_MORNING, read_files=reader)
    html = posts.calls[0][0]
    assert "Zoznam kariet z CODEXu je aktuálny k 4.8. 08:10" in html, html


def test_no_codex_note_when_every_code_is_known(pg):
    _suppliers(pg)
    _codex(pg, as_of=TUE_MORNING - timedelta(hours=1))
    reader = Reader({_wire("126000092", SUP_A): _desadv_content("126000092", [GOOD_A])})
    _insert_desadv_at(pg, SUP_A, _fname("126000092", SUP_A), uploaded_at=MON_EVENING,
                      doc_number="126000092")
    posts = PostRecorder()
    confirm.sweep(pg, _cfg(), listdir=lambda: _dirs(in_dl=list(reader.files)), post=posts,
                  now=TUE_MORNING, read_files=reader)
    html = posts.calls[0][0]
    assert reader.calls, "the file WAS checked"
    assert "neprevezme" not in html and "Zoznam kariet" not in html, html


def test_the_production_reader_reads_the_waiting_file_from_in_dl_read_only(pg):
    """The default `read_files` (no injection) reads `<orion_dl_dir>\\Z-<file>` over SFTP in
    "r" mode — paramiko faked, the one external boundary."""
    _suppliers(pg)
    _codex(pg, as_of=TUE_MORNING - timedelta(hours=1))
    _insert_desadv_at(pg, SUP_A, _fname("126000093", SUP_A), uploaded_at=MON_EVENING,
                      doc_number="126000093")
    wire = _wire("126000093", SUP_A)
    handle = MagicMock()
    handle.__enter__.return_value.read.return_value = \
        _desadv_content("126000093", [DEAD]).encode("latin-1")
    fake_sftp = MagicMock()
    fake_sftp.open.return_value = handle
    fake_client = MagicMock()
    fake_client.open_sftp.return_value = fake_sftp
    dl_dir = "C:\\ORION\\TEST\\in_DL"
    posts = PostRecorder()
    with patch("paramiko.SSHClient", return_value=fake_client):
        confirm.sweep(pg, _cfg(orion_host="192.168.1.10", orion_dl_dir=dl_dir),
                      listdir=lambda: _dirs(in_dl=[wire]), post=posts, now=TUE_MORNING)
    fake_sftp.open.assert_called_once_with(f"{dl_dir}\\{wire}", "r")
    for forbidden in ("file", "put", "rename", "remove"):
        getattr(fake_sftp, forbidden).assert_not_called()
    assert f"kód {DEAD} v CODEXe neexistuje" in posts.calls[0][0]


# --- the pieces --------------------------------------------------------------------------

def test_lin_codes_reads_back_exactly_what_generate_wrote():
    content = _desadv_content("126000061", [GOOD_A, DEAD, GOOD_A, GOOD_B])
    assert desadv_edi.lin_codes(content) == [GOOD_A, DEAD, GOOD_B], \
        "every LIN code once, in file order; the HDR is never mistaken for a line"


def test_lin_codes_tolerates_a_bare_newline_file_and_blank_lines():
    lin = "LIN" + "1".rjust(6) + DEAD.ljust(13) + " " * 190
    assert desadv_edi.lin_codes(f"HDR{'x' * 50}\n{lin}\n\n") == [DEAD]


def test_head_wording_agrees_with_the_count():
    since = datetime(2026, 8, 3, 18, 3, tzinfo=TZ)
    same_day = datetime(2026, 8, 3, 20, 0, tzinfo=TZ)
    later = datetime(2026, 8, 5, 11, 0, tzinfo=TZ)
    assert "1 dodací list je stále neprevzatý v ORIONe — treba ho prijať" in \
        confirm._carryover_head(1, "desadv", None, later)
    assert "5 dodacích listov je stále neprevzatých" in \
        confirm._carryover_head(5, "desadv", None, later)
    assert "2 objednávky sú stále neprevzaté v ORIONe — treba ich prijať" in \
        confirm._carryover_head(2, "edi", None, later)
    assert "Stále 1 objednávka neprevzatá v ORIONe (od 18:03) — treba ju prijať" in \
        confirm._carryover_head(1, "edi", since, same_day)
    assert "(od 3.8. 18:03)" in confirm._carryover_head(3, "desadv", since, later), \
        "an older incident names its day, not just a bare time"


def test_read_files_only_ever_opens_for_reading_and_skips_a_vanished_file():
    base = "C:\\ORION\\COMMUNICATOR\\data\\in_DL"
    handle = MagicMock()
    handle.__enter__.return_value.read.return_value = "LIN     1 4711\r\n".encode("latin-1")
    fake_sftp = MagicMock()
    fake_sftp.open.side_effect = [handle, FileNotFoundError("gone")]
    fake_client = MagicMock()
    fake_client.open_sftp.return_value = fake_sftp
    cfg = Config(orion_host="192.168.1.10", orion_user="u", orion_pass="p")
    with patch("paramiko.SSHClient", return_value=fake_client):
        got = upload.read_files(cfg, base, ["Z-a.txt", "Z-b.txt"])
    assert got == {"Z-a.txt": "LIN     1 4711\r\n"}
    assert [c.args for c in fake_sftp.open.call_args_list] == [
        (f"{base}\\Z-a.txt", "r"), (f"{base}\\Z-b.txt", "r")]
    for forbidden in ("file", "put", "rename", "remove", "unlink", "mkdir", "rmdir"):
        getattr(fake_sftp, forbidden).assert_not_called()
    fake_sftp.close.assert_called_once()
    fake_client.close.assert_called_once()


def test_failed_and_unknown_heads_agree_with_the_count():
    one = [{"id": 1}]
    two = [{"id": 1}, {"id": 2}]
    five = [{"id": i} for i in range(5)]
    assert "1 dodací list skončil v priečinku" in confirm._group_html("failed", one, "desadv")
    assert "1 objednávka skončila v priečinku" in confirm._group_html("failed", one, "edi")
    assert "2 objednávky skončili v priečinku" in confirm._group_html("failed", two, "edi")
    assert "1 dodací list zmizol zo všetkých" in confirm._group_html("unknown", one, "desadv")
    assert "2 dodacie listy zmizli zo všetkých" in \
        confirm._group_html("unknown", two, "desadv")
    assert "5 objednávok zmizlo zo všetkých" in confirm._group_html("unknown", five, "edi")


def test_capped_list_remainder_agrees_with_the_count():
    items = [f"<li>{i}</li>" for i in range(20)]
    assert report.capped_list(items[:15], 15, "ďalší", "ďalšie", "ďalších") == \
        "<ul>" + "".join(items[:15]) + "</ul>"
    assert report.capped_list(items[:16], 15, "ďalší", "ďalšie", "ďalších").endswith(
        "<p>&#8230; a ešte 1 ďalší.</p>")
    assert report.capped_list(items[:18], 15, "ďalšia", "ďalšie", "ďalších").endswith(
        "<p>&#8230; a ešte 3 ďalšie.</p>")
    assert report.capped_list(items, 15, "ďalšia", "ďalšie", "ďalších").endswith(
        "<p>&#8230; a ešte 5 ďalších.</p>")


def test_the_stale_question_reminder_uses_the_agreeing_remainder():
    rows = [{"id": i, "kind": "dl_supplier", "customer_ean": "", "customer_name": "",
             "wording": f"dodavatel{i}@example.sk", "item_key": "", "context": {},
             "payload": {}, "created_at": datetime(2026, 8, 3, 9, i, tzinfo=TZ),
             "reminder_sent_at": None} for i in range(16)]
    html = question_alerts._group_html(rows, {}, 2, "")
    assert html.count("<li>") == 15
    assert "a ešte 1 ďalšia." in html, html


# --- review round 2 pins -----------------------------------------------------------------

def test_the_widening_leaves_out_a_file_simply_waiting_for_its_first_morning(pg):
    """The widening must keep the carryover rule: a file uploaded THIS morning, still before
    its first import chance, is normal waiting — never listed (that is the #133 false alarm)."""
    _suppliers(pg)
    _insert_desadv_at(pg, SUP_A, _fname("126000101", SUP_A), uploaded_at=MON_EVENING,
                      doc_number="126000101")
    fresh = _insert_desadv_at(pg, SUP_A, _fname("126000102", SUP_A),
                              uploaded_at=TUE_MORNING.replace(hour=10, minute=30),
                              doc_number="126000102")
    pg.execute("UPDATE desadv_sent SET import_checked_at = now() WHERE id = %s", (fresh,))
    posts = PostRecorder()
    confirm.sweep(pg, _cfg(),
                  listdir=lambda: _dirs(in_dl=[_wire("126000101", SUP_A),
                                               _wire("126000102", SUP_A)]),
                  post=posts, now=TUE_MORNING)
    html = posts.calls[0][0]
    assert "1 dodací list je stále neprevzatý" in html, html
    assert "126000102" not in html
    members = pg.execute("SELECT count(*) FROM import_alert_incident_desadv_members"
                         ).fetchone()[0]
    assert members == 1


def test_the_widening_never_crosses_into_another_channel(pg):
    """A waiting row whose file routes to a DIFFERENT channel (a non-DESADV_ name → the
    orders channel) is never pulled into the delivery-notes alert."""
    _suppliers(pg)
    _insert_desadv_at(pg, SUP_A, _fname("126000111", SUP_A), uploaded_at=MON_EVENING,
                      doc_number="126000111")
    other = _insert_desadv_at(pg, SUP_A, "LEGACY_126000112.txt", uploaded_at=MON_EVENING,
                              doc_number="126000112")
    pg.execute("UPDATE desadv_sent SET import_checked_at = now() WHERE id = %s", (other,))
    posts = PostRecorder()
    confirm.sweep(pg, _cfg(),
                  listdir=lambda: _dirs(in_dl=[_wire("126000111", SUP_A),
                                               "Z-LEGACY_126000112.txt"]),
                  post=posts, now=TUE_MORNING)
    assert [c for _h, c in posts.calls] == [243]
    assert "126000111" in posts.calls[0][0] and "126000112" not in posts.calls[0][0]


def test_reminder_od_is_the_oldest_upload_among_the_waiting_files(pg):
    _suppliers(pg)
    for doc, at in (("126000121", MON_EVENING.replace(hour=9, minute=15)),
                    ("126000122", MON_EVENING)):
        _insert_desadv_at(pg, SUP_A, _fname(doc, SUP_A), uploaded_at=at, doc_number=doc)
    wires = [_wire("126000121", SUP_A), _wire("126000122", SUP_A)]
    posts = PostRecorder()
    confirm.sweep(pg, _cfg(), listdir=lambda: _dirs(in_dl=wires), post=posts,
                  now=TUE_MORNING)
    _make_due(pg)
    confirm.sweep(pg, _cfg(), listdir=lambda: _dirs(in_dl=wires), post=posts,
                  now=TUE_MORNING.replace(hour=16))
    assert len(posts.calls) == 2
    assert "Stále 2 dodacie listy neprevzaté v ORIONe (od 3.8. 09:15)" in posts.calls[1][0]


def test_lin_codes_never_invents_a_code_from_a_utf8_byte_read_as_latin1():
    """`put()` writes the text as UTF-8 and `read_files` decodes latin-1: „Å" becomes
    „Ã\\x85", and `str.splitlines()` treats \\x85 as a line break — a HDR tail could turn into a
    fake „LIN" code. The parser splits on the file's own line ends only."""
    content = desadv_edi.generate(
        {"customerEanEdi": SUP_A, "customerName": "ÅLINDT s.r.o.", "docNumber": "1",
         "deliveryDate": "03.08.2026",
         "items": [{"gtin": GOOD_A, "name": "Rožok", "supplierName": "Rožok",
                    "quantity": 1, "unit": "ks", "unitPrice": 1.0}]}, {}, {}).content
    as_read = content.encode("utf-8").decode("latin-1")
    assert desadv_edi.lin_codes(as_read) == [GOOD_A]


def test_the_carryover_alert_spells_codex_like_every_other_message(pg):
    """One spelling in one alert — „CODEX"/„v CODEXe" (the #467 messages, the code line)."""
    _suppliers(pg)
    rid = _insert_desadv_at(pg, SUP_A, _fname("126000131", SUP_A), uploaded_at=MON_EVENING,
                            doc_number="126000131")
    posts = PostRecorder()
    w = _wire("126000131", SUP_A)
    confirm.sweep(pg, _cfg(), listdir=lambda: _dirs(in_dl=[w]), post=posts, now=TUE_MORNING)
    assert "treba ho prijať v CODEXe" in posts.calls[0][0], posts.calls[0][0]
    pg.execute("UPDATE desadv_sent SET import_checked_at = now() - interval '10 minutes' "
               "WHERE id = %s", (rid,))
    confirm.sweep(pg, _cfg(), listdir=lambda: _dirs(arch=[w]), post=posts,
                  now=TUE_MORNING.replace(hour=12))
    assert "prijaté v CODEXe" in posts.calls[1][0], posts.calls[1][0]
    assert "Codex" not in "".join(h for h, _c in posts.calls)


def test_the_grouped_ops_alert_remainder_agrees_with_the_count(pg):
    """`dl_alerts._format_grouped` groups ONE kind per post (every header noun is masculine:
    e-maily, dodacie listy, skeny) — its remainder goes through the same agreeing helper."""
    for i in range(11):
        dl_alerts.enqueue(pg, 592, "human_processing_review",
                          dl_alerts.item_line(f"odosielatel{i}@x.test", f"Predmet {i}"),
                          message_id=f"m476-{i}")
    class Cfg:
        dashboard_base_url = ""
        ops_channel_id = 592

    posted: list[str] = []
    dl_alerts.flush_pending(pg, Cfg(), post=lambda c, h, **kw: posted.append(h) or {"id": 1})
    assert "a ešte 1 ďalší." in posted[0], posted[0]


# --- review round 3 pins -----------------------------------------------------------------

def test_the_widening_never_pulls_a_waiting_file_into_a_failed_group(pg):
    """Only CARRYOVER groups are widened. A failed/unknown group marks its rows TERMINAL — a
    still-waiting DL pulled into it would be stamped `failed` and never self-heal to imported
    (review round 3, probe-reproduced with the kind filter removed)."""
    _suppliers(pg)
    bad = _insert_desadv_at(pg, SUP_A, _fname("126000141", SUP_A), uploaded_at=MON_EVENING,
                            doc_number="126000141")
    waiting = _insert_desadv_at(pg, SUP_A, _fname("126000142", SUP_A),
                                uploaded_at=MON_EVENING, doc_number="126000142")
    pg.execute("UPDATE desadv_sent SET import_checked_at = now() WHERE id = %s", (waiting,))
    posts = PostRecorder()
    confirm.sweep(pg, _cfg(),
                  listdir=lambda: {"in": set(), "in_DL": {_wire("126000142", SUP_A)},
                                   "archCodex": set(),
                                   "unconfirmed": {_wire("126000141", SUP_A)}},
                  post=posts, now=TUE_MORNING)
    assert len(posts.calls) == 1, [h for h, _c in posts.calls]
    assert "1 dodací list skončil v priečinku" in posts.calls[0][0], posts.calls[0][0]
    assert _desadv_status(pg, bad) == "failed"
    assert _desadv_status(pg, waiting) is None, "a waiting file must never be marked terminal"


def test_the_widened_list_is_in_upload_order(pg):
    """The due row that triggers the group can be NEWER than a waiting row the widening adds —
    the list still reads oldest upload first."""
    _suppliers(pg)
    older = _insert_desadv_at(pg, SUP_A, _fname("126000151", SUP_A),
                              uploaded_at=MON_EVENING.replace(hour=8),
                              doc_number="126000151")
    _insert_desadv_at(pg, SUP_A, _fname("126000152", SUP_A), uploaded_at=MON_EVENING,
                      doc_number="126000152")
    pg.execute("UPDATE desadv_sent SET import_checked_at = now() WHERE id = %s", (older,))
    posts = PostRecorder()
    confirm.sweep(pg, _cfg(),
                  listdir=lambda: _dirs(in_dl=[_wire("126000151", SUP_A),
                                               _wire("126000152", SUP_A)]),
                  post=posts, now=TUE_MORNING)
    html = posts.calls[0][0]
    assert html.index("126000151") < html.index("126000152"), html
