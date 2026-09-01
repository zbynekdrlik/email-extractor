"""#268 krok 1: charakterizačné testy PRED rozdelením `app/httpapi.py` (2693 riadkov) na
9 modulov.

Existujúcich ~3600 riadkov testov v tomto priečinku overuje CHOVANIE (stavové kódy, JSON
tvar, DOM cez Playwright) — nikdy surové bajty odpovede a nikdy tabuľku route ako celok.
To sú presne diery, ktoré plán rozdelenia (issuecomment na #268) označuje ako riziko pre
krok 4 (1226 riadkov HTML/CSS/JS presunutých naraz) a krok 10 (`orders_questions`, 446
riadkov s dvojspojením proti duplicitnému uploadu): preklep v ceste route alebo v
skopírovanom HTML reťazci by nezachytil ani jeden existujúci test.

Tri testy nižšie (zachytené proti HEAD `bd9e28e` na `dev`, docs-only commit nad
`3e95cbf`, na ktorý bol plán písaný — žiadna zmena v `app/httpapi.py` medzi nimi):

1. `test_route_table_matches_the_pre_split_baseline` — presná tabuľka route.
2. `test_html_template_constants_match_their_pre_split_checksum` — sha256 piatich
   vyrenderovaných HTML konštánt.
3. `test_orders_digest_happy_path_returns_todays_and_yesterdays_provenance_stats` —
   `/api/orders/digest` doteraz nemal ŽIADEN happy-path test (`test_httpapi.py:53`
   overuje len 401 bez session).

Toto sú CHARAKTERIZAČNÉ testy — pinujú SÚČASNÉ chovanie ako základňu na porovnanie po
každom kroku presunu, nie regresný RED->GREEN pár (žiadna produkčná zmena v tomto PR).
"""
import hashlib
import os

from psycopg.types.json import Json

from app.config import Config
from app.httpapi import (
    ASK_DL_HTML,
    ASK_HTML,
    DASH_HTML,
    LOGIN_HTML,
    ZNALOSTI_HTML,
    create_app,
)

PG_DSN = os.environ.get("PG_TEST_DSN")


def _client():
    cfg = Config(pg_dsn=PG_DSN, data_dir="/tmp", api_token="tok", dash_password="secret",
                 secret_key="test-secret")
    app = create_app(cfg)
    app.testing = True
    return app.test_client()


def _login(c):
    c.post("/login", data={"password": "secret"})


# ---- 1. route-table inventory ------------------------------------------------------

# Every (methods, rule) pair `create_app()` registers, captured from HEAD `bd9e28e`
# (dev, verified against #268's plan comment — see the design comment on the issue).
# Flask's own built-in `static` rule is deliberately excluded: it exists regardless of
# how create_app() is written and carries no signal about the split. HEAD/OPTIONS are
# stripped from `methods` so the list is stable rather than brittle (Flask adds them
# automatically to every GET route and every route in general, respectively).
EXPECTED_ROUTES = sorted([
    (("GET",), "/"),
    # #342: the codex-bridge push endpoint (machine X-Token auth) — a legitimate NEW route,
    # not a #268 code-move; added here in the same commit that registers it.
    (("POST",), "/api/codex/orders"),
    (("GET",), "/api/fix-queue"),
    (("POST",), "/api/fix/<int:fid>/resolve"),
    (("GET",), "/api/imap-failures"),
    (("GET",), "/api/message/<int:mid>"),
    (("POST",), "/api/message/<int:mid>/fix"),
    (("POST",), "/api/message/<int:mid>/reclassify"),
    (("POST",), "/api/message/<int:mid>/reprocess"),
    (("GET",), "/api/messages"),
    (("GET",), "/api/orders/digest"),
    (("GET",), "/api/orders/dl/stats"),
    (("GET",), "/api/orders/held"),
    (("POST",), "/api/orders/question/<int:qid>/answer"),
    (("POST",), "/api/orders/question/<int:qid>/undo"),
    (("GET",), "/api/orders/questions"),
    (("GET",), "/api/orders/spend"),
    (("GET",), "/api/orders/taught"),
    (("GET",), "/api/znalosti/catalog"),
    (("DELETE",), "/api/znalosti/clients"),
    (("GET",), "/api/znalosti/clients"),
    (("POST",), "/api/znalosti/clients"),
    (("GET",), "/api/znalosti/customer/<ean>"),
    (("POST",), "/api/znalosti/customer/<ean>"),
    (("DELETE",), "/api/znalosti/customer/<ean>/<int:rid>"),
    (("GET",), "/api/znalosti/customers"),
    (("GET",), "/api/znalosti/dl-products"),
    (("POST",), "/api/znalosti/dl-products"),
    (("DELETE",), "/api/znalosti/dl-products/<gtin>"),
    (("DELETE",), "/api/znalosti/dl-suppliers"),
    (("GET",), "/api/znalosti/dl-suppliers"),
    (("POST",), "/api/znalosti/dl-suppliers"),
    (("GET",), "/api/znalosti/global"),
    (("POST",), "/api/znalosti/global"),
    (("DELETE",), "/api/znalosti/global/<int:rid>"),
    (("GET",), "/api/znalosti/products"),
    (("POST",), "/api/znalosti/products"),
    (("DELETE",), "/api/znalosti/products/<gtin>"),
    (("GET",), "/eml/<mid>"),
    (("GET",), "/favicon.ico"),
    (("GET",), "/files/<mid>/<int:idx>"),
    (("GET",), "/health"),
    (("GET",), "/login"),
    (("POST",), "/login"),
    (("GET",), "/logout"),
    (("GET",), "/otazky"),
    (("GET",), "/otazky-dl"),
    (("GET",), "/sklad-dl/<k>"),
    (("GET",), "/sklad/<k>"),
    (("GET",), "/version"),
    (("GET",), "/znalosti"),
    (("GET",), "/znalosti/<ean>"),
])


