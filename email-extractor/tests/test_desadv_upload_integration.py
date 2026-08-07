"""Composes the three #203 F4 primitives — `desadv_edi.build()`, `desadv`'s two-phase
ledger (F1), and `upload.put()`'s `dir_override` — the way a future worker (F5, not
built yet) will: build the document, claim the ledger row BEFORE the SFTP write,
upload, then confirm on success or release on failure.

This is not the worker itself — no message claiming, no DB `messages` state — it proves
the PRIMITIVES compose correctly as a sequence, including the claim-release failure
path the ledger exists to protect: a claim taken before a side effect must be released
on EVERY failure path between the claim and that side effect
(`.claude/rules/n8n-workflow-edits.md` rule 2 — the exact n8n incident class that lost
13 real orders before `edi_sent` went two-phase, #153).
"""
from unittest.mock import MagicMock, patch

from app.config import Config
from app.orders import desadv, desadv_edi, upload

ORION_DL_DIR = "C:\\ORION\\COMMUNICATOR\\data\\in_DL"


def _cfg(**kw):
    base = dict(orion_host="192.168.1.10", orion_port=22, orion_user="u", orion_pass="p",
               orion_dl_dir=ORION_DL_DIR)
    base.update(kw)
    return Config(**base)


def _header():
    return {"customerEanEdi": "8586013743063", "customerName": "Testovaci Dodavatel"}


def _extraction():
    return {"docNumber": "0100000042", "deliveryDate": "04.08.2026"}


def _items():
    return [{"gtin": "8500000000001", "name": "Chlieb", "matchedCatalogName": "Chlieb konzum",
            "quantity": 5, "unit": "ks", "unitPrice": 1.0, "totalPrice": 5.0, "mass": 0}]


def _fake_client():
    fake_file = MagicMock()
    fake_sftp = MagicMock()
    fake_sftp.file.return_value.__enter__.return_value = fake_file
    fake_client = MagicMock()
    fake_client.open_sftp.return_value = fake_sftp
    return fake_client, fake_sftp


def test_happy_path_claims_uploads_to_in_dl_with_z_prefix_then_confirms(pg):
    built = desadv_edi.build(_header(), _extraction(), _items(), [])
    assert built.can_create is True

    claimed = desadv.claim_send(pg, built.customer_ean_edi, built.doc_number, built.filename)
    assert claimed is True

    fake_client, fake_sftp = _fake_client()
    with patch("paramiko.SSHClient", return_value=fake_client):
        ok = upload.put(_cfg(), desadv_edi.upload_name(built.filename), built.content,
                        dir_override=ORION_DL_DIR)
    assert ok is True
    fake_sftp.file.assert_called_once_with(
        ORION_DL_DIR + "\\" + "Z-" + built.filename, "w")

    desadv.confirm_sent(pg, built.customer_ean_edi, built.doc_number)
    row = pg.execute(
        "SELECT uploaded_at, filename FROM desadv_sent WHERE doc_number = %s",
        (built.doc_number,)).fetchone()
    assert row[0] is not None
    assert row[1] == built.filename          # the ledger keeps the BASE name (R83)


def test_a_failed_upload_releases_the_claim_so_the_document_can_be_retried(pg):
    """The n8n incident class: every failure between claim and the side effect must
    release the claim, or the document is silently never sent again."""
    built = desadv_edi.build(_header(), _extraction(), _items(), [])
    assert desadv.claim_send(pg, built.customer_ean_edi, built.doc_number,
                             built.filename) is True

    fake_client, fake_sftp = _fake_client()
    fake_sftp.file.side_effect = OSError("connection reset")
    try:
        with patch("paramiko.SSHClient", return_value=fake_client):
            upload.put(_cfg(), desadv_edi.upload_name(built.filename), built.content,
                      dir_override=ORION_DL_DIR)
    except OSError:
        desadv.release_send(pg, built.customer_ean_edi, built.doc_number)

    assert pg.execute("SELECT count(*) FROM desadv_sent WHERE doc_number = %s",
                      (built.doc_number,)).fetchone()[0] == 0
    # A retry can claim again — the document is not silently lost.
    assert desadv.claim_send(pg, built.customer_ean_edi, built.doc_number,
                             built.filename) is True


def test_a_second_claim_for_the_same_document_is_refused_while_the_first_is_fresh(pg):
    built = desadv_edi.build(_header(), _extraction(), _items(), [])
    assert desadv.claim_send(pg, built.customer_ean_edi, built.doc_number,
                             built.filename) is True
    # A second worker racing the same document (e.g. a re-announcement mail) must not
    # also claim it while the first claim is still fresh — this is what stops the
    # W2/W3 duplicate-upload race the n8n registry was exposed to.
    assert desadv.claim_send(pg, built.customer_ean_edi, built.doc_number,
                             "other.txt") is False
