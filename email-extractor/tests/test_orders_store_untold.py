"""#493 review round 11 — an order whose shop the mail does not tell is never folded into one.

* a form order whose file could not be told (its line is also in the body, or the forms
  share the row) next to a named shop still makes the mail one of several shops — for a
  sender WITHOUT learned rules both are asked, never shipped to its one card;
* a header that is only noise („Objednávka 04.08.", „43 KT", „KW 42", „Pondelok") names no
  shop block, so two goods files of ONE shop stay one order (#81.1);
* a table mail whose model names two shops is read by the model (the table path keeps only
  the first order).

Synthetic data only (the repo is public).
"""
import os

from app.config import Config
from app.orders import extract, pipeline, snapshot, store_key

PG_DSN = os.environ.get("PG_TEST_DSN")
SENDER = "nakup@retazec-test.sk"
CATALOG = ("GTIN,Sklad,Názov,doplnok\n"
           "ROZ,1,Rožky,\n"
           "CHL,1,Chlieb kváskový 1kg,\n"
           "VIA,1,Vianočka 400g,\n")
CUSTOMERS = ("Názov organizácie,EAN kód EDI,Obec,Ulica,Meno pre fakturáciu,Číslo mobilu,E-mail\n"
             "Tržnica Mestečko,2999000000202,Mestečko,Riadok 1,,,\n"
             f"Centrála reťazca,2999000000101,Mestečko,Hlavná 9,,,{SENDER}\n")
GTIN = {"Rožky": "ROZ", "Chlieb kváskový 1kg": "CHL", "Vianočka 400g": "VIA"}
HDR = "Názov\tcena bez DPH\tcena s DPH\tks\n"


def _cfg():
    return Config(pg_dsn=PG_DSN, data_dir="/tmp", api_token="tok", dash_password="pw",
                  secret_key="t", orders_shadow=False, odoo_url="", odoo_api_key="",
                  orders_channel_id=0)


class Client:
    """Answers by call name — the item answer by the product named in the prompt."""
    last_prompt_hash = "untold123456"

    def __init__(self, orders):
        self.orders = orders

    def json_call(self, system, user, schema, name="result"):
        if name == "orders":
            return {"senderName": "Nákup", "senderEmail": SENDER, "companyName": "Reťazec",
                    "isChangeRequest": False, "notes": "", "orders": self.orders}
        if name == "customer":
            return {"ean_edi": "", "confidence": 0.2}
        for n, g in sorted(GTIN.items(), key=lambda kv: -len(kv[0])):
            if n in user:
                return {"gtin": g, "confidence": 0.95, "matchedCatalogName": "", "reason": ""}
        raise AssertionError(f"unscripted {name}")


class Rec:
    def __init__(self):
        self.uploads = []

    def upload(self, cfg, name, content):
        self.uploads.append((name, content))
        return True

    def post(self, cfg, html, transport=None):
        return {"id": 1}


def _it(name, qty, quote):
    return {"name": name, "quantity": qty, "unit": "ks", "sourceQuote": quote}


def _o(store, *items):
    return {"orderNumber": "", "deliveryDate": "04.08.2026", "recipientGroup": "",
            "store": store, "items": list(items)}


def _run(pg, mid, text, orders):
    snapshot.import_snapshot(pg, CATALOG, CUSTOMERS)
    pg.execute("INSERT INTO messages (message_id, category) VALUES (%s, 'ai_orders')", (mid,))
    rec = Rec()
    mail = {"message_id": mid, "subject": "objednávka", "from_addr": SENDER,
            "from_name": "Nákup", "combined_text": text, "today": "2026-07-30"}
    result = pipeline.run(pg, _cfg(), mail, snapshot.latest_snapshot_id(pg),
                          client=Client(orders), upload=rec.upload, post=rec.post)
    return [(r["customer_ean"], [i["gtin"] for i in r["items"]])
            for r in result["order_results"] if r["status"] == "ok"]


