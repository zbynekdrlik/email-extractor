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
        if case.get("divergence"):
            continue          # asserted separately below, with the reason
        assert got.content == case["expected"]["content"], case["name"]
        assert got.line_count == case["expected"]["lineCount"], case["name"]
        assert got.skipped == case["expected"]["skipped"], case["name"]


def test_the_one_deliberate_divergence_is_strict_ascii():
    """The production generator maps only the Slovak letters, so anything outside its
    table (an en dash in a customer name) reaches the fixed-width file as multi-byte
    UTF-8 — a latent column shift for a byte-oriented reader. We fold to strict ASCII.

    The assertion on the reference bytes is deliberate: it proves the divergence is real
    and would fail if production ever fixed it, at which point this test goes away.
    """
    case = next(c for c in FIXTURE if c.get("divergence"))
    reference = case["expected"]["content"]
    assert not reference.isascii(), "reference no longer diverges — drop this test"
    got = edi.build(**case["input"])
    assert got.content.isascii()
    assert len(got.content.split("\r\n")[0]) == 1157, "the layout is unchanged"
    assert got.line_count == case["expected"]["lineCount"]


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


# --- #153: claim vs. confirmed upload — an orphaned claim must be reclaimed, never a
# silent permanent skip. 13 real orders were lost this way on 2026-08-03 (a crash between
# `claim_send` and `upload` that never reached `release_send`). ---

def test_an_orphaned_stale_claim_is_reclaimed_not_silently_skipped(pg):
    """The run that made this claim died before ever uploading or releasing it (killed
    process, restart, anything outside the `except` around `upload()`). The old code
    read the surviving row as "already sent" and lost the order forever. A claim older
    than the freshness window and never confirmed must be reclaimable."""
    built = edi.build(**FIXTURE[0]["input"])
    assert edi.claim_send(pg, "153", "04.08.2026", built.content, "f1.txt") is True
    # simulate the run dying: nothing released, nothing confirmed, just old
    pg.execute("UPDATE edi_sent SET sent_at = now() - interval '11 minutes' "
              "WHERE customer_ean = '153'")
    assert edi.claim_send(pg, "153", "04.08.2026", built.content, "f2.txt") is True, \
        "a stale, never-confirmed claim must be reclaimed — never silently skipped"
    assert pg.execute(
        "SELECT count(*) FROM edi_sent WHERE customer_ean = '153'").fetchone()[0] == 1, \
        "reclaim updates the SAME row — never inserts a duplicate"
    assert pg.execute(
        "SELECT filename FROM edi_sent WHERE customer_ean = '153'").fetchone()[0] == "f2.txt"


def test_a_confirmed_upload_still_blocks_a_duplicate_no_matter_how_old(pg):
    """A CONFIRMED send is a real, physical delivery — it must block a duplicate
    forever, unlike a bare claim which is only provisional until confirmed."""
    built = edi.build(**FIXTURE[0]["input"])
    assert edi.claim_send(pg, "154", "04.08.2026", built.content, "f1.txt") is True
    edi.confirm_sent(pg, "154", "04.08.2026", built.content)
    pg.execute("UPDATE edi_sent SET sent_at = now() - interval '1 year' "
              "WHERE customer_ean = '154'")
    assert edi.claim_send(pg, "154", "04.08.2026", built.content, "f2.txt") is False, \
        "a CONFIRMED upload blocks a duplicate regardless of age"


def test_a_fresh_unconfirmed_claim_still_blocks_a_concurrent_duplicate(pg):
    """Inside the freshness window another worker may genuinely be mid-upload right
    now — reclaiming it would race a real upload in flight."""
    built = edi.build(**FIXTURE[0]["input"])
    assert edi.claim_send(pg, "155", "04.08.2026", built.content, "f1.txt") is True
    assert edi.claim_send(pg, "155", "04.08.2026", built.content, "f2.txt") is False, \
        "a fresh, still-unconfirmed claim must not be reclaimed"


# --- claim_send_or_identify (#271: built on the shared `claim.claim_or_identify()`
# primitive, mirrors desadv.py's own #216 test coverage for the identical CTE shape) --

