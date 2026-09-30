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
from app.orders import codex_cards, confirm, desadv_edi, dl_snapshot, snapshot, upload

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
    assert "Stále 1 dodací list" in html, html
    assert "4 dodac" not in html and "dodacie listy" not in html
    assert "126000004" in html and SUP_A_NAME in html, "the waiting DL + its supplier"
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
    assert f"a ešte {18 - confirm.LIST_LIMIT} ďalších" in html


# --- all imported -> no reminder, the incident closes ------------------------------------

def test_all_imported_sends_no_reminder_one_all_clear_and_closes_the_incident(pg):
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
    for doc in ("126000021", "126000022"):
        _insert_desadv_at(pg, SUP_A, _fname(doc, SUP_A), uploaded_at=MON_EVENING,
                          doc_number=doc)
    posts = PostRecorder()
    confirm.sweep(pg, _cfg(),
                  listdir=lambda: _dirs(in_dl=list(reader.files)),
                  post=posts, now=TUE_MORNING, read_files=reader)
    html = posts.calls[0][0]
    assert (f"DL 126000021 ({SUP_A_NAME}): CODEX ho neprevezme — kódy {DEAD}, 4712 v CODEXe "
            "neexistujú. Zadajte ho ručne.") in html, html
    assert f"<li>DL 126000022 ({SUP_A_NAME})</li>" in html
    assert html.index("126000021") < html.index("126000022"), \
        "a file CODEX will never take is listed first"


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
