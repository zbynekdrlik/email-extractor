"""One-off migration (#104): import the sheet's `doplnok` column into global_item_memory
before the column stops feeding the matching ladder. Global, not per-customer — the alias
lives on the CARD in catalog_snapshot, not on any one buyer (see the #104 design comment)."""
from app.orders import alias_migration, memory, snapshot


def _snap(pg, catalog):
    """Freeze one catalog (with the given aliases) + a minimal customer row."""
    customer_csv = "Názov organizácie,EAN kód EDI,E-mail\nZákazník,999,z@z.sk\n"
    header = "GTIN,Názov,doplnok\n"
    rows = "\n".join(f'{c["gtin"]},{c["name"]},"{c.get("alias", "")}"' for c in catalog)
    return snapshot.import_snapshot(pg, header + rows + "\n", customer_csv)


# --- split_alias -----------------------------------------------------------------------

def test_split_alias_handles_comma_semicolon_and_slash():
    assert alias_migration.split_alias("rožok, šiška; vianočka/kalač") == \
        ["rožok", "šiška", "vianočka", "kalač"]


def test_split_alias_drops_short_noise_tokens():
    assert alias_migration.split_alias("ab, rožok") == ["rožok"]


def test_split_alias_of_empty_is_empty():
    assert alias_migration.split_alias("") == []
    assert alias_migration.split_alias(None) == []


# --- migrate -----------------------------------------------------------------------------

def test_migrate_imports_every_alias_as_a_global_wording(pg):
    _snap(pg, [{"gtin": "G1", "name": "Rožok štandart 50g", "alias": "rožok, rožtek"},
              {"gtin": "G2", "name": "Vianočka 400g", "alias": "twister"}])
    result = alias_migration.migrate(pg)
    assert result == {"cards": 2, "wordings": 3, "imported": 3}
    assert memory.resolve_global(pg, "rožok").gtin == "G1"
    assert memory.resolve_global(pg, "rožtek").gtin == "G1"
    assert memory.resolve_global(pg, "twister").gtin == "G2"


def test_migrate_records_the_source_as_sheet_import(pg):
    _snap(pg, [{"gtin": "G1", "name": "Rožok štandart 50g", "alias": "rožok"}])
    alias_migration.migrate(pg)
    row = pg.execute("SELECT taught_by FROM global_item_memory WHERE item_key = %s",
                     (memory.item_key("rožok"),)).fetchone()
    assert row[0] == "sheet-import"


def test_migrate_skips_a_card_with_no_alias(pg):
    _snap(pg, [{"gtin": "G1", "name": "Rožok štandart 50g", "alias": ""}])
    assert alias_migration.migrate(pg) == {"cards": 0, "wordings": 0, "imported": 0}


def test_migrate_is_idempotent(pg):
    _snap(pg, [{"gtin": "G1", "name": "Rožok štandart 50g", "alias": "rožok"}])
    alias_migration.migrate(pg)
    result = alias_migration.migrate(pg)
    assert result == {"cards": 1, "wordings": 1, "imported": 0}
    assert memory.resolve_global(pg, "rožok").gtin == "G1"


def test_migrate_never_overwrites_an_existing_human_teaching(pg):
    """A human answer always outranks a bulk sheet import — first-teach-wins is the same
    rule remember_global already enforces for two humans (#102); a later sheet import must
    not be able to silently take that back."""
    memory.add_global_alias(pg, "rožok", "HUMAN", "Rožok kváskový 70g", by="sklad")
    _snap(pg, [{"gtin": "G1", "name": "Rožok štandart 50g", "alias": "rožok"}])
    alias_migration.migrate(pg)
    assert memory.resolve_global(pg, "rožok").gtin == "HUMAN"


def test_migrate_with_no_snapshot_does_nothing(pg):
    assert alias_migration.migrate(pg) == {"cards": 0, "wordings": 0, "imported": 0}
