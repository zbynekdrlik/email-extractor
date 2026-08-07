"""DESADV upload ledger (#200 F1) — the DL counterpart of edi_sent (test_orders_edi.py),
with a different identity: (supplier_ean, doc_number), no content hash. Mirrors that
file's ledger tests, plus the W4 fix this ledger exists to prove (two different
suppliers reusing the same short doc number must never collide).
"""
from app.orders import desadv


def test_the_same_document_is_only_ever_sent_once(pg):
    first = desadv.claim_send(pg, "8586013743063", "0100000001", "f1.txt")
    second = desadv.claim_send(pg, "8586013743063", "0100000001", "f2.txt")
    assert first is True
    assert second is False, "a duplicate upload would create a duplicate order in ORION"
    assert pg.execute("SELECT count(*) FROM desadv_sent").fetchone()[0] == 1


def test_claim_send_refuses_an_empty_doc_number(pg):
    """Review finding on #200's PR: this ledger has no content hash, so an empty
    doc_number would collapse EVERY numberless document from one supplier onto one
    ledger row — the second one silently lost, exactly the W4 failure the ledger
    exists to fix."""
    assert desadv.claim_send(pg, "8586013743063", "", "f1.txt") is False
    assert pg.execute("SELECT count(*) FROM desadv_sent").fetchone()[0] == 0


def test_claim_send_refuses_an_empty_supplier_ean(pg):
    assert desadv.claim_send(pg, "", "0100000001", "f1.txt") is False
    assert pg.execute("SELECT count(*) FROM desadv_sent").fetchone()[0] == 0


def test_confirm_sent_does_not_raise_when_the_claim_is_already_gone(pg):
    """The claim can legitimately vanish underneath (released, or reclaimed by
    another worker) — confirm_sent must warn, not crash, since the document IS
    already physically uploaded either way (review finding on #200's PR)."""
    desadv.claim_send(pg, "157", "0100000157", "f1.txt")
    desadv.release_send(pg, "157", "0100000157")
    desadv.confirm_sent(pg, "157", "0100000157")   # must not raise
    assert pg.execute("SELECT count(*) FROM desadv_sent").fetchone()[0] == 0


def test_the_ledger_remembers_which_file_carried_which_claim(pg):
    desadv.claim_send(pg, "8586013743063", "0100000001", "DESADV_x.txt")
    row = pg.execute("SELECT filename FROM desadv_sent").fetchone()
    assert row[0] == "DESADV_x.txt"


def test_a_failed_upload_is_released_so_it_can_be_retried(pg):
    """The ledger is claimed BEFORE the upload; if the upload fails the claim must go,
    or the document could never be sent again."""
    assert desadv.claim_send(pg, "111", "0100000099", "f1.txt") is True
    desadv.release_send(pg, "111", "0100000099")
    assert desadv.claim_send(pg, "111", "0100000099", "f1.txt") is True


# --- W4: bare doc-number collision across suppliers — this is the whole reason the
# identity is (supplier_ean, doc_number) and not doc_number alone (the n8n bug). ---

def test_two_different_suppliers_may_share_the_same_short_doc_number(pg):
    """Real n8n incident class (R90/W4): a short doc number like '68944' can be issued
    independently by two unrelated suppliers. The old registry (keyed on doc_number
    alone) would read the second as a duplicate and silently drop a real delivery."""
    assert desadv.claim_send(pg, "supplier-A-ean", "68944", "a.txt") is True
    assert desadv.claim_send(pg, "supplier-B-ean", "68944", "b.txt") is True
    assert pg.execute("SELECT count(*) FROM desadv_sent").fetchone()[0] == 2


def test_the_same_supplier_reusing_a_doc_number_still_dedupes(pg):
    """The fix scopes by supplier, it does not disable dedup — the SAME supplier
    re-announcing the SAME doc number is still exactly the duplicate-skip case R32
    describes (e.g. a re-announcement mail)."""
    assert desadv.claim_send(pg, "supplier-A-ean", "68944", "a.txt") is True
    assert desadv.claim_send(pg, "supplier-A-ean", "68944", "a2.txt") is False