BODY_ROZKY = ("Subject: objednávka\n\nFrom: " + SENDER + "\n\nBody: Dobrý deň, posielam "
              "objednávky na zajtra, rožky prosím ráno.\n\nAttachments:\n"
              "===== OBJ Pekárová žena TR.xlsx =====\n[Sheet: H]\nPredajňa: Tržnica\n"
              "na 04.08.2026\n" + HDR + "Chlieb kváskový 1kg\t1.10\t1.30\t5\n\n"
              "===== OBJ Pekárová žena PR.xlsx =====\n[Sheet: H]\nna 04.08.2026\n"
              + HDR + "Rožky\t0.10\t0.12\t120\n")
SHARED_ROWS = ("Subject: objednávka\n\nFrom: " + SENDER + "\n\nBody: Dobrý deň, posielam "
               "objednávky.\n\nAttachments:\n"
               "===== OBJ Pekárová žena TR.xlsx =====\n[Sheet: H]\nPredajňa: Tržnica\n"
               "na 04.08.2026\n" + HDR + "Chlieb kváskový 1kg\t1.10\t1.30\t5\n"
               "Rožky\t0.10\t0.12\t\n\n"
               "===== OBJ Pekárová žena PR.xlsx =====\n[Sheet: H]\nna 04.08.2026\n"
               + HDR + "Chlieb kváskový 1kg\t1.10\t1.30\t\nRožky\t0.10\t0.12\t120\n")
TWO_SHOPS = [_o("Predajňa: Tržnica", _it("Chlieb kváskový 1kg", 5, "Chlieb kváskový 1kg | 5")),
             _o("", _it("Rožky", 120, "Rožky | 120"))]


def test_an_untold_form_order_next_to_a_named_shop_is_never_shipped_by_address(pg):
    """The PR form reports no header and its line („Rožky") is also in the body: its file is
    untold, so only Tržnica had an identity — one shop, not a multi-shop mail, and both
    orders went to the sender's one card."""
    assert _run(pg, "m-body", BODY_ROZKY, TWO_SHOPS) == []


def test_shared_rows_with_reformatted_quotes_are_never_shipped_by_address(pg):
    assert _run(pg, "m-shared", SHARED_ROWS, TWO_SHOPS) == []


def test_a_noise_only_header_names_no_block():
    def block(header):
        return store_key.names_a_block(store_key.for_orders([{"store": header}])[0])
    assert [block(h) for h in ("Objednávka 04.08.", "43 KT", "KW 42", "Pondelok", "16.10.")] \
        == [False] * 5
    assert [block(h) for h in ("Predajňa:", "BISTRO", "Prevádzka 2")] == [True] * 3


def test_two_goods_files_under_a_noise_header_stay_one_order(pg):
    """#81.1: ONE shop's goods split into two files, each sheet titled „Objednávka 04.08." —
    one order to the sender's card, exactly as before #493."""
    text = ("Subject: objednávka\n\nFrom: " + SENDER + "\n\nBody: Dobrý deň.\n\nAttachments:\n"
            "===== Objednávka pečivo.xlsx =====\n[Sheet: H]\nObjednávka 04.08.\nRožky 120 ks\n\n"
            "===== Objednávka chlieb.xlsx =====\n[Sheet: H]\nObjednávka 04.08.\n"
            "Chlieb kváskový 1kg 5 ks\n")
    orders = [_o("Objednávka 04.08.", _it("Rožky", 120, "Rožky 120 ks")),
              _o("Objednávka 04.08.", _it("Chlieb kváskový 1kg", 5, "Chlieb kváskový 1kg 5 ks"))]
    assert _run(pg, "m-goods", text, orders) == [("2999000000101", ["ROZ", "CHL"])]


