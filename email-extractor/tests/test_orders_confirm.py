"""Import CONFIRMATION (#151): uploading a file to `in/` is not proof Communicator ever
took it. `confirm.sweep` classifies every uploaded-but-unresolved `edi_sent` row against a
read-only listing of `in`, `in/archCodex`, `in/unconfirmed` and alerts the warehouse for
every outcome except a clean import — never silently.

Live evidence pinned by these tests (see the #151 design discussion): the `Z-` filename
prefix is NOT the imported signal — presence in `archCodex` is, with or without it.
"""
import os

from app import db
from app.config import Config
from app.orders import confirm

PG_DSN = os.environ.get("PG_TEST_DSN")


class PostRecorder:
    def __init__(self):
        self.calls = []

    def __call__(self, cfg, html, **kw):
        self.calls.append((html, kw.get("channel_id")))
        return {"id": 1}


def _cfg(**kw):
    base = dict(pg_dsn=PG_DSN, data_dir="/tmp", odoo_url="https://erp.example.sk",
               odoo_api_key="k", orders_channel_id=152, delivery_notes_channel_id=243,
               import_confirm_timeout_minutes=60, import_confirm_interval_minutes=5)
    base.update(kw)
    return Config(**base)


def _insert(pg, ean, filename, uploaded_minutes_ago=0, checked_minutes_ago=None):
    row = pg.execute(
        """INSERT INTO edi_sent (customer_ean, delivery_date, content_sha256, filename,
                                 uploaded_at, import_checked_at)
           VALUES (%s, '04.08.2026', %s, %s,
                   now() - make_interval(mins => %s),
                   CASE WHEN %s::int IS NULL THEN NULL
                        ELSE now() - make_interval(mins => %s::int) END)
           RETURNING id""",
        (ean, "hash-" + filename, filename, uploaded_minutes_ago,
         checked_minutes_ago, checked_minutes_ago or 0)).fetchone()
    return row[0]


def _status(pg, rid):
    return pg.execute(
        "SELECT import_status FROM edi_sent WHERE id = %s", (rid,)).fetchone()[0]


# --- imported: archCodex presence, WITH or WITHOUT the Z- prefix ---------------------

def test_a_file_present_in_archcodex_is_confirmed_imported_with_no_alert(pg):
    rid = _insert(pg, "1", "ORDER_a.txt", uploaded_minutes_ago=90)
    posts = PostRecorder()
    n = confirm.sweep(pg, _cfg(),
                      listdir=lambda: {"in": set(), "archCodex": {"ORDER_a.txt"},
                                       "unconfirmed": set()},
                      post=posts)
    assert n == 1
    assert posts.calls == [], "a clean import must never alert"
    assert _status(pg, rid) == "imported"


def test_a_z_prefixed_file_in_archcodex_still_counts_as_imported(pg):
    """The ticket's own original proposal (key on the Z- prefix) is WRONG — live evidence
    showed an already-imported file sitting 5+ hours with no Z- prefix yet. The prefix, if
    present, must not be REQUIRED."""
    rid = _insert(pg, "2", "ORDER_b.txt", uploaded_minutes_ago=90)
    posts = PostRecorder()
    confirm.sweep(pg, _cfg(),
                  listdir=lambda: {"in": set(), "archCodex": {"Z-ORDER_b.txt"},
                                   "unconfirmed": set()},
                  post=posts)
    assert posts.calls == []
    assert _status(pg, rid) == "imported"


# --- failed: unconfirmed, alerts immediately regardless of age -----------------------

def test_a_file_in_unconfirmed_alerts_immediately_even_when_very_fresh(pg):
    rid = _insert(pg, "3", "ORDER_c.txt", uploaded_minutes_ago=1)
    posts = PostRecorder()
    confirm.sweep(pg, _cfg(),
                  listdir=lambda: {"in": set(), "archCodex": set(),
                                   "unconfirmed": {"ORDER_c.txt"}},
                  post=posts)
    assert len(posts.calls) == 1
    assert "ZLYHAL" in posts.calls[0][0]
    assert "ORDER_c.txt" in posts.calls[0][0]
    assert posts.calls[0][1] == 152
    assert _status(pg, rid) == "failed"


# --- still in in/: silent while young, timeout+alert once stale ----------------------

def test_a_fresh_file_still_in_in_is_silent_no_status_no_alert(pg):
    rid = _insert(pg, "4", "ORDER_d.txt", uploaded_minutes_ago=1)
    posts = PostRecorder()
    n = confirm.sweep(pg, _cfg(),
                      listdir=lambda: {"in": {"ORDER_d.txt"}, "archCodex": set(),
                                       "unconfirmed": set()},
                      post=posts)
    assert n == 0
    assert posts.calls == []
    row = pg.execute(
        "SELECT import_status, import_checked_at FROM edi_sent WHERE id = %s",
        (rid,)).fetchone()
    assert row[0] is None, "still legitimately pending — Communicator sweeps every ~25-30m"
    assert row[1] is not None, "the throttle timestamp must still advance"