# --- #153-style two-phase claim/confirm, from inception (W2/W3) -----------------

def test_an_orphaned_stale_claim_is_reclaimed_not_silently_skipped(pg):
    """The run that made this claim died before ever uploading or releasing it. A
    claim older than the freshness window and never confirmed must be reclaimable —
    exactly the fix edi_sent needed retrofitted after 13 real orders were lost."""
    assert desadv.claim_send(pg, "153", "0100000153", "f1.txt") is True
    pg.execute("UPDATE desadv_sent SET sent_at = now() - interval '11 minutes' "
              "WHERE supplier_ean = '153'")
    assert desadv.claim_send(pg, "153", "0100000153", "f2.txt") is True, \
        "a stale, never-confirmed claim must be reclaimed — never silently skipped"
    assert pg.execute(
        "SELECT count(*) FROM desadv_sent WHERE supplier_ean = '153'").fetchone()[0] == 1, \
        "reclaim updates the SAME row — never inserts a duplicate"
    assert pg.execute(
        "SELECT filename FROM desadv_sent WHERE supplier_ean = '153'").fetchone()[0] == "f2.txt"


def test_a_confirmed_upload_still_blocks_a_duplicate_no_matter_how_old(pg):
    assert desadv.claim_send(pg, "154", "0100000154", "f1.txt") is True
    desadv.confirm_sent(pg, "154", "0100000154")
    pg.execute("UPDATE desadv_sent SET sent_at = now() - interval '1 year' "
              "WHERE supplier_ean = '154'")
    assert desadv.claim_send(pg, "154", "0100000154", "f2.txt") is False, \
        "a CONFIRMED upload blocks a duplicate regardless of age"


def test_a_fresh_unconfirmed_claim_still_blocks_a_concurrent_duplicate(pg):
    """Inside the freshness window another worker may genuinely be mid-upload right
    now — reclaiming it would race a real upload in flight."""
    assert desadv.claim_send(pg, "155", "0100000155", "f1.txt") is True
    assert desadv.claim_send(pg, "155", "0100000155", "f2.txt") is False, \
        "a fresh, still-unconfirmed claim must not be reclaimed"


def test_confirm_sent_stamps_the_upload_so_it_is_never_reclaimed(pg):
    desadv.claim_send(pg, "156", "0100000156", "f1.txt")
    row = pg.execute(
        "SELECT uploaded_at FROM desadv_sent WHERE supplier_ean = '156'").fetchone()
    assert row[0] is None, "claimed but not yet confirmed"
    desadv.confirm_sent(pg, "156", "0100000156")
    row = pg.execute(
        "SELECT uploaded_at FROM desadv_sent WHERE supplier_ean = '156'").fetchone()
    assert row[0] is not None


# --- already_sent (#204, DL migration F5): the READ-ONLY shadow-mode counterpart --

def test_already_sent_is_false_for_an_unknown_document(pg):
    assert desadv.already_sent(pg, "200", "0100000200") is False


def test_already_sent_is_true_only_after_confirm(pg):
    desadv.claim_send(pg, "201", "0100000201", "f1.txt")
    assert desadv.already_sent(pg, "201", "0100000201") is False, \
        "a bare claim is not yet a genuine upload — a live claim racing a shadow peek " \
        "must not report a false duplicate"
    desadv.confirm_sent(pg, "201", "0100000201")
    assert desadv.already_sent(pg, "201", "0100000201") is True


def test_already_sent_never_writes_anything(pg):
    """The whole point: shadow must never INSERT/UPDATE the ledger."""
    desadv.already_sent(pg, "202", "0100000202")
    assert pg.execute(
        "SELECT count(*) FROM desadv_sent WHERE supplier_ean = '202'").fetchone()[0] == 0


def test_already_sent_refuses_empty_identity(pg):
    assert desadv.already_sent(pg, "", "0100000203") is False
    assert desadv.already_sent(pg, "203", "") is False
