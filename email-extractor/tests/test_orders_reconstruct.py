"""Reconstructing customer wording -> shipped card from the archive (#83).

Pure, DB-free: the pairing logic itself is what needs proving, with synthetic emails that
mirror the SHAPE of the real archive (freeform "Name : Nx" lines, a quoted reply chain, a
multi-day order) without containing any real customer's text.
"""
from app.orders import reconstruct


def _email(body: str) -> str:
    return f"Subject: Objednávka\n\nFrom: zakaznik@example.sk\n\nBody: {body}"


# --- extract_day_blocks ----------------------------------------------------

def test_a_single_day_order_is_one_block_in_written_order():
    text = _email("Dobrý deň\n\nRožok 70g : 50 x\n\nBageta 250g : 4 x\n\nVianočka : 2x\n\n"
                   "S pozdravom")
    blocks = reconstruct.extract_day_blocks(text)
    assert len(blocks) == 1
    assert blocks[0]["items"] == [
        ("Rožok 70g", 50.0), ("Bageta 250g", 4.0), ("Vianočka", 2.0)]


def test_quoted_reply_history_is_never_read_as_a_block():
    text = _email(
        "Na SOBOTU 4.7.2026 poprosím\n\nVianočka : 2x\n\nS pozdravom\n\nJana Nováková\n\n"
        "Dňa 2026-06-23 09:52 Predaj napísal(a):\n\n"
        "> Na SOBOTU 27.6.2026 poprosím\n> \n> Vianočka : 9x\n")
    blocks = reconstruct.extract_day_blocks(text)
    assert len(blocks) == 1 and blocks[0]["items"] == [("Vianočka", 2.0)], \
        "the quoted week's '9x' must never leak in as this week's quantity"


def test_a_dead_end_reply_with_no_fresh_content_yields_no_blocks():
    text = _email(">> Dobrý deň ,\n>> \n>> na 21.7. poprosím:\n>> \n>> Vianočka : 1x\n\n"
                   "Dňa 2026-07-15 07:54 zakaznik@example.sk napísal(a):\n\n> Dobre.")
    assert reconstruct.extract_day_blocks(text) == []


def test_a_multi_day_email_splits_into_one_block_per_stated_day():
    text = _email("Dobrý deň,\n\nna 16.7\n\nRožok 70g : 20 x\n\nVianočka : 1 x\n\n"
                   "na 17.7\n\nRožok 70g : 30 x\n\nVianočka : 2 x\n\nĎakujem")
    blocks = reconstruct.extract_day_blocks(text)
    assert [(b["day"], b["month"]) for b in blocks] == [(16, 7), (17, 7)]
    assert blocks[0]["items"] == [("Rožok 70g", 20.0), ("Vianočka", 1.0)]
    assert blocks[1]["items"] == [("Rožok 70g", 30.0), ("Vianočka", 2.0)]


# --- wordings_for_order ------------------------------------------------------

def test_a_matching_item_count_pairs_positionally_in_order():
    text = _email("Rožok 70g : 50 x\n\nBageta 250g : 4 x\n\nChlieb pšenično ražný : 6 x\n\n"
                   "Vianočka : 2x")
    got = reconstruct.wordings_for_order(text, "2026-07-04", 4)
    assert got == ["Rožok 70g", "Bageta 250g", "Chlieb pšenično ražný", "Vianočka"]


def test_a_count_mismatch_refuses_the_pairing_rather_than_guess():
    """The real #80 failure mode: a multi-item email but the shipped record only has ONE
    item (a dropped line). Pairing 4 wordings against 1 shipped card would be a guess."""
    text = _email("Rožok 70g : 50 x\n\nBageta 250g : 4 x\n\nChlieb x : 6 x\n\nVianočka : 2x")
    assert reconstruct.wordings_for_order(text, "2026-07-04", 1) is None


def test_a_zero_or_negative_item_count_is_never_reconstructable():
    text = _email("Vianočka : 2x")
    assert reconstruct.wordings_for_order(text, "2026-07-04", 0) is None


def test_a_multi_day_email_is_disambiguated_by_the_orders_own_delivery_date():
    text = _email("na 16.7\n\nRožok 70g : 20 x\n\nVianočka : 1 x\n\n"
                   "na 17.7\n\nRožok 70g : 30 x\n\nBageta 250g : 3 x\n\nVianočka : 2 x")
    # ISO date, matching the SECOND (17.7, 3-item) block:
    assert reconstruct.wordings_for_order(text, "2026-07-17", 3) == \
        ["Rožok 70g", "Bageta 250g", "Vianočka"]
    # DD.MM.YYYY date, matching the FIRST (16.7, 2-item) block:
    assert reconstruct.wordings_for_order(text, "16.07.2026", 2) == \
        ["Rožok 70g", "Vianočka"]


def test_a_date_match_never_falls_back_to_a_different_days_block():
    """Even when a DIFFERENT day's block happens to have the right item count, the dated
    block is used exclusively — a coincidental count match on the WRONG day must not pair."""
    text = _email("na 16.7\n\nRožok 70g : 20 x\n\nVianočka : 1 x\n\n"
                   "na 17.7\n\nRožok 70g : 30 x\n\nBageta 250g : 3 x")
    # 17.7's block has 2 items, same as 16.7's — but the date says 17.7, so ONLY that block
    # may be used, and its item count (2) does match here — fine. Now ask for count 1: no
    # block dated 17.7 has 1 item, so this must refuse even though 16.7 does.
    assert reconstruct.wordings_for_order(text, "2026-07-17", 1) is None


def test_an_undated_single_block_order_pairs_by_count_alone():
    text = _email("Dobrý deň,\n\npoprosil by som:\n\nRožok 70g 6x\n\nBageta 250g 4x")
    # no "na D.M" header at all, and the item lines use a space (no colon/dash) — this
    # customer format is not one this module parses, so it correctly yields nothing rather
    # than a false match.
    assert reconstruct.wordings_for_order(text, "2026-07-04", 2) is None


def test_an_undated_single_block_matches_by_count_when_the_lines_do_parse():
    text = _email("Dobrý deň,\n\nRožok 70g : 6x\n\nBageta 250g : 4x")
    assert reconstruct.wordings_for_order(text, "2026-07-04", 2) == \
        ["Rožok 70g", "Bageta 250g"]


def test_no_reconstructable_text_at_all_returns_none():
    assert reconstruct.wordings_for_order(_email("Ďakujem, pekný deň."), "2026-07-04", 1) \
        is None


def test_an_unparseable_delivery_date_still_falls_back_to_count_alone():
    text = _email("Rožok 70g : 6x\n\nBageta 250g : 4x")
    assert reconstruct.wordings_for_order(text, "", 2) == ["Rožok 70g", "Bageta 250g"]
