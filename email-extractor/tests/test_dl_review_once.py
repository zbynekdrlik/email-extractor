"""#495 — the ❗ „Dodací list potrebuje kontrolu" Odoo post goes out ONCE per delivery note
(supplier + DL number) and reason, not once per print / reprocess.

30 days of the warehouse thread (channel 243, 16.9.-16.10.): 57 ❗ posts about only 21 DLs +
9 without a number — the same DL up to 8x, because a supplier mails every DL three times and
every board answer reprocesses each copy again. One choke point (`dl_events._post_review`)
remembers what went out in `dl_review_posts` (UNIQUE doc_key + reason hash): the same DL with
the same reason is never posted twice, a DIFFERENT reason is, a DL without a number is posted
once per mail, and a post Odoo never received is not remembered. Shadow posts nothing.

Drives the REAL worker (`dl_worker.tick`) against a real Postgres, scripted model answers
only. All fixtures SYNTHETIC (made-up numbers / names) — public repo.
"""
from __future__ import annotations

import re
from pathlib import Path

from test_dl_worker import (
    ITEM_MATCHED,
    SUPPLIER_MATCHED,
    FakeClient,
    _attach,
    _cfg,
    _dl_days_ago,
    _doc,
    _msg,
    _snapshot,
)

from app.orders import dl_worker

DL_NO = "0100555001"


class Poster:
    """A fake Odoo post: records every ❗/✅ body; `deliver=False` = Odoo never got it."""

    def __init__(self, deliver: bool = True):
        self.html: list[str] = []
        self.deliver = deliver

    def __call__(self, cfg, html):
        self.html.append(html)
        return {"id": len(self.html)} if self.deliver else None

    def reviews(self) -> list[str]:
        return [h for h in self.html if "potrebuje kontrolu" in h]


def _no_upload(*a, **k):
    raise AssertionError("a review path never uploads")


def _tick(pg, tmp_path, mid, docs, post, items=None, **cfgkw):
    """One mail with one attachment, processed by one live tick."""
    _msg(pg, mid=mid)
    _attach(pg, tmp_path, mid)
    cfg = _cfg(delivery_notes_engine="python", data_dir=str(tmp_path), **cfgkw)
    client = FakeClient({"dl_documents": [docs], "dl_supplier": [SUPPLIER_MATCHED] * 3,
                         "dl_item": list(items or [ITEM_MATCHED] * 3)})
    return dl_worker.tick(pg, cfg, client=client, upload=_no_upload, post=post)


def _reprocess(pg, mid):
    pg.execute("UPDATE messages SET processed = false, processing_at = NULL, attempts = 0 "
               "WHERE message_id = %s", (mid,))


def _post_events(pg, mid):
    return [r[0] for r in pg.execute(
        "SELECT status FROM email_events WHERE message_id = %s AND stage = 'review_post' "
        "ORDER BY id", (mid,)).fetchall()]


def test_two_mails_of_the_same_dl_with_the_same_reason_post_one_review(pg, tmp_path):
    _snapshot(pg)
    post = Poster()
    _tick(pg, tmp_path, "dl-a", _doc(doc_number=DL_NO, total=999.0), post)
    _tick(pg, tmp_path, "dl-b", _doc(doc_number=DL_NO, total=999.0), post)
    assert len(post.reviews()) == 1, post.reviews()
    # the second mail still records its own review outcome on its timeline …
    assert pg.execute("SELECT count(*) FROM email_events WHERE message_id = 'dl-b' "
                      "AND stage = 'review'").fetchone()[0] == 1
    # … and WHY nothing went to Odoo
    assert _post_events(pg, "dl-a") == ["posted"]
    assert _post_events(pg, "dl-b") == ["suppressed"]


def test_numbers_inside_the_same_reason_do_not_make_it_a_new_reason(pg, tmp_path):
    """A reprint read with other sums is still „the totals do not add up" — one post."""
    _snapshot(pg)
    post = Poster()
    _tick(pg, tmp_path, "dl-a", _doc(doc_number=DL_NO, total=999.0), post)
    _tick(pg, tmp_path, "dl-b", _doc(doc_number=DL_NO, total=888.0), post)
    assert len(post.reviews()) == 1


def test_a_different_reason_for_the_same_dl_posts_again(pg, tmp_path):
    _snapshot(pg)
    post = Poster()
    _tick(pg, tmp_path, "dl-a", _doc(doc_number=DL_NO, total=999.0), post)
    # the same DL, now with a delivery date far outside the plausible window (#400 gate)
    _tick(pg, tmp_path, "dl-b", _doc(doc_number=DL_NO, delivery_date=_dl_days_ago(60)),
          post)
    assert len(post.reviews()) == 2
    assert "mimo očakávaného rozsahu" in post.reviews()[1]


