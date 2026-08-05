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


KARMEN_CASH_TEXT = (
    "prevádzka: KARMEN CASH AND CARRY Zvolen, Námestie SNP 10\n"
    "Termín dodávky: 03.08.2026\n"
    "Dátum vystavenia: 01.08.2026\n"
    "Int. kód a názov tovaru\n"
    "Katalógové číslo a názov tovaru\n"
    "1 K001 10,5 KS 8588001805647 1 2,50 26,25\n"
    "Rožok štandart 50g\n"
    "Poznámka:\n"
)

LABAS_TEXT = (
    "LABAS s.r.o. KS/OC Predajňa Zvolen MOBIL 0900123456\n"
    "Termín dodania: 03.08.2026\n"
    "Dátum vystavenia: 01.08.2026\n"
    "Celkom\n"
    "1 45231 K002 Chlieb ražný 1000g 3 10,5 ks 1,20 12,60 132,30\n"
    "Kusový EAN kód: 8588001805579\n"
    "Celková hmotnosť: 10 kg\n"
)


def test_two_line_karmen_cash_item_layout_does_not_falsely_flag_description_lines():
    """The KARMEN_CASH/LABAS two-physical-lines-per-item shape has a bare description
    line with no header/item keyword of its own — the WHOLE item-block span (bookends +
    every line in between) must be consumed as one blob, not line-by-line, or a normal
    product description would be wrongly treated as extra content."""
    residual = se.residual_text(KARMEN_CASH_TEXT)
    assert "Rožok štandart" not in residual, \
        f"an item description line must be consumed by the item-block span, got: {residual!r}"


def test_a_plain_karmen_cash_order_costs_no_llm_call():
    """Review finding (PR #182): the "Int. kód a názov tovaru" header line — part of the
    real template (it's what `extract_order_data` itself keys its dispatch on) — was NOT
    in the header/item-block pattern list, so EVERY KARMEN_CASH order false-positived.
    Pinning `has_meaningful_residual` (not just a substring check) is what would have
    caught this the first time."""
    residual = se.residual_text(KARMEN_CASH_TEXT)
    assert se.has_meaningful_residual(residual) is False, \
        f"a plain KARMEN_CASH order must never trigger the LLM check, got: {residual!r}"


def test_a_plain_labas_order_costs_no_llm_call():
    residual = se.residual_text(LABAS_TEXT)
    assert se.has_meaningful_residual(residual) is False, \
        f"a plain LABAS order must never trigger the LLM check, got: {residual!r}"


def test_labas_trailing_weight_value_does_not_leak_as_residual():
    """Review finding (PR #182): the item-block end-boundary only matched the anchor
    WORD ("Celkov[áa] hmotnos"), not the rest of that line — a real weight value after it
    used to leak through as "residual" (e.g. "ť: 123,45 kg")."""
    text = LABAS_TEXT.replace("Celková hmotnosť: 10 kg", "Celková hmotnosť: 123,45 kg")
    residual = se.residual_text(text)
    assert "123,45" not in residual, f"the trailing weight value leaked: {residual!r}"
    assert se.has_meaningful_residual(residual) is False


def test_a_genuine_complaint_starting_with_vasa_is_not_swallowed():
    """Review finding (PR #182): the old `^Vaš[a]\\b` boilerplate pattern matched ANY
    line starting with the very common Slovak possessive "Vaša" — including a genuine
    complaint, silently erasing it with zero trace before any LLM ever saw it."""
    text = KARMEN_TEXT + "\nVaša faktúra za minulý mesiac bola nesprávna, prosím opravte.\n"
    residual = se.residual_text(text)
    assert "faktúra" in residual.lower() or "nespravna" in residual.lower() \
        or "nesprávna" in residual.lower()
    assert se.has_meaningful_residual(residual) is True


def test_a_request_sharing_a_line_with_a_phone_number_is_not_swallowed():
    """Review finding (PR #182): the old `tel\\.?:?\\s*[+\\d]` pattern was unanchored —
    it matched ANY line merely MENTIONING a phone number, even one carrying a genuine
    request, and dropped the whole line."""
    text = KARMEN_TEXT + (
        "\nVolajte mi na tel. 0911 223 344 dnes poobede, chcem zmenit adresu dodania.\n")
    residual = se.residual_text(text)
    assert "zmenit adresu" in residual.lower() or "dodania" in residual.lower()
    assert se.has_meaningful_residual(residual) is True


def test_a_bare_phone_number_line_is_still_treated_as_boilerplate():
    text = KARMEN_TEXT + "\ntel: 0911 223 344\n"
    residual = se.residual_text(text)
    assert se.has_meaningful_residual(residual) is False


def test_a_duplicated_template_eg_a_forwarded_quote_is_fully_consumed():
    """Review finding (PR #182): `_strip_consumed` only matched the FIRST occurrence of
    each pattern — a forwarded mail with the template appearing twice (a quoted original
    below a customer's own note) left the second copy sitting in the residual."""
    forwarded = f"Poslané ďalej:\n\n{KARMEN_TEXT}\n\n{KARMEN_TEXT}"
    residual = se.residual_text(forwarded)
    assert "Vyšlá objednávka" not in residual
    assert "Rožok štandart" not in residual


def test_prompt_file_exists_and_is_non_empty():
    assert len(se.prompt()) > 50
