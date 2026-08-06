"""Match-quality provenance + a live, honest 'days since incident' signal (#196).

The warehouse checks every AI-matched order by hand because it has no MEASURABLE basis
to stop — these functions give it one: per-day counts by provenance (deterministic / AI
rung / held for review / error), and "days since the last CONFIRMED incident", always
computed live from a real, dated log — never a hand-maintained constant that ages.
"""
from psycopg.types.json import Json

from app.orders import reliability, report


def _run(pg, day, rules=(), shadow=False, status="ok", orders=1):
    rid = int(pg.execute(
        "INSERT INTO order_runs (message_id, shadow, status, started_at, finished_at, "
        "result) VALUES ('m', %s, %s, %s::timestamptz, %s::timestamptz, %s) "
        "RETURNING id",
        (shadow, status, f"{day} 10:00:00+00", f"{day} 10:00:05+00",
         Json({"orders": orders}))).fetchone()[0])
    for rule in rules:
        pg.execute("INSERT INTO order_items (run_id, name, rule) VALUES (%s, 'x', %s)",
                   (rid, rule))
    return rid


# --- provenance buckets ----------------------------------------------------

def test_provenance_buckets_deterministic_llm_and_review(pg):
    _run(pg, "2026-08-05", rules=("catalog_name", "alias_customer", "llm_sure",
                                  "llm_sure", "unmatched", "llm_borderline"))
    stats = reliability.provenance_stats_for_day(pg, "2026-08-05")
    assert stats["items"] == 6
    assert stats["deterministic"] == 2
    assert stats["llm"] == 2
    assert stats["review"] == 2


def test_the_new_195_guard_counts_as_held_for_review_too(pg):
    _run(pg, "2026-08-05", rules=("llm_sure_lexical_gap",))
    stats = reliability.provenance_stats_for_day(pg, "2026-08-05")
    assert stats["review"] == 1 and stats["llm"] == 0


def test_a_shadow_run_is_never_counted(pg):
    """Shadow observes, never ships — it must not inflate the trust metric."""
    _run(pg, "2026-08-05", rules=("llm_sure",), shadow=True)
    stats = reliability.provenance_stats_for_day(pg, "2026-08-05")
    assert stats["items"] == 0


def test_only_the_requested_day_is_counted(pg):
    _run(pg, "2026-08-04", rules=("llm_sure",))
    _run(pg, "2026-08-05", rules=("catalog_name",))
    stats = reliability.provenance_stats_for_day(pg, "2026-08-05")
    assert stats["items"] == 1 and stats["deterministic"] == 1


def test_orders_and_errors_are_counted_at_the_run_level(pg):
    _run(pg, "2026-08-05", orders=2)
    _run(pg, "2026-08-05", status="error", orders=1)
    stats = reliability.provenance_stats_for_day(pg, "2026-08-05")
    assert stats["runs"] == 2
    assert stats["orders"] == 3
    assert stats["errors"] == 1


def test_a_day_with_nothing_processed_reports_honest_zeros(pg):
    stats = reliability.provenance_stats_for_day(pg, "2026-08-05")
    assert stats == {"day": "2026-08-05", "runs": 0, "orders": 0, "errors": 0,
                     "items": 0, "deterministic": 0, "llm": 0, "review": 0}


# --- days since the last confirmed incident --------------------------------

def test_no_incidents_yet_reports_unknown_not_zero(pg):
    """Honesty over reassurance: no recorded history means UNKNOWN, never a fake 0."""
    assert reliability.days_since_incident(pg) is None


def test_days_since_the_last_incident_is_live_computed(pg):
    pg.execute("INSERT INTO match_incidents (occurred_on, description, issue_ref) "
              "VALUES (now()::date - interval '10 days', 'x', '#999')")
    assert reliability.days_since_incident(pg) == 10


def test_only_the_most_recent_incident_counts(pg):
    pg.execute(
        "INSERT INTO match_incidents (occurred_on, description, issue_ref) VALUES "
        "(now()::date - interval '30 days', 'old', '#1'), "
        "(now()::date - interval '2 days', 'new', '#2')")
    assert reliability.days_since_incident(pg) == 2


# --- the digest posts once per day, never once per tick --------------------

class _Cfg:
    orders_channel_id = 152
    odoo_url = "https://odoo.example"
    odoo_api_key = "k"
    dashboard_base_url = ""


