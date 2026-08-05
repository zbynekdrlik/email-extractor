"""Extra-content detection (#133 "ZMENA ROZHODNUTIA", 2026-08-05): a deterministic
pre-filter decides whether ANY meaningful text is left over once the known static-order
template is subtracted — only then does an LLM call happen. All fixtures below are
SYNTHETIC, matching the documented template shapes, never real customer mail.
"""
from app.orders import static_extra as se

KARMEN_TEXT = (
    "Vyšlá objednávka č.: 12345/2026\n"
    "KARMEN 7, Prešov\n"
    "Prev.:7\n"
    "Dátum vystavenia: 01.08.2026\n"
    "Termín dodávky: 03.08.2026\n"
    "Množstvo\n"
    "8588001800013 Rožok štandart 50g 10,000 ks 0,50\n"
    "Nákupná cena spolu\n"
)


def test_a_pure_template_mail_has_no_residual():
    assert se.residual_text(KARMEN_TEXT) == ""
    assert se.has_meaningful_residual(se.residual_text(KARMEN_TEXT)) is False


def test_a_genuine_customer_addition_survives_as_residual():
    text = KARMEN_TEXT + "\nProsím doructe tentokrat na inu adresu, sme v rekonstrukcii.\n"
    residual = se.residual_text(text)
    assert "doructe" in residual.lower() or "inu adresu" in residual.lower()
    assert se.has_meaningful_residual(residual) is True


def test_common_boilerplate_signature_is_not_flagged_as_residual():
    text = KARMEN_TEXT + (
        "\nS pozdravom\n"
        "Ján Novák\n"
        "tel: 0911 223 344\n"
        "e-mail: jan.novak@karmen.sk\n"
        "KARMEN - velkoobchod potravin s.r.o.\n"
        "ICO: 12345678\n"
    )
    residual = se.residual_text(text)
    assert se.has_meaningful_residual(residual) is False, \
        f"boilerplate must not be flagged as extra content, got: {residual!r}"


def test_a_short_stray_fragment_is_below_the_meaningful_threshold():
    assert se.has_meaningful_residual("ok") is False
    assert se.has_meaningful_residual("") is False
    assert se.has_meaningful_residual("ahoj, dakujem") is True


def test_two_line_karmen_cash_item_layout_does_not_falsely_flag_description_lines():
    """The KARMEN_CASH/LABAS two-physical-lines-per-item shape has a bare description
    line with no header/item keyword of its own — the WHOLE item-block span (bookends +
    every line in between) must be consumed as one blob, not line-by-line, or a normal
    product description would be wrongly treated as extra content."""
    text = (
        "prevádzka: KARMEN CASH AND CARRY Zvolen, Námestie SNP 10\n"
        "Termín dodávky: 03.08.2026\n"
        "Dátum vystavenia: 01.08.2026\n"
        "Int. kód a názov tovaru\n"
        "Katalógové číslo a názov tovaru\n"
        "1 K001 10,5 KS 8588001805647 1 2,50 26,25\n"
        "Rožok štandart 50g\n"
        "Poznámka:\n"
    )
    residual = se.residual_text(text)
    assert "Rožok štandart" not in residual, \
        f"an item description line must be consumed by the item-block span, got: {residual!r}"


def test_prompt_file_exists_and_is_non_empty():
    assert len(se.prompt()) > 50
