"""EDI writer + upload ledger (#64).

ORION reads a fixed-width file; a shifted column is a wrong order, so the writer is
pinned **byte for byte** against `fixtures/edi_reference.json`. Those bytes were produced
by running the PRODUCTION n8n generator (the `ASSEMBLE AND GENERATE EDI` Code node) under
node — the fixture is parity with what the warehouse receives today, not with my reading
of the format.

The ledger is the other half: uploading the same order twice would create a duplicate
order in ORION (#51), so it is made impossible rather than unlikely.
"""
import json
from pathlib import Path

from app.orders import edi

FIXTURE = json.loads(
    (Path(__file__).parent / "fixtures" / "edi_reference.json").read_text(encoding="utf-8"))


# --- byte parity with the production generator ---------------------------

def test_the_writer_matches_the_production_generator_byte_for_byte():
    for case in FIXTURE:
        got = edi.build(**case["input"])
        assert got.content == case["expected"]["content"], case["name"]
        assert got.line_count == case["expected"]["lineCount"], case["name"]
        assert got.skipped == case["expected"]["skipped"], case["name"]


def test_the_header_is_exactly_1157_characters():
    got = edi.build(**FIXTURE[0]["input"])
    assert len(got.content.split("\r\n")[0]) == 1157


def test_lines_end_with_crlf_and_the_file_ends_with_sum():
    got = edi.build(**FIXTURE[0]["input"])
    assert "\r\n" in got.content and got.content.split("\r\n")[-1] == "SUM"


def test_an_unmatched_item_is_skipped_from_the_file_and_named():
    got = edi.build(**FIXTURE[1]["input"])
    assert got.skipped == ["neznámy produkt"]
    assert "neznámy" not in got.content.lower()


def test_diacritics_never_reach_the_file():
    got = edi.build(**FIXTURE[2]["input"])
    assert all(ord(c) < 128 for c in got.content), "ORION reads ASCII fixed-width"


# --- filename ------------------------------------------------------------

def test_the_filename_carries_customer_date_and_a_unique_stamp():
    name = edi.filename("2000000000864", "04.08.2026", "PO12345", stamp="123456789")
    assert name.startswith("ORDER_000864_PO12345_20260804_123456789")
    assert name.endswith(".txt")


def test_two_files_for_the_same_order_still_differ():
    a = edi.filename("2000000000864", "04.08.2026", "", stamp="000000001")
    b = edi.filename("2000000000864", "04.08.2026", "", stamp="000000002")
    assert a != b


def test_the_change_prefix_finds_the_original_upload():
    """A change request is corrected by hand in ORION, so the report has to tell the
    warehouse which file to look for."""
    assert edi.change_prefix("2000000000864", "04.08.2026", "") == "ORDER_000864_20260804_"


# --- the ledger ----------------------------------------------------------

def test_the_same_order_content_is_only_ever_sent_once(pg):
    built = edi.build(**FIXTURE[0]["input"])
    first = edi.claim_send(pg, "2000000000864", "04.08.2026", built.content, "f1.txt")
    second = edi.claim_send(pg, "2000000000864", "04.08.2026", built.content, "f2.txt")
    assert first is True
    assert second is False, "a duplicate upload would create a duplicate order in ORION"
    assert pg.execute("SELECT count(*) FROM edi_sent").fetchone()[0] == 1


def test_a_changed_order_for_the_same_day_is_a_new_send(pg):
    a = edi.build(**FIXTURE[0]["input"])
    changed = dict(FIXTURE[0]["input"])
    changed["items"] = changed["items"][:1]
    b = edi.build(**changed)
    assert edi.claim_send(pg, "2000000000864", "04.08.2026", a.content, "f1.txt") is True
    assert edi.claim_send(pg, "2000000000864", "04.08.2026", b.content, "f2.txt") is True


def test_the_ledger_remembers_which_file_carried_which_content(pg):
    built = edi.build(**FIXTURE[0]["input"])
    edi.claim_send(pg, "2000000000864", "04.08.2026", built.content, "ORDER_x.txt")
    row = pg.execute("SELECT filename, content_sha256 FROM edi_sent").fetchone()
    assert row[0] == "ORDER_x.txt" and len(row[1]) == 64


def test_a_failed_upload_is_released_so_it_can_be_retried(pg):
    """The ledger is claimed BEFORE the upload; if the upload fails the claim must go,
    or the order could never be sent again."""
    built = edi.build(**FIXTURE[0]["input"])
    assert edi.claim_send(pg, "111", "04.08.2026", built.content, "f1.txt") is True
    edi.release_send(pg, "111", "04.08.2026", built.content)
    assert edi.claim_send(pg, "111", "04.08.2026", built.content, "f1.txt") is True
