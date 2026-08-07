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


# --- message_id / claimed_by (#216): tell a self-retry apart from a real duplicate --

def test_claim_send_records_which_message_made_the_claim(pg):
    desadv.claim_send(pg, "210", "0100000210", "f1.txt", message_id="msg-a")
    row = pg.execute(
        "SELECT message_id FROM desadv_sent WHERE supplier_ean = '210'").fetchone()
    assert row[0] == "msg-a"


def test_claim_send_with_no_message_id_leaves_it_null(pg):
    """Backward compatible: an existing caller that never passes message_id (or any
    future one) behaves exactly as before this column existed."""
    desadv.claim_send(pg, "211", "0100000211", "f1.txt")
    row = pg.execute(
        "SELECT message_id FROM desadv_sent WHERE supplier_ean = '211'").fetchone()
    assert row[0] is None


def test_claimed_by_returns_the_current_claimant(pg):
    desadv.claim_send(pg, "212", "0100000212", "f1.txt", message_id="msg-b")
    assert desadv.claimed_by(pg, "212", "0100000212") == "msg-b"


def test_claimed_by_returns_empty_for_no_row_at_all(pg):
    assert desadv.claimed_by(pg, "213", "0100000213") == ""


def test_claimed_by_returns_empty_for_a_legacy_row_with_no_message_id(pg):
    """A row that predates #216 (or was written by a caller that never passed
    message_id) must read back as "" — never confused with a real message_id, so it
    is always (safely) treated as an unknown/genuine duplicate, never a self-retry."""
    desadv.claim_send(pg, "214", "0100000214", "f1.txt")
    assert desadv.claimed_by(pg, "214", "0100000214") == ""


def test_claimed_by_refuses_empty_identity(pg):
    assert desadv.claimed_by(pg, "", "0100000215") == ""
    assert desadv.claimed_by(pg, "215", "") == ""


def test_reclaiming_a_stale_claim_updates_the_message_id_to_the_new_claimant(pg):
    """A stale, never-confirmed claim can be genuinely reclaimed by a DIFFERENT
    message — the recorded claimant must track whoever holds it NOW, not whoever
    claimed it first."""
    desadv.claim_send(pg, "216", "0100000216", "f1.txt", message_id="msg-old")
    pg.execute("UPDATE desadv_sent SET sent_at = now() - interval '11 minutes' "
              "WHERE supplier_ean = '216'")
    assert desadv.claim_send(pg, "216", "0100000216", "f2.txt",
                             message_id="msg-new") is True
    assert desadv.claimed_by(pg, "216", "0100000216") == "msg-new"


# --- claim_send_or_identify (#216, review finding: an atomic combination closes the
# TOCTOU gap a separate claim_send()+claimed_by() pair would otherwise leave) --------

def test_claim_send_or_identify_claims_a_fresh_document(pg):
    claimed, holder = desadv.claim_send_or_identify(pg, "220", "0100000220", "f1.txt",
                                                     message_id="msg-a")
    assert claimed is True
    assert holder == ""
    row = pg.execute(
        "SELECT message_id FROM desadv_sent WHERE supplier_ean = '220'").fetchone()
    assert row[0] == "msg-a"


def test_claim_send_or_identify_reports_the_same_message_as_the_current_holder(pg):
    """The exact #216 scenario: THIS message already shipped the document earlier —
    the refusal must come back tagged with the SAME message_id so the caller can
    recognise its own prior claim."""
    desadv.claim_send(pg, "221", "0100000221", "f1.txt", message_id="msg-b")
    desadv.confirm_sent(pg, "221", "0100000221")
    claimed, holder = desadv.claim_send_or_identify(pg, "221", "0100000221", "f2.txt",
                                                     message_id="msg-b")
    assert claimed is False
    assert holder == "msg-b"


def test_claim_send_or_identify_reports_a_different_message_as_the_current_holder(pg):
    """A genuine cross-message duplicate: a DIFFERENT message already shipped it."""
    desadv.claim_send(pg, "222", "0100000222", "f1.txt", message_id="msg-old")
    desadv.confirm_sent(pg, "222", "0100000222")
    claimed, holder = desadv.claim_send_or_identify(pg, "222", "0100000222", "f2.txt",
                                                     message_id="msg-new")
    assert claimed is False
    assert holder == "msg-old"


def test_claim_send_or_identify_reports_empty_holder_for_a_legacy_row(pg):
    desadv.claim_send(pg, "223", "0100000223", "f1.txt")   # no message_id
    desadv.confirm_sent(pg, "223", "0100000223")
    claimed, holder = desadv.claim_send_or_identify(pg, "223", "0100000223", "f2.txt",
                                                     message_id="msg-new")
    assert claimed is False
    assert holder == ""


def test_claim_send_or_identify_refuses_empty_identity(pg):
    claimed, holder = desadv.claim_send_or_identify(pg, "", "0100000224", "f1.txt")
    assert (claimed, holder) == (False, "")
    assert pg.execute("SELECT count(*) FROM desadv_sent").fetchone()[0] == 0


def test_claim_send_or_identify_reclaims_a_stale_claim_and_updates_the_holder(pg):
    desadv.claim_send(pg, "225", "0100000225", "f1.txt", message_id="msg-old")
    pg.execute("UPDATE desadv_sent SET sent_at = now() - interval '11 minutes' "
              "WHERE supplier_ean = '225'")
    claimed, holder = desadv.claim_send_or_identify(pg, "225", "0100000225", "f2.txt",
                                                     message_id="msg-new")
    assert claimed is True
    assert holder == ""
    assert desadv.claimed_by(pg, "225", "0100000225") == "msg-new"


def test_claim_send_or_identify_never_double_inserts(pg):
    """Same invariant as claim_send's own dedup test — the atomic combined path must
    not accidentally create two rows for the same identity."""
    desadv.claim_send_or_identify(pg, "226", "0100000226", "f1.txt", message_id="a")
    desadv.claim_send_or_identify(pg, "226", "0100000226", "f2.txt", message_id="b")
    assert pg.execute(
        "SELECT count(*) FROM desadv_sent WHERE supplier_ean = '226'").fetchone()[0] == 1
