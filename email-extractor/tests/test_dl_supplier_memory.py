"""Taught DL supplier addresses (#202, DL migration F3) — sender_email -> ean_edi mapping."""
from app.orders import dl_supplier_memory as dsm


def test_remember_then_resolve(pg):
    assert dsm.remember(pg, "obchod@mlynvrbovce.sk", "S1", "Mlyn Vrbovce s.r.o.") is True
    got = dsm.resolve(pg, "obchod@mlynvrbovce.sk")
    assert got == {"ean_edi": "S1", "name": "Mlyn Vrbovce s.r.o."}


def test_resolve_is_case_and_whitespace_insensitive(pg):
    dsm.remember(pg, "Obchod@MlynVrbovce.sk", "S1", "Mlyn Vrbovce s.r.o.")
    assert dsm.resolve(pg, "  obchod@mlynvrbovce.sk  ") == {"ean_edi": "S1",
                                                             "name": "Mlyn Vrbovce s.r.o."}


def test_resolve_unknown_address_is_none(pg):
    assert dsm.resolve(pg, "nikto@nikde.sk") is None


def test_a_second_teach_corrects_in_place_not_a_new_history_row(pg):
    dsm.remember(pg, "a@x.sk", "S1", "Prvý dodávateľ")
    dsm.remember(pg, "a@x.sk", "S2", "Druhý dodávateľ")
    assert dsm.resolve(pg, "a@x.sk") == {"ean_edi": "S2", "name": "Druhý dodávateľ"}
    assert pg.execute("SELECT count(*) FROM dl_supplier_memory").fetchone()[0] == 1


def test_forget_removes_the_taught_mapping(pg):
    dsm.remember(pg, "a@x.sk", "S1", "Dodávateľ")
    assert dsm.forget(pg, "a@x.sk") is True
    assert dsm.resolve(pg, "a@x.sk") is None
    assert dsm.forget(pg, "a@x.sk") is False, "already gone — forget is not idempotent-true"


def test_missing_fields_are_refused(pg):
    assert dsm.remember(pg, "", "S1", "x") is False
    assert dsm.remember(pg, "a@x.sk", "", "x") is False
    assert dsm.resolve(pg, "") is None
    assert dsm.forget(pg, "") is False