def test_a_dl_without_a_number_is_reviewed_once_per_mail(pg, tmp_path):
    _snapshot(pg)
    post = Poster()
    _tick(pg, tmp_path, "dl-a", _doc(doc_number="", total=999.0), post)
    _tick(pg, tmp_path, "dl-b", _doc(doc_number="", total=999.0), post)
    assert len(post.reviews()) == 2, "no number = no shared key: each mail is its own DL"
    _reprocess(pg, "dl-a")
    cfg = _cfg(delivery_notes_engine="python", data_dir=str(tmp_path))
    dl_worker.tick(pg, cfg, client=FakeClient({"dl_documents": [
        _doc(doc_number="", total=999.0)]}), upload=_no_upload, post=post)
    assert len(post.reviews()) == 2, "a reprocess of the same mail never repeats it"


def test_a_held_dl_printed_three_times_is_posted_once(pg, tmp_path):
    """The live Lunys shape: one DL arrives in three mails, an unmatched line holds it (#365);
    every copy dedupes onto the SAME open board question — one ❗, not three."""
    _snapshot(pg)
    post = Poster()
    doc = _doc(doc_number=DL_NO, total=8.0, items=[
        {"name": "Rožok 50g", "quantity": 10, "unit": "ks", "unitPrice": 0.5,
         "totalPrice": 5.0},
        {"name": "Neznámy chlebík", "quantity": 3, "unit": "ks", "unitPrice": 1.0,
         "totalPrice": 3.0}])
    no_match = {"gtin": "NO_MATCH", "matchConfidence": 0.0, "matchReason": "žiadna zhoda"}
    for mid in ("dl-a", "dl-b", "dl-c"):
        _tick(pg, tmp_path, mid, doc, post, items=[ITEM_MATCHED, no_match] * 2)
    reviews = post.reviews()
    assert len(reviews) == 1, reviews
    assert "Neznámy chlebík" in reviews[0]
    assert pg.execute("SELECT count(*) FROM order_questions WHERE kind = 'dl_item'"
                      ).fetchone()[0] == 1


def test_a_post_odoo_never_received_is_not_remembered(pg, tmp_path):
    _snapshot(pg)
    failed, ok = Poster(deliver=False), Poster()
    _tick(pg, tmp_path, "dl-a", _doc(doc_number=DL_NO, total=999.0), failed)
    _tick(pg, tmp_path, "dl-b", _doc(doc_number=DL_NO, total=999.0), ok)
    assert len(failed.reviews()) == 1 and len(ok.reviews()) == 1, \
        "the undelivered first post must not suppress the next one"
    assert _post_events(pg, "dl-a") == ["post_failed"]
    assert pg.execute("SELECT count(*) FROM dl_review_posts").fetchone()[0] == 1


def test_a_post_that_raises_is_not_remembered(pg, tmp_path):
    _snapshot(pg)

    def boom(cfg, html):
        raise RuntimeError("Odoo down")

    ok = Poster()
    _tick(pg, tmp_path, "dl-a", _doc(doc_number=DL_NO, total=999.0), boom)
    _tick(pg, tmp_path, "dl-b", _doc(doc_number=DL_NO, total=999.0), ok)
    assert len(ok.reviews()) == 1


def test_shadow_posts_nothing_and_remembers_nothing(pg, tmp_path):
    _snapshot(pg)
    post = Poster()
    _msg(pg, mid="dl-s")
    _attach(pg, tmp_path, "dl-s")
    cfg = _cfg(delivery_notes_engine="n8n", delivery_notes_shadow=True,
               data_dir=str(tmp_path))
    dl_worker.tick(pg, cfg, client=FakeClient({"dl_documents": [
        _doc(doc_number=DL_NO, total=999.0)]}), upload=_no_upload, post=post)
    assert post.html == []
    assert pg.execute("SELECT count(*) FROM dl_review_posts").fetchone()[0] == 0


def test_every_review_post_goes_through_the_choke_point():
    """`dl_report.build_review` is called from exactly ONE place — `dl_events._post_review`.
    A new direct call would post a ❗ past the „once per DL" memory, so it fails here."""
    app_dir = Path(__file__).resolve().parent.parent / "app"
    calls: dict[str, int] = {}
    for path in sorted(app_dir.rglob("*.py")):
        text = path.read_text(encoding="utf-8")
        n = len(re.findall(r"(?<!def )\bbuild_review\(", text))
        if n:
            calls[path.relative_to(app_dir).as_posix()] = n
    assert calls == {"orders/dl_events.py": 1}, calls


def test_dl_review_posts_is_its_own_migration_revision_with_a_unique_key(pg):
    import psycopg
    import pytest

    from app import db
    assert any(r.name == "add_dl_review_posts" for r in db.REVISIONS)
    pg.execute("INSERT INTO dl_review_posts (doc_key, reason_hash, message_id, reason) "
               "VALUES ('k', 'h', 'm1', 'r')")
    with pytest.raises(psycopg.errors.UniqueViolation):
        pg.execute("INSERT INTO dl_review_posts (doc_key, reason_hash, message_id, reason) "
                   "VALUES ('k', 'h', 'm2', 'r')")
    pg.execute("INSERT INTO dl_review_posts (doc_key, reason_hash, message_id, reason) "
               "VALUES ('k', 'other', 'm2', 'r2')")   # the same DL, another reason
