"""What the order engine costs, and the €30/month tripwire (#89).

The user accepted the measured running cost (~€17/month at 155 ai_orders mails) and asked for
a **€30/month tripwire**: not a hard stop — an unshipped order costs the business far more
than the tokens — but one message when the month runs hot, naming the worst runs so the cause
is findable (a prompt gone verbose, a runaway retry, a mail with 200 items).

Two numbers belong together here and are reported together: what the month cost, and the
**share of item decisions made with no model call at all**. That share is supposed to RISE as
the delivery history fills (#86, #88) — the same mechanism that makes the engine more certain
makes it cheaper, so a falling share is the early warning that something regressed.
"""
from __future__ import annotations

import logging

log = logging.getLogger("orders.spend")

# The prices are in USD, the user's budget is in euro. One fixed, deliberately conservative
# rate: a tripwire that moves with the FX market would trip on the market, not on us.
USD_PER_EUR = 1.10
FREE_RULES = ("catalog_name", "alias_exact", "history_sure")


def record(conn, run_id: int, spend: dict) -> None:
    """Store what one run cost. Called with `llm.Client.spend()`."""
    conn.execute(
        """UPDATE order_runs
              SET calls = %s, cached_calls = %s, tokens_in = %s, tokens_cached = %s,
                  tokens_out = %s, cost_usd = %s, model = %s
            WHERE id = %s""",
        (spend.get("calls", 0), spend.get("cached_calls", 0), spend.get("tokens_in", 0),
         spend.get("tokens_cached", 0), spend.get("tokens_out", 0),
         float(spend.get("cost_usd", 0.0)), spend.get("model", ""), run_id))


def month_to_date(conn, month: str = "") -> dict:
    """The bill so far this month. A shadow run is billed too — the model was called."""
    row = conn.execute(
        """SELECT count(*), coalesce(sum(cost_usd), 0), coalesce(sum(calls), 0),
                  coalesce(sum(cached_calls), 0)
             FROM order_runs
            WHERE to_char(coalesce(finished_at, started_at), 'YYYY-MM')
                  = coalesce(nullif(%s, ''), to_char(now(), 'YYYY-MM'))""",
        (month,)).fetchone()
    usd = float(row[1] or 0)
    return {"runs": int(row[0] or 0), "cost_usd": usd, "cost_eur": usd / USD_PER_EUR,
            "calls": int(row[2] or 0), "cached_calls": int(row[3] or 0),
            "month": month or _this_month(conn)}


def deterministic_share(conn, month: str = "") -> dict:
    """How many item decisions needed no model call. This number should rise over time."""
    row = conn.execute(
        """SELECT count(*) FILTER (WHERE i.rule = ANY(%s)), count(*)
              FROM order_items i JOIN order_runs r ON r.id = i.run_id
             WHERE to_char(coalesce(r.finished_at, r.started_at), 'YYYY-MM')
                   = coalesce(nullif(%s, ''), to_char(now(), 'YYYY-MM'))""",
        (list(FREE_RULES), month)).fetchone()
    free, total = int(row[0] or 0), int(row[1] or 0)
    return {"free": free, "total": total,
            "pct": (100.0 * free / total) if total else 0.0}


def top_runs(conn, limit: int = 3, month: str = "") -> list[dict]:
    rows = conn.execute(
        """SELECT id, message_id, cost_usd, calls
             FROM order_runs
            WHERE to_char(coalesce(finished_at, started_at), 'YYYY-MM')
                  = coalesce(nullif(%s, ''), to_char(now(), 'YYYY-MM'))
            ORDER BY cost_usd DESC NULLS LAST LIMIT %s""",
        (month, limit)).fetchall()
    return [{"run_id": r[0], "message_id": r[1], "cost_usd": float(r[2] or 0),
             "calls": int(r[3] or 0)} for r in rows]


def cap_tripped(conn, cap_eur: float = 30.0) -> bool:
    """True exactly ONCE per month, the first time the month passes the cap.

    Claiming the month in the table is what makes it once-per-month instead of
    once-per-run — the per-run version would send a message for every remaining order of the
    month, which is the notification noise the user removed everywhere else.
    """
    mtd = month_to_date(conn)
    if mtd["cost_eur"] <= cap_eur:
        return False
    claimed = conn.execute(
        """INSERT INTO order_spend_alerts (month, cost_eur)
           VALUES (%s, %s) ON CONFLICT (month) DO NOTHING RETURNING month""",
        (mtd["month"], mtd["cost_eur"])).fetchone()
    if claimed:
        log.warning("spend cap: %.2f EUR month-to-date is over the %.0f EUR cap",
                    mtd["cost_eur"], cap_eur)
    return bool(claimed)


def trip_message(conn, cap_eur: float = 30.0) -> str:
    """Slovak, phone-readable: what the month cost and which runs to look at."""
    mtd = month_to_date(conn)
    share = deterministic_share(conn)
    lines = [f"Objednávkový automat presiahol mesačný strop {cap_eur:.0f} € — "
             f"za {mtd['month']} je to {mtd['cost_eur']:.2f} € "
             f"({mtd['runs']} spracovaných e-mailov, "
             f"{share['pct']:.0f} % položiek rozhodnutých bez modelu).",
             "Najdrahšie behy:"]
    for r in top_runs(conn):
        lines.append(f"  • beh {r['run_id']} ({r['message_id']}): "
                     f"{r['cost_usd'] / USD_PER_EUR:.2f} €, {r['calls']} volaní")
    return "\n".join(lines)


def _this_month(conn) -> str:
    return str(conn.execute("SELECT to_char(now(), 'YYYY-MM')").fetchone()[0])