def _current_routes(app):
    rules = []
    for r in app.url_map.iter_rules():
        if r.endpoint == "static":
            continue
        methods = tuple(sorted(m for m in (r.methods or ()) if m not in ("HEAD", "OPTIONS")))
        rules.append((methods, r.rule))
    return sorted(rules)


def test_route_table_matches_the_pre_split_baseline():
    """Every route `create_app()` registers today, exactly. A route dropped, renamed, or
    mistyped during the #268 split (e.g. `/api/znalosti/dl-products` -> a typo'd
    `dl_products`) silently widens or narrows who `_gate()` covers — this is the cheapest
    precise proof nothing in the table moved."""
    app = create_app(Config(pg_dsn="postgresql://unused", data_dir="/tmp", api_token="t",
                            dash_password="p", secret_key="s"))
    actual = _current_routes(app)
    missing = sorted(set(EXPECTED_ROUTES) - set(actual))
    added = sorted(set(actual) - set(EXPECTED_ROUTES))
    assert not missing and not added, (
        f"route table changed since the #268 pre-split baseline.\n"
        f"MISSING (in baseline, not in current app): {missing}\n"
        f"UNEXPECTED (in current app, not in baseline): {added}\n"
        f"If this is a legitimate route change, update EXPECTED_ROUTES above in the "
        f"same PR/commit.")


# ---- 2. HTML template constant checksums ---------------------------------------------

# sha256 of the fully-rendered constant (module-level string, before the request-time
# `.replace("__VERSION__", ...)` substitution) captured from HEAD `bd9e28e`.
#
# ASK_HTML / ASK_DL_HTML are hashed instead of the shared `_ASK_HTML_TEMPLATE` they are
# both built from (`_ASK_HTML_TEMPLATE.replace(...)` x5 placeholders each): any mutation
# to `_ASK_HTML_TEMPLATE` itself changes both derived hashes too (it is their strict
# superset of content), AND this also catches a bug in the five-`.replace()` chain that
# builds each derived page (e.g. a mistyped placeholder that leaves `__TITLE__` literally
# in the response) — which hashing `_ASK_HTML_TEMPLATE` alone would miss. Hashing all
# three together would just be checking the same shared bytes a third time for zero
# additional protection, so `_ASK_HTML_TEMPLATE` is deliberately NOT in this dict.
EXPECTED_TEMPLATE_SHA256 = {
    "LOGIN_HTML": "d1eb57ea9d855df8d1b580ce2fcdc8135329c9ada4eb47bc2ec78bce9e20313c",
    # #369: re-pinned — the customer board card gained a "Nie je to objednávka — takéto
    # maily ignoruj" button (DASH_HTML's admin customer block directly; ASK_HTML/ASK_DL_HTML
    # via the shared _ASK_HTML_TEMPLATE's customerQuestionCard both derive from).
    "DASH_HTML": "b64079c1743e3e468992c17e07ec4330fdecedaa65580697e9273619a93a61fc",
    "ASK_HTML": "6a6042fbb6748849fa58166849557355e813ab9aee13cc7208ae9e385a893cd5",
    "ASK_DL_HTML": "ae9282bb60588806f5ac9fd1e7a973627b9d0eba92423be87fa9c830ee8aeef5",
    "ZNALOSTI_HTML": "9f04bd74b57f0e22b8b2e7b7810995f5955b1b7796925dd259f44b68eac598b7",
}

_TEMPLATE_CONSTANTS = {
    "LOGIN_HTML": LOGIN_HTML,
    "DASH_HTML": DASH_HTML,
    "ASK_HTML": ASK_HTML,
    "ASK_DL_HTML": ASK_DL_HTML,
    "ZNALOSTI_HTML": ZNALOSTI_HTML,
}


