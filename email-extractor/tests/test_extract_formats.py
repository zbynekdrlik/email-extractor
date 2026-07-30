"""Format edge cases that silently dropped or corrupted order data (#23).

Four independent defects, one synthetic fixture each — no real customer data:

1. .ods with `number-columns-repeated`: repeated/empty columns were not expanded,
   so columns shifted LEFT and the quantity column was read from the wrong place.
2. an ms-excel MIME on a file that is really OOXML (or CSV): the declared mime
   force-routed it to xlrd, which raised on non-BIFF → flag='error', no text, so
   the order never reached the classifier or Vision.
3. flat ODF (.fods/.fodt): accepted by the dispatcher but odfpy's load() only reads
   zip-packaged ODF → BadZipFile → flag='error', no text.
4. legacy .xls dates: xlrd returns date cells as float serials, so a delivery date
   came out as `45102` instead of a date.
"""
import datetime
import io

import openpyxl
import xlwt
from odf import table, text
from odf.opendocument import OpenDocumentSpreadsheet

from app import extract


def _cell(value=""):
    c = table.TableCell(valuetype="string")
    c.addElement(text.P(text=str(value)))
    return c


def _ods_with_repeated_columns() -> bytes:
    """A Calc sheet whose row is: Rožok | (3 empty) | 120 — the 3 empty columns are
    stored as ONE cell with number-columns-repeated="3", exactly as Calc writes it."""
    doc = OpenDocumentSpreadsheet()
    tbl = table.Table(name="Objednavka")

    head = table.TableRow()
    head.addElement(_cell("Produkt"))
    head.addElement(table.TableCell(numbercolumnsrepeated="3"))
    head.addElement(_cell("Mnozstvo"))
    tbl.addElement(head)

    row = table.TableRow()
    row.addElement(_cell("Rožok kváskový 70g"))
    row.addElement(table.TableCell(numbercolumnsrepeated="3"))
    row.addElement(_cell("120"))
    tbl.addElement(row)

    doc.spreadsheet.addElement(tbl)
    buf = io.BytesIO()
    doc.write(buf)
    return buf.getvalue()


def _xlsx_bytes() -> bytes:
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Objednavka"
    ws.append(["Produkt", "Mnozstvo"])
    ws.append(["Bageta kvásková 500g", 4])
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def _flat_ods_bytes() -> bytes:
    """Flat single-XML ODF (what LibreOffice writes as .fods) — no zip container."""
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<office:document '
        'xmlns:office="urn:oasis:names:tc:opendocument:xmlns:office:1.0" '
        'xmlns:table="urn:oasis:names:tc:opendocument:xmlns:table:1.0" '
        'xmlns:text="urn:oasis:names:tc:opendocument:xmlns:text:1.0" '
        'office:mimetype="application/vnd.oasis.opendocument.spreadsheet">'
        '<office:body><office:spreadsheet>'
        '<table:table table:name="Objednavka">'
        '<table:table-row>'
        '<table:table-cell><text:p>Produkt</text:p></table:table-cell>'
        '<table:table-cell><text:p>Mnozstvo</text:p></table:table-cell>'
        '</table:table-row>'
        '<table:table-row>'
        '<table:table-cell><text:p>Vianočka 400g</text:p></table:table-cell>'
        '<table:table-cell><text:p>7</text:p></table:table-cell>'
        '</table:table-row>'
        '</table:table></office:spreadsheet></office:body></office:document>'
    ).encode("utf-8")


def _xls_with_a_date() -> bytes:
    wb = xlwt.Workbook()
    ws = wb.add_sheet("Dodanie")
    date_style = xlwt.easyxf(num_format_str="DD.MM.YYYY")
    ws.write(0, 0, "Datum dodania")
    ws.write(0, 1, "Mnozstvo")
    ws.write(1, 0, datetime.datetime(2026, 8, 3), date_style)
    ws.write(1, 1, 12)
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


# ---- 1) .ods repeated columns must not shift the grid ----

def test_ods_repeated_columns_are_expanded():
    r = extract.extract_attachment(
        "objednavka.ods", "application/vnd.oasis.opendocument.spreadsheet",
        _ods_with_repeated_columns())
    assert r["flag"] != "error", r.get("error")
    line = [ln for ln in r["text"].splitlines() if "Rožok" in ln][0]
    cells = [c.strip() for c in line.split("|")]
    assert cells[0] == "Rožok kváskový 70g"
    assert cells[-1] == "120", f"quantity column shifted: {cells}"
    assert len(cells) == 5, f"3 empty columns must stay empty, got {cells}"


# ---- 2) the real format decides, not the declared mime ----

def test_xlsx_declared_as_ms_excel_without_an_extension_is_still_read():
    r = extract.extract_attachment("objednavka", "application/vnd.ms-excel", _xlsx_bytes())
    assert r["flag"] != "error", r.get("error")
    assert "Bageta kvásková 500g" in r["text"]
    assert "4" in r["text"]


def test_csv_declared_as_ms_excel_is_still_read():
    csv = "Produkt;Mnozstvo\nŠiška džemová 50g;24\n".encode()
    r = extract.extract_attachment("objednavka", "application/vnd.ms-excel", csv)
    assert r["flag"] != "error", r.get("error")
    assert "Šiška džemová 50g" in r["text"]


def test_a_real_xls_still_goes_through_xlrd():
    r = extract.extract_attachment("stary.xls", "application/vnd.ms-excel", _xls_with_a_date())
    assert r["method"] == "xls", r
    assert "Mnozstvo" in r["text"]


# ---- 3) flat ODF must not be swallowed as an error ----

def test_flat_odf_is_extracted():
    r = extract.extract_attachment(
        "objednavka.fods", "application/vnd.oasis.opendocument.spreadsheet",
        _flat_ods_bytes())
    assert r["flag"] != "error", r.get("error")
    assert "Vianočka 400g" in r["text"]
    assert "7" in r["text"]


# ---- 4) legacy .xls dates must be dates, not serial numbers ----

def test_xls_date_cells_are_formatted_as_dates():
    r = extract.extract_attachment("dodanie.xls", "application/vnd.ms-excel",
                                   _xls_with_a_date())
    assert r["flag"] != "error", r.get("error")
    assert "2026-08-03" in r["text"] or "03.08.2026" in r["text"], r["text"]
    assert "46237" not in r["text"], f"date leaked as a serial number: {r['text']}"
