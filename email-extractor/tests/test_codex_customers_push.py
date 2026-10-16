"""#493: the dev2 CODEX customer-cards push tool — CI-testable via the DI seam, and its SQL run
for real against a synthetic DuckDB file (duckdb is a dev dependency; the tool imports it
lazily so the add-on image never needs it).

Synthetic firms only (invented names, EANs, addresses) — this repo is public.
"""
import datetime

import duckdb

from tools import codex_customers_push as push


def _firma(path, rows):
    con = duckdb.connect(str(path))
    con.execute("CREATE SCHEMA raw")
    con.execute("CREATE SCHEMA meta")
    con.execute("""CREATE TABLE raw.firma (NICO DOUBLE, IICODOPL BIGINT, DDATVYPU DATE,
                   AEDIEAN VARCHAR, ANAZORG VARCHAR, AOBCHODM VARCHAR, AULICA VARCHAR,
                   AOBEC VARCHAR, APSC VARCHAR, AEMAIL VARCHAR)""")
    con.executemany("INSERT INTO raw.firma VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", rows)
    con.execute("CREATE TABLE meta.etl_runs (table_name VARCHAR, status VARCHAR, "
                "finished_at TIMESTAMP)")
    con.execute("INSERT INTO meta.etl_runs VALUES ('firma', 'ok', '2026-10-15 16:02:24'), "
                "('sm002', 'ok', '2026-10-15 17:00:00')")
    con.close()


D1, D2 = datetime.date(2026, 10, 1), datetime.date(2026, 10, 15)
ROWS = [
    # one shop, two GENERATIONS: the newest extract wins (new street + two addresses)
    (1001.0, None, D1, "2999000000310", "Tržnica Mestečko", "Tržnica", "Stará 1", "Mestečko",
     "036 01", "stara@retazec-test.sk"),
    (1001.0, None, D2, "2999000000310", "Tržnica Mestečko - Riadok 1", "Tržnica Mestečko",
     "Riadok 1", "Mestečko", "036 01", "Nakup@Retazec-Test.sk; druhy@retazec-test.sk,"),
    # a branch of the same firm, its own EAN
    (1001.0, 1, D2, "2999000000311", "Dolina Makovická", "", "Makovická", "Dolina", "010 01",
     "nakup@retazec-test.sk"),
    # an EAN on TWO different firms: imported for neither, reported
    (1001.0, 2, D2, "2999000000399", "Predajňa Lipová", "", "Lipová 6", "Mestečko", "",
     "nakup@retazec-test.sk"),
    (2002.0, None, D2, "2999000000399", "Iná firma s.r.o.", "", "Centrum 3", "Hôrka", "", None),
    # no EAN -> skipped; no name at all -> trade name, else skipped
    (3003.0, None, D2, None, "Bez EAN", "", "", "", "", None),
    (4004.0, None, D2, "2999000000312", "", "Len obchodné meno", "", "Ves", "", None),
]


def test_the_sql_keeps_the_newest_generation_per_firm_and_branch(tmp_path):
    _firma(tmp_path / "c.duckdb", ROWS)
    rows = push.query_duckdb(str(tmp_path / "c.duckdb"))
    by = {(r["nico"], r["branch"], r["ean"]): r for r in rows}
    assert ("1001", None, "2999000000310") in by
    assert by[("1001", None, "2999000000310")]["street"] == "Riadok 1"
    assert len([r for r in rows if r["ean"] == "2999000000310"]) == 1
    assert all(r["ean"] for r in rows), "a firm without an EDI EAN is never fetched"
    assert push.query_as_of(str(tmp_path / "c.duckdb")) == datetime.datetime(2026, 10, 15, 16,
                                                                             2, 24)


def test_build_customers_splits_addresses_and_never_imports_an_ambiguous_ean(tmp_path):
    _firma(tmp_path / "c.duckdb", ROWS)
    customers, ambiguous = push.build_customers(push.query_duckdb(str(tmp_path / "c.duckdb")))
    by = {c["ean_edi"]: c for c in customers}
    assert set(by) == {"2999000000310", "2999000000311", "2999000000312"}
    shop = by["2999000000310"]
    assert shop["emails"] == ["nakup@retazec-test.sk", "druhy@retazec-test.sk"]
    assert (shop["name"], shop["trade_name"], shop["city"], shop["zip"]) == (
        "Tržnica Mestečko - Riadok 1", "Tržnica Mestečko", "Mestečko", "036 01")
    assert by["2999000000312"]["name"] == "Len obchodné meno"
    assert [a["ean_edi"] for a in ambiguous] == ["2999000000399"]
    assert {f["name"] for f in ambiguous[0]["firms"]} == {"Predajňa Lipová",
                                                          "Iná firma s.r.o."}


def test_the_same_firm_row_twice_is_one_card_not_an_ambiguity():
    row = {"nico": "1", "branch": None, "ean": "2999000000320", "name": "A", "emails": ""}
    customers, ambiguous = push.build_customers([row, dict(row)])
    assert len(customers) == 1 and ambiguous == []


def test_ean_and_email_normalization():
    assert push.ean(" 2999 000-000320 ") == "2999000000320"
    assert push.ean("SK123") == ""
    assert push.emails("A@B.sk; c@d.sk,a@b.sk  x") == ["a@b.sk", "c@d.sk"]


def test_run_posts_one_body_with_the_etl_time_and_the_token():
    posted = []

    def poster(url, headers, body):
        posted.append((url, headers, body))
        return {"stored": 1, "snapshot_id": 45}

    rows = [{"nico": "1", "branch": None, "ean": "2999000000320", "name": "A",
             "emails": "a@b.sk"}]
    res = push.run("https://addon/api/codex/customers", "tok", query=lambda: rows,
                   as_of=lambda: datetime.datetime(2026, 10, 15, 16, 2, 24), poster=poster)
    url, headers, body = posted[0]
    assert headers["X-Token"] == "tok"
    assert body["source_as_of"] == "2026-10-15T16:02:24+00:00"
    assert [c["ean_edi"] for c in body["customers"]] == ["2999000000320"]
    assert res == {"fetched": 1, "customers": 1, "ambiguous": 0, "stored": 1,
                   "source_as_of": "2026-10-15T16:02:24+00:00", "snapshot_id": 45}
    line = push.pushed_line(res, "https://user:pw@addon:8443/api/codex/customers?x=1")
    assert line.endswith("to=https://addon:8443") and "snapshot=45" in line


def test_an_empty_list_is_never_posted():
    posted = []
    res = push.run("u", "t", query=lambda: [], as_of=lambda: None,
                   poster=lambda *a: posted.append(a))
    assert posted == [] and res["error"]


def test_the_url_is_derived_from_the_orders_push_url():
    assert (push.customers_url("https://addon.example/api/codex/orders")
            == "https://addon.example/api/codex/customers")


def test_main_requires_url_and_token(monkeypatch, capsys):
    monkeypatch.delenv("CODEX_PUSH_URL", raising=False)
    monkeypatch.delenv("CODEX_CUSTOMERS_PUSH_URL", raising=False)
    monkeypatch.delenv("CODEX_PUSH_TOKEN", raising=False)
    assert push.main([]) == 2
    assert "required" in capsys.readouterr().err


def test_main_dry_run_counts_without_posting(tmp_path, capsys):
    _firma(tmp_path / "c.duckdb", ROWS)
    assert push.main(["--dry-run", "--db", str(tmp_path / "c.duckdb")]) == 0
    out = capsys.readouterr().out
    assert "customers=3 ambiguous=1 (2999000000399)" in out