def test_a_table_mail_whose_model_names_two_shops_is_read_by_the_model():
    """A filled price list (table path) plus another shop's order in a .docx: the table path
    kept only the first order — the BISTRO order vanished."""
    hdr = "Názov\t%DPH\tVO bez DPH\tVO s DPH\n"
    text = ("Subject: objednávka\n\nAttachments:\n===== Cennik.xlsx =====\n[Sheet: H]\n"
            "Predajňa: Alfa\n" + hdr + "Rožky\t20\t0.10\t0.12\t120\n\n"
            "===== BISTRO.docx =====\nBISTRO: 7x Vianočka 400g\n")
    orders = [_o("Predajňa: Alfa", _it("Rožky", 120, "Rožky\t20\t0.10\t0.12\t120")),
              _o("BISTRO", _it("Vianočka 400g", 7, "7x Vianočka 400g"))]

    class Model:
        last_prompt_hash = "x"

        def json_call(self, *a, **k):
            return {"senderName": "N", "senderEmail": SENDER, "companyName": "",
                    "isChangeRequest": False, "notes": "", "orders": orders}
    out = extract.run(Model(), {"combined_text": text, "today": "2026-07-30",
                                "subject": "objednávka"})
    assert out["source"] == "model"
    assert [o["store"] for o in out["orders"]] == ["Predajňa: Alfa", "BISTRO"]


# --- review round 12 ------------------------------------------------------------------------

def _mail(*files):
    s = ("Subject: objednávka\n\nFrom: " + SENDER + "\n\nBody: Dobrý deň, posielam "
         "objednávky.\n\nAttachments:\n")
    return s + "\n".join(f"===== {n} =====\n[Sheet: H]\n{t}" for n, t in files)


def test_a_wordless_file_next_to_one_named_shop_is_never_shipped_by_address(pg):
    text = _mail(("OBJ Pekárová žena TR.xlsx", "Predajňa: Tržnica\nna 04.08.2026\n" + HDR
                  + "Chlieb kváskový 1kg\t1.10\t1.30\t5\n"),
                 ("OBJ Pekárová žena.xlsx", "na 04.08.2026\n" + HDR + "Rožky\t0.10\t0.12\t120\n"))
    orders = [_o("Predajňa: Tržnica", _it("Chlieb kváskový 1kg", 5,
                                          "Chlieb kváskový 1kg\t1.10\t1.30\t5")),
              _o("", _it("Rožky", 120, "Rožky\t0.10\t0.12\t120"))]
    assert _run(pg, "m-wordless", text, orders) == []


def test_two_shops_named_by_a_single_letter_are_two(pg):
    text = _mail(("OBJ Pekárová žena A.xlsx", "Predajňa A\nna 04.08.2026\n" + HDR
                  + "Chlieb kváskový 1kg\t1.10\t1.30\t5\n"),
                 ("OBJ Pekárová žena B.xlsx", "Predajňa B\nna 04.08.2026\n" + HDR
                  + "Rožky\t0.10\t0.12\t120\n"))
    orders = [_o("Predajňa A", _it("Chlieb kváskový 1kg", 5,
                                   "Chlieb kváskový 1kg\t1.10\t1.30\t5")),
              _o("Predajňa B", _it("Rožky", 120, "Rožky\t0.10\t0.12\t120"))]
    assert _run(pg, "m-letters", text, orders) == []


def test_two_shops_headed_by_a_number_code_are_two(pg):
    text = _mail(("OBJ Pekárová žena 2.xlsx", "č. 2\nna 04.08.2026\n" + HDR
                  + "Chlieb kváskový 1kg\t1.10\t1.30\t5\n"),
                 ("OBJ Pekárová žena 3.xlsx", "č. 3\nna 04.08.2026\n" + HDR
                  + "Rožky\t0.10\t0.12\t120\n"))
    orders = [_o("č. 2", _it("Chlieb kváskový 1kg", 5, "Chlieb kváskový 1kg\t1.10\t1.30\t5")),
              _o("č. 3", _it("Rožky", 120, "Rožky\t0.10\t0.12\t120"))]
    assert _run(pg, "m-codes", text, orders) == []


def test_a_number_names_a_block_unless_an_order_or_sheet_word_owns_it():
    def block(header):
        return store_key.names_a_block(store_key.for_orders([{"store": header}])[0])
    assert [block(h) for h in ("č. 2", "A", "Predajňa 3")] == [True] * 3
    assert [block(h) for h in ("Objednávka 1", "Objednávka č. 123", "Hárok 1")] == [False] * 3