def test_html_template_constants_match_their_pre_split_checksum():
    """A retyped-instead-of-copied character in one of these five constants during the
    #268 step-4 move (1226 lines, pure string relocation) is invisible to every other
    test in this suite — none of them assert on raw response bytes, only on DOM
    behaviour. This is the byte-parity proof the plan explicitly asks for."""
    mismatches = []
    for name, value in _TEMPLATE_CONSTANTS.items():
        actual = hashlib.sha256(value.encode("utf-8")).hexdigest()
        expected = EXPECTED_TEMPLATE_SHA256[name]
        if actual != expected:
            mismatches.append(f"  {name}: expected {expected} got {actual} "
                              f"(len={len(value)})")
    assert not mismatches, (
        "HTML template constant(s) changed since the #268 pre-split baseline:\n"
        + "\n".join(mismatches) +
        "\nIf this is a LEGITIMATE content change (not a copy/paste accident during the "
        "split), recompute with:\n"
        "  python3 -c \"import hashlib; from app.httpapi import <NAME>; "
        "print(hashlib.sha256(<NAME>.encode('utf-8')).hexdigest())\"\n"
        "and update EXPECTED_TEMPLATE_SHA256 above in the same commit.")


# ---- 3. /api/orders/digest happy path -------------------------------------------------

def _seed_todays_run(pg):
    """One order_runs + order_items row dated TODAY (the endpoint computes "today" via
    SQL now(), so this must use now() too, not a fixed date string like
    test_orders_reliability.py's `_run` helper uses).

    #277: `match_incidents.occurred_on` is seeded via SQL `now()::date - interval '3
    days'` (matching `test_orders_reliability.py`'s own idiom for the same table), NEVER
    Python `datetime.date.today()` — the endpoint (`reliability.days_since_incident()`)
    computes the day-delta entirely in SQL against Postgres's own `now()::date`, and the
    test container runs `TimeZone=Etc/UTC`. A Python-local seed disagrees with Postgres
    UTC for ~2h/night on a non-UTC host (00:00-02:00 CEST) once the local date rolls
    over before the UTC one does — this is the exact bug #277 fixed; keep the seed on
    Postgres's own clock so it never regresses."""
    rid = int(pg.execute(
        "INSERT INTO order_runs (message_id, shadow, status, started_at, finished_at, "
        "result) VALUES ('char-test-msg', false, 'ok', now(), now(), %s) RETURNING id",
        (Json({"orders": 1}),)).fetchone()[0])
    pg.execute("INSERT INTO order_items (run_id, name, rule) VALUES (%s, 'x', 'llm_sure')",
              (rid,))
    pg.execute(
        "INSERT INTO match_incidents (occurred_on, description, issue_ref) "
        "VALUES (now()::date - interval '3 days', 'characterization test seed', "
        "'#268-char-test')")


def test_orders_digest_happy_path_returns_todays_and_yesterdays_provenance_stats(pg):
    """test_httpapi.py:53 (`test_orders_digest_requires_a_session`) only ever proved the
    401-without-session boundary — there has never been a test that the endpoint actually
    returns real data once authenticated. Plan step 8 moves this route into
    `httpapi_reports.py`; without this test the move would carry zero net-new
    protection against a logic slip in `_db()`/`reliability` wiring."""
    _seed_todays_run(pg)
    c = _client()
    _login(c)
    r = c.get("/api/orders/digest")
    assert r.status_code == 200
    body = r.get_json()

    assert set(body) == {"today", "yesterday", "days_since_incident"}
    for key in ("today", "yesterday"):
        assert set(body[key]) == {"day", "runs", "orders", "errors", "items",
                                  "deterministic", "llm", "review"}

    # real, non-zero values from the seeded run — proves the endpoint actually queried
    # the DB rather than e.g. returning a stub/empty shape.
    assert body["today"]["runs"] == 1
    assert body["today"]["orders"] == 1
    assert body["today"]["items"] == 1
    assert body["today"]["llm"] == 1
    assert body["today"]["errors"] == 0
    # exercises reliability.py's subtraction-based bucketing (det_n = items_n - llm_n -
    # review_n) directly, not just implied by items/llm above (review-caught, PR #276).
    assert body["today"]["deterministic"] == 0
    assert body["today"]["review"] == 0
    assert body["yesterday"]["runs"] == 0

    # a real, live-computed value (never a hand-maintained constant) — the seeded
    # match_incidents row above makes this a concrete number, not the honest-None a
    # totally empty table would also correctly report.
    assert isinstance(body["days_since_incident"], int)
    assert body["days_since_incident"] >= 3
