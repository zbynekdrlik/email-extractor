"""Read-only aggregate/reporting endpoints — spend, order digest, DL stats, IMAP
ingestion failures (#268 krok 8).

Moved VERBATIM out of `app/httpapi.py` (no behavior change) — see the design comment on
#268 for exactly what moved and why. All four are aggregate-only views (counts, sums,
provenance stats) — never a mail body, an attachment, or any single message's content.
"""
from __future__ import annotations

from flask import Flask, jsonify

from . import db
from .db import MAX_UID_ATTEMPTS
from .httpapi_common import Deps


def register(app: Flask, deps: Deps) -> None:
    @app.get("/api/orders/spend")
    def api_orders_spend():
        """What the order engine costs this month, and how much of it needed no model (#89).

        The two numbers belong together: the deterministic share is supposed to RISE as the
        delivery history fills, so a falling share explains a rising bill.
        """
        from .orders import spend as spend_mod
        with deps.db() as c:
            mtd = spend_mod.month_to_date(c)
            share = spend_mod.deterministic_share(c)
            top = spend_mod.top_runs(c)
        return jsonify(month=mtd["month"], runs=mtd["runs"],
                       cost_eur=round(mtd["cost_eur"], 2),
                       cost_usd=round(mtd["cost_usd"], 2),
                       per_email_eur=round(mtd["cost_eur"] / mtd["runs"], 3)
                       if mtd["runs"] else 0.0,
                       calls=mtd["calls"], cached_calls=mtd["cached_calls"],
                       cap_eur=float(getattr(deps.cfg, "orders_spend_cap_eur", 30) or 0),
                       free_pct=round(share["pct"], 1), free=share["free"],
                       decisions=share["total"], top_runs=top)

    @app.get("/api/orders/digest")
    def api_orders_digest():
        """#196: the same match-provenance stats + 'days since incident' the daily Odoo
        digest carries — the warehouse's measurable, live basis for trust, on the
        dashboard too, not only in the Odoo channel."""
        from .orders import reliability
        with deps.db() as c:
            today = reliability.provenance_stats_for_day(c)
            yesterday = reliability.provenance_stats_for_day(
                c, c.execute(
                    "SELECT to_char(now() - interval '1 day', 'YYYY-MM-DD')").fetchone()[0])
            since = reliability.days_since_incident(c)
        return jsonify(today=today, yesterday=yesterday, days_since_incident=since)

    @app.get("/api/orders/dl/stats")
    def api_orders_dl_stats():
        """#231: the "stavy" the DL nástenka asks for — today/yesterday's DL run counts
        (`reliability.dl_provenance_stats_for_day`, built for #204's daily digest — same
        aggregate-only shape: run/document counts, no mail body, no attachment). Reachable
        by BOTH the full admin login and the DL-only `sklad_dl` role (it is in
        `SKLAD_DL_PATHS`); the orders-only `sklad` role has no matching path and gets a
        plain 401, same as any other endpoint outside its own board."""
        from .orders import reliability
        with deps.db() as c:
            today = reliability.dl_provenance_stats_for_day(c)
            # #239 deep-review finding: the three current-health gauges are NOT
            # day-scoped — the JS badge only ever reads them off `today` (see
            # ASK_DL_HTML's loadStats()), so recomputing the identical three queries
            # for "yesterday" would be pure waste.
            yesterday = reliability.dl_provenance_stats_for_day(
                c, c.execute(
                    "SELECT to_char(now() - interval '1 day', 'YYYY-MM-DD')").fetchone()[0],
                include_current_health=False)
        return jsonify(today=today, yesterday=yesterday)

    @app.get("/api/imap-failures")
    def api_imap_failures():
        """Emails that could not be ingested at all (#20) — they have no messages row,
        so this is the ONLY place they are visible. Never let them be silent."""
        with deps.db() as c:
            items = db.list_uid_failures(c)
            pending, skipped = db.count_uid_failures(c)
        return jsonify(total=pending + skipped, items=items, shown=len(items),
                       max_attempts=MAX_UID_ATTEMPTS, pending=pending, skipped=skipped)