def test_claim_send_or_identify_claims_a_fresh_document(pg):
    built = edi.build(**FIXTURE[0]["input"])
    claimed, confirmed = edi.claim_send_or_identify(
        pg, "220", "04.08.2026", built.content, "f1.txt")
    assert (claimed, confirmed) == (True, False)
    assert pg.execute("SELECT count(*) FROM edi_sent").fetchone()[0] == 1


def test_claim_send_or_identify_reports_confirmed_for_a_genuine_duplicate(pg):
    built = edi.build(**FIXTURE[0]["input"])
    edi.claim_send_or_identify(pg, "221", "04.08.2026", built.content, "f1.txt")
    edi.confirm_sent(pg, "221", "04.08.2026", built.content)
    claimed, confirmed = edi.claim_send_or_identify(
        pg, "221", "04.08.2026", built.content, "f2.txt")
    assert (claimed, confirmed) == (False, True), \
        "a CONFIRMED upload must be reported as a genuine duplicate"


def test_claim_send_or_identify_reports_unconfirmed_for_a_fresh_concurrent_claim(pg):
    """#271: the whole point of this function — a fresh, UNCONFIRMED claim held by
    another concurrent run is NOT the same situation as a confirmed duplicate, and the
    caller must be told apart which one it is."""
    built = edi.build(**FIXTURE[0]["input"])
    edi.claim_send_or_identify(pg, "222", "04.08.2026", built.content, "f1.txt")
    claimed, confirmed = edi.claim_send_or_identify(
        pg, "222", "04.08.2026", built.content, "f2.txt")
    assert (claimed, confirmed) == (False, False), \
        "an unconfirmed claim must be reported as such, never as 'already sent'"


def test_claim_send_or_identify_reclaims_a_stale_unconfirmed_claim(pg):
    built = edi.build(**FIXTURE[0]["input"])
    edi.claim_send_or_identify(pg, "223", "04.08.2026", built.content, "f1.txt")
    pg.execute("UPDATE edi_sent SET sent_at = now() - interval '11 minutes' "
              "WHERE customer_ean = '223'")
    claimed, confirmed = edi.claim_send_or_identify(
        pg, "223", "04.08.2026", built.content, "f2.txt")
    assert (claimed, confirmed) == (True, False)
    row = pg.execute(
        "SELECT filename, count(*) OVER () FROM edi_sent WHERE customer_ean = '223'"
    ).fetchone()
    assert row == ("f2.txt", 1), "reclaim updates the SAME row — never inserts a duplicate"


def test_claim_send_or_identify_behaves_like_claim_send_for_a_blank_identity(pg):
    """`edi.claim_send()` has no empty-identity guard (unlike `desadv.claim_send`) —
    `edi_sent`'s uniqueness includes the content hash, so a blank customer_ean/
    delivery_date does not collapse distinct documents onto one row the way an
    empty `desadv_sent.doc_number` would. `claim_send_or_identify` must behave the
    SAME way (a new, deliberately consistent sibling), not add its own guard."""
    claimed, confirmed = edi.claim_send_or_identify(pg, "", "", "content-a", "f1.txt")
    assert (claimed, confirmed) == (True, False)
    assert pg.execute("SELECT count(*) FROM edi_sent").fetchone()[0] == 1


def test_claim_send_or_identify_never_double_inserts(pg):
    built = edi.build(**FIXTURE[0]["input"])
    edi.claim_send_or_identify(pg, "224", "04.08.2026", built.content, "f1.txt")
    edi.claim_send_or_identify(pg, "224", "04.08.2026", built.content, "f2.txt")
    assert pg.execute("SELECT count(*) FROM edi_sent").fetchone()[0] == 1


def test_confirm_sent_stamps_the_upload_so_it_is_never_reclaimed(pg):
    built = edi.build(**FIXTURE[0]["input"])
    edi.claim_send(pg, "156", "04.08.2026", built.content, "f1.txt")
    row = pg.execute(
        "SELECT uploaded_at FROM edi_sent WHERE customer_ean = '156'").fetchone()
    assert row[0] is None, "claimed but not yet confirmed"
    edi.confirm_sent(pg, "156", "04.08.2026", built.content)
    row = pg.execute(
        "SELECT uploaded_at FROM edi_sent WHERE customer_ean = '156'").fetchone()
    assert row[0] is not None