def test_the_digest_posts_once_per_day_not_once_per_tick(pg):
    posted = []
    cfg = _Cfg()
    assert reliability.maybe_post_daily_digest(
        pg, cfg, post=lambda c, h: posted.append(h)) is True
    assert reliability.maybe_post_daily_digest(
        pg, cfg, post=lambda c, h: posted.append(h)) is False
    assert len(posted) == 1


def test_shadow_mode_posts_nothing(pg):
    posted = []
    assert reliability.maybe_post_daily_digest(
        pg, _Cfg(), shadow=True, post=lambda c, h: posted.append(h)) is False
    assert posted == []


def test_a_posting_failure_never_raises(pg):
    """Mirrors _post_summary/_check_spend_cap: a notification failure must never break
    the worker loop."""
    def _boom(cfg, html, **kw):
        raise RuntimeError("odoo is down")

    assert reliability.maybe_post_daily_digest(pg, _Cfg(), post=_boom) is True


# --- wired into worker.tick(), same pattern as _check_spend_cap ------------

def test_the_worker_tick_posts_the_daily_digest_once(pg, monkeypatch):
    from app.config import Config
    from app.orders import snapshot as snap
    from app.orders import worker

    snap.import_snapshot(
        pg, "GTIN,Sklad,Názov,doplnok\nG,1,x,\n",
        "Názov organizácie,EAN kód EDI,Obec,Ulica,Meno pre fakturáciu,"
        "Číslo mobilu,E-mail\nA,2000000000001,M,U,,,a@b.sk\n")
    pg.execute("INSERT INTO messages (message_id, category, processed)"
              " VALUES ('mw2', 'ai_orders', false)")

    posted = []
    monkeypatch.setattr(report, "post_from_config",
                        lambda cfg, html, **kw: posted.append(html))

    def fake_pipeline(conn, cfg, message, snapshot_id):
        return {"status": "ok", "items": []}

    cfg = Config(pg_dsn="", ai_orders_engine="python", orders_shadow=False)
    assert worker.tick(pg, cfg, pipeline=fake_pipeline) == 1
    assert len(posted) == 1
    assert pg.execute("SELECT count(*) FROM order_digest_sent").fetchone()[0] == 1


def test_the_dashboard_api_reports_todays_and_yesterdays_provenance_and_the_incident_gap(pg):
    import os

    from app.config import Config
    from app.httpapi import create_app

    _run(pg, reliability._yesterday(pg), rules=("catalog_name", "llm_sure", "unmatched"))
    pg.execute("INSERT INTO match_incidents (occurred_on, description, issue_ref) "
              "VALUES (now()::date - interval '4 days', 'x', '#999')")
    cfg = Config(pg_dsn=os.environ["PG_TEST_DSN"], data_dir="/tmp", dash_password="pw",
                secret_key="t")
    app = create_app(cfg)
    app.testing = True
    c = app.test_client()
    c.post("/login", data={"password": "pw"})
    r = c.get("/api/orders/digest")
    assert r.status_code == 200
    body = r.get_json()
    assert body["days_since_incident"] == 4
    assert body["yesterday"]["items"] == 3
    assert body["yesterday"]["deterministic"] == 1
    assert body["yesterday"]["llm"] == 1
    assert body["yesterday"]["review"] == 1


def test_the_worker_tick_never_posts_the_digest_while_shadowing(pg, monkeypatch):
    from app.config import Config
    from app.orders import snapshot as snap
    from app.orders import worker

    pg.execute(
        """INSERT INTO messages (message_id, category, subject, combined_text, processed)
           VALUES ('mw3', 'ai_orders', 'Objednávka', '10x rožok', false)""")
    snap.import_snapshot(pg, "GTIN,Sklad,Názov,doplnok\nG,1,x,\n",
                         "Názov organizácie,EAN kód EDI,Obec,Ulica,Meno pre fakturáciu,"
                         "Číslo mobilu,E-mail\nA,2000000000001,M,U,,,a@b.sk\n")

    posted = []
    monkeypatch.setattr(report, "post_from_config",
                        lambda cfg, html, **kw: posted.append(html))
    cfg = Config(pg_dsn="", ai_orders_engine="n8n", orders_shadow=True)
    worker.tick(pg, cfg, pipeline=lambda conn, cfg, message, snapshot_id: {"status": "ok"})
    assert posted == []
    assert pg.execute("SELECT count(*) FROM order_digest_sent").fetchone()[0] == 0
