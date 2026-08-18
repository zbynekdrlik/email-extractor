"""#342 req 5: the conservative promo / newsletter detector."""
import pytest

from app.orders.promo import looks_like_promo


@pytest.mark.parametrize("subject, sender", [
    ("AKCIA -20% na pečivo", "newsletter@velkosklad.sk"),
    ("Nový leták s týždennými zľavami", "marketing@dodavatel.sk"),
    ("VÝPREDAJ skladových zásob", "noreply@shop.sk"),
    ("Novinky v ponuke", "news@firma.sk"),
    ("Zľavnené produkty tento týždeň", "mailing@velko.sk"),
    ("Akcie na tento mesiac", "no-reply@e-shop.sk"),
])
def test_bulk_sender_plus_promo_subject_is_promo(subject, sender):
    assert looks_like_promo(subject, sender) is True


def test_list_unsubscribe_header_alone_is_promo():
    # The strongest signal: even a neutral subject/sender is promo when the bulk header is set.
    assert looks_like_promo("Ponuka", "obchod@firma.sk",
                            list_unsubscribe="<mailto:unsub@x>") is True


@pytest.mark.parametrize("form", [
    "AKCIA", "akcie", "akciový leták",           # akci
    "leták", "letáky", "reklamný leták",         # letak
    "zľava", "zľavy", "zľavnené",                # zlav
    "výpredaj", "výpredaji",                     # vypredaj
    "novinka", "novinky",                        # novink
])
def test_promo_subject_stems_match_their_slovak_inflections(form):
    # Verified against real inflected forms, not an assumed ASCII stem (the #265 lesson).
    assert looks_like_promo(form, "newsletter@x.sk") is True


@pytest.mark.parametrize("subject, sender", [
    ("Re: objednávka na zajtra", "objednavky.pno.martin@gmail.com"),  # real order (prod #59)
    ("Objednávka 20ks rožkov", "sjbarancek2@gmail.com"),              # real order (prod #68)
    ("Dobrý deň, prosím o dodanie", "klient@pekaren.sk"),
])
def test_real_orders_are_not_promo(subject, sender):
    assert looks_like_promo(subject, sender) is False


def test_promo_word_without_a_bulk_sender_still_asks():
    # A real customer asking about an akcia is NOT dropped — bulk sender is required too.
    assert looks_like_promo("akcia na sklade? máme záujem objednať",
                            "klient@pekaren.sk") is False


def test_bulk_sender_without_a_promo_subject_still_asks():
    # A noreply automation with a neutral subject is not dropped without a promo signal.
    assert looks_like_promo("Potvrdenie prijatia", "noreply@system.sk") is False


def test_empty_inputs_are_not_promo():
    assert looks_like_promo("", "") is False
    assert looks_like_promo("", "", "", "") is False