def test_pre_migration_rows_are_backfilled_as_confirmed_not_left_as_orphans(pg, reapply_schema):
    """Every row that existed before `uploaded_at` was added was written by the OLD
    one-phase code (claim immediately followed by upload) — it must become CONFIRMED
    the moment the column is added, never a 'reclaimable orphan' that would trigger a
    duplicate delivery once the 10-minute window passes."""
    pg.execute("ALTER TABLE edi_sent DROP COLUMN uploaded_at")
    pg.execute(
        """INSERT INTO edi_sent (customer_ean, delivery_date, content_sha256, filename,
                                 sent_at)
           VALUES ('157', '04.08.2026', 'deadbeef', 'historical.txt',
                   now() - interval '30 days')""")
    reapply_schema()     # re-run the migration: the column comes back, and so does the backfill
    row = pg.execute(
        "SELECT uploaded_at, sent_at FROM edi_sent WHERE customer_ean = '157'").fetchone()
    assert row[0] is not None and row[0] == row[1], \
        "a pre-existing row must be backfilled as confirmed, not left NULL/orphaned"


def test_the_document_date_is_injectable_so_parity_does_not_expire_overnight():
    """The HDR carries the document's creation date. Reading it from the clock made the
    byte-parity fixture pass only on the day it was recorded and fail every day after —
    a test that expires is not a test. `today` is therefore an argument."""
    case = FIXTURE[0]
    built = edi.build(**dict(case["input"], today="20991231"))
    assert "20991231" in built.content
    assert "20260730" not in built.content
    # and with no date given it still tracks the real clock
    from datetime import UTC, datetime
    live = {k: v for k, v in case["input"].items() if k != "today"}
    assert datetime.now(UTC).strftime("%Y%m%d") in edi.build(**live).content


def test_the_ledger_recognizes_the_same_order_built_on_a_different_day(pg):
    """The document carries its creation date, so the SAME order rebuilt after midnight
    produced different bytes — and a hash over the raw bytes let it through the ledger as
    if it had never been sent. That is the duplicate order in ORION (#51), one retry
    across midnight away. The ledger must key on the ORDER, not on the paperwork date."""
    case = FIXTURE[0]
    day1 = edi.build(**dict(case["input"], today="20260730"))
    day2 = edi.build(**dict(case["input"], today="20260731"))
    assert day1.content != day2.content, "different paperwork date, as expected"

    ean, delivery = case["input"]["ean"], case["input"]["deliveryDate"]
    assert edi.claim_send(pg, ean, delivery, day1.content, "a.txt") is True
    assert edi.claim_send(pg, ean, delivery, day2.content, "b.txt") is False, \
        "the same order must never be claimable twice"


def test_a_genuinely_different_order_is_still_claimable(pg):
    """The normalization must not go so far that two different orders collide."""
    case = FIXTURE[0]
    a = edi.build(**dict(case["input"], today="20260730"))
    other = dict(case["input"])
    other["items"] = [dict(other["items"][0], quantity=999)]
    b = edi.build(**dict(other, today="20260730"))
    ean, delivery = case["input"]["ean"], case["input"]["deliveryDate"]
    assert edi.claim_send(pg, ean, delivery, a.content, "a.txt") is True
    assert edi.claim_send(pg, ean, delivery, b.content, "b.txt") is True


def test_the_ledger_blanks_exactly_the_document_date_field():
    """Review finding: the hash blanks a fixed offset. Pin that the offset really is the
    creation date, so a layout change cannot silently blank a different column."""
    case = FIXTURE[0]
    built = edi.build(**dict(case["input"], today="20991231"))
    at, ln = edi.DOC_DATE_AT, edi.DOC_DATE_LEN
    assert built.content[at:at + ln] == "20991231"