def test_a_file_past_the_timeout_still_in_in_alerts_and_is_marked_timeout(pg):
    rid = _insert(pg, "5", "ORDER_e.txt", uploaded_minutes_ago=61)
    posts = PostRecorder()
    confirm.sweep(pg, _cfg(import_confirm_timeout_minutes=60),
                  listdir=lambda: {"in": {"ORDER_e.txt"}, "archCodex": set(),
                                   "unconfirmed": set()},
                  post=posts)
    assert len(posts.calls) == 1
    assert "60 min" in posts.calls[0][0]
    assert _status(pg, rid) == "timeout"


# --- gone from all three: never silent success ----------------------------------------

def test_a_file_gone_from_all_three_directories_alerts_as_unknown_never_silent(pg):
    rid = _insert(pg, "6", "ORDER_f.txt", uploaded_minutes_ago=5)
    posts = PostRecorder()
    confirm.sweep(pg, _cfg(),
                  listdir=lambda: {"in": set(), "archCodex": set(), "unconfirmed": set()},
                  post=posts)
    assert len(posts.calls) == 1
    assert "zmizol" in posts.calls[0][0]
    assert _status(pg, rid) == "unknown"


# --- dedup: a resolved row is never rechecked or alerted twice ------------------------

def test_an_already_resolved_row_is_never_swept_or_alerted_again(pg):
    rid = _insert(pg, "7", "ORDER_g.txt", uploaded_minutes_ago=90)
    calls = {"n": 0}

    def listdir():
        calls["n"] += 1
        return {"in": set(), "archCodex": set(), "unconfirmed": {"ORDER_g.txt"}}

    posts = PostRecorder()
    confirm.sweep(pg, _cfg(), listdir=listdir, post=posts)
    assert len(posts.calls) == 1

    n2 = confirm.sweep(pg, _cfg(), listdir=listdir, post=posts)
    assert n2 == 0
    assert calls["n"] == 1, "a terminal row must never trigger a second SFTP listing"
    assert len(posts.calls) == 1, "and must never alert twice"
    assert _status(pg, rid) == "failed"


# --- SFTP is contacted only when there is real work ------------------------------------

def test_sweep_never_touches_sftp_when_nothing_is_pending(pg):
    def boom():
        raise AssertionError("SFTP must never be contacted with no pending rows")

    n = confirm.sweep(pg, _cfg(), listdir=boom, post=PostRecorder())
    assert n == 0


def test_the_per_row_throttle_skips_a_recently_checked_row(pg):
    _insert(pg, "8", "ORDER_h.txt", uploaded_minutes_ago=90, checked_minutes_ago=1)

    def boom():
        raise AssertionError("a recently-checked row must not trigger a fresh listing")

    n = confirm.sweep(pg, _cfg(import_confirm_interval_minutes=5), listdir=boom,
                      post=PostRecorder())
    assert n == 0


# --- channel routing: ORDER_* vs DESADV_* -----------------------------------------------

def test_desadv_alert_routes_to_the_delivery_notes_channel(pg):
    _insert(pg, "9", "DESADV_a.txt", uploaded_minutes_ago=1)
    posts = PostRecorder()
    confirm.sweep(pg, _cfg(),
                  listdir=lambda: {"in": set(), "archCodex": set(),
                                   "unconfirmed": {"DESADV_a.txt"}},
                  post=posts)
    assert posts.calls[0][1] == 243


def test_desadv_alert_falls_back_to_the_orders_channel_when_unset(pg):
    _insert(pg, "10", "DESADV_b.txt", uploaded_minutes_ago=1)
    posts = PostRecorder()
    confirm.sweep(pg, _cfg(delivery_notes_channel_id=0),
                  listdir=lambda: {"in": set(), "archCodex": set(),
                                   "unconfirmed": {"DESADV_b.txt"}},
                  post=posts)
    assert posts.calls[0][1] == 152


# --- migration: never retroactively alert on pre-existing history --------------------

def test_pre_migration_rows_are_backfilled_as_already_imported_never_swept(pg):
    """Every row confirmed-uploaded before this feature shipped predates import
    confirmation entirely — backfilling it straight to 'imported' (not NULL/pending)
    stops the first sweep after deploy from flooding Odoo with alerts for old, already-
    settled orders (see #151's design discussion)."""
    pg.execute("ALTER TABLE edi_sent DROP COLUMN import_status, "
              "DROP COLUMN import_confirmed_at, DROP COLUMN import_checked_at")
    pg.execute(
        """INSERT INTO edi_sent (customer_ean, delivery_date, content_sha256, filename,
                                 sent_at, uploaded_at)
           VALUES ('11', '04.08.2026', 'deadbeef', 'historical.txt',
                   now() - interval '30 days', now() - interval '30 days')""")
    db.init_schema(pg)   # re-run the migration: columns come back, so does the backfill
    row = pg.execute(
        """SELECT import_status, import_confirmed_at, uploaded_at FROM edi_sent
            WHERE customer_ean = '11'""").fetchone()
    assert row[0] == "imported"
    assert row[1] is not None and row[1] == row[2]
    assert confirm.due_rows(pg, 5) == [], "a backfilled historical row must never be swept"
