"""`app/orders/claim.py`'s reusable claim-or-identify primitive (#271).

Exercised directly against the real `edi_sent` table with HAND-WRITTEN SQL — not
through `edi.py`'s own wrapper — to prove the primitive is genuinely generic (its
correctness does not depend on any one caller's specific column shape), independent
of `edi.claim_send_or_identify`'s own tests in `test_orders_edi.py`.
"""
from app.orders import claim


def _insert_sql():
    return (
        "INSERT INTO edi_sent (customer_ean, delivery_date, content_sha256, filename) "
        "VALUES (%s, %s, %s, %s) "
        "ON CONFLICT (customer_ean, delivery_date, content_sha256) "
        "DO UPDATE SET sent_at = now(), filename = EXCLUDED.filename "
        "WHERE edi_sent.uploaded_at IS NULL "
        "AND edi_sent.sent_at < now() - make_interval(mins => %s) "
        "RETURNING filename")


_IDENTIFY_SQL = ("SELECT filename FROM edi_sent "
                 "WHERE customer_ean = %s AND delivery_date = %s AND content_sha256 = %s")


def _claim(pg, ean, date, chash, filename, stale_minutes=10):
    return claim.claim_or_identify(
        pg,
        insert_sql=_insert_sql(),
        insert_params=(ean, date, chash, filename, stale_minutes),
        identify_sql=_IDENTIFY_SQL,
        identify_params=(ean, date, chash))


def test_a_fresh_claim_is_taken_and_reports_the_just_inserted_returning_value(pg):
    claimed, info = _claim(pg, "300", "01.01.2026", "hash-a", "f1.txt")
    assert claimed is True
    # `RETURNING filename` on a fresh INSERT reports the value just written — this is
    # the "claimed=True reports whatever RETURNING produced" half of the contract
    # (edi.claim_send_or_identify deliberately uses RETURNING NULL instead, to keep
    # the claimed branch's tuple independent of the written value — this test proves
    # the primitive itself doesn't force that choice on every caller).
    assert info == ("f1.txt",)
    assert pg.execute("SELECT count(*) FROM edi_sent").fetchone()[0] == 1


def test_a_second_claim_of_the_same_identity_is_refused_and_identifies_the_holder(pg):
    _claim(pg, "301", "01.01.2026", "hash-a", "f1.txt")
    claimed, info = _claim(pg, "301", "01.01.2026", "hash-a", "f2.txt")
    assert claimed is False
    assert info == ("f1.txt",), "the identify branch reports the PRE-EXISTING holder"
    assert pg.execute("SELECT count(*) FROM edi_sent").fetchone()[0] == 1, \
        "the ins CTE's own WHERE refused the write — no second row, no overwrite"


def test_a_stale_claim_is_reclaimed_the_same_way_a_direct_call_would_be(pg):
    _claim(pg, "302", "01.01.2026", "hash-a", "f1.txt", stale_minutes=10)
    pg.execute("UPDATE edi_sent SET sent_at = now() - interval '11 minutes' "
              "WHERE customer_ean = '302'")
    claimed, info = _claim(pg, "302", "01.01.2026", "hash-a", "f2.txt", stale_minutes=10)
    assert claimed is True
    assert info == ("f2.txt",)
    assert pg.execute(
        "SELECT count(*) FROM edi_sent WHERE customer_ean = '302'").fetchone()[0] == 1, \
        "a reclaim updates the SAME row — never inserts a duplicate"


def test_a_different_identity_is_a_genuinely_separate_claim(pg):
    claimed_a, _ = _claim(pg, "303", "01.01.2026", "hash-a", "f1.txt")
    claimed_b, _ = _claim(pg, "304", "01.01.2026", "hash-a", "f2.txt")
    assert claimed_a is True
    assert claimed_b is True
    assert pg.execute("SELECT count(*) FROM edi_sent").fetchone()[0] == 2


def test_a_mismatched_insert_and_identify_target_reports_empty_never_raises(pg):
    """The primitive's own defensive branch (see its docstring): if `insert_sql`
    refuses (a real conflict its own WHERE won't update) but `identify_sql` was built
    against a DIFFERENT identity than the insert actually targeted — a caller bug, not
    a scenario a correctly-paired insert/identify can ever produce — the wrapper
    returns `(False, ())` rather than crashing on an unpacked `None`."""
    _claim(pg, "306", "01.01.2026", "hash-a", "f1.txt")
    pg.execute("UPDATE edi_sent SET uploaded_at = now() WHERE customer_ean = '306'")
    claimed, info = claim.claim_or_identify(
        pg,
        insert_sql=_insert_sql(),
        # Refused: uploaded_at IS NOT NULL now, so the ON CONFLICT ... WHERE fails.
        insert_params=("306", "01.01.2026", "hash-a", "f2.txt", 10),
        identify_sql=_IDENTIFY_SQL,
        # Deliberately mismatched — targets an identity that was never claimed.
        identify_params=("does-not-exist", "01.01.2026", "hash-a"))
    assert (claimed, info) == (False, ())
