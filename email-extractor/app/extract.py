"""
Attachment + body text extraction core.

Strategy (validated by the 100-email quality spike, 2026-06-25):
  - native text first: text-layer PDF (pdfplumber), docx, xlsx, txt/csv
  - PDF yields no text OR garbage (mojibake / "(cid:" / low alpha) -> OCR
  - images -> OCR (Tesseract ces+slk+eng, 300 DPI)
  - low OCR confidence on an image/scan -> needs_vision (route to AI Vision)

Two fixes over the spike:
  1. skip decorative signature/banner images (wide aspect ratio / tiny) instead
     of OCR-ing them into noise
  2. when needs_vision is set, DROP the noisy OCR text (keep a short placeholder)
     so ~KBs of garbage never pollute the combined text the classifier sees
"""
from __future__ import annotations

import io

OCR_LANG = "ces+slk+eng"
OCR_CONFIG = "--oem 1 --psm 6 -c preserve_interword_spaces=1"
OCR_DPI = 300
OCR_MAX_PAGES = 15
TEXT_PDF_MIN_CHARS_PER_PAGE = 50
GARBAGE_ALPHA = 0.55
NEEDS_VISION_CONF = 72
MIN_IMG_BYTES = 8000
MIN_IMG_PIXELS = 40000          # < ~200x200 = logo/icon
BANNER_ASPECT = 3.0            # wider/taller than this = decorative banner
JUNK_EXT = {"vcf", "ics", "p7s", "asc", "pgp", "gpg", "smime", "sig", "key"}
_ALPHA_EXTRA = set(".,;:-/€%()@")


def alpha_ratio(s: str) -> float:
    """Fraction of chars that are letters/digits/space/common punct (noise detector)."""
    if not s:
        return 0.0
    good = sum(1 for ch in s if ch.isalnum() or ch.isspace() or ch in _ALPHA_EXTRA)
    return round(good / len(s), 3)


def looks_garbage(text: str, alpha: float | None = None) -> bool:
    """True when a native text extraction is gibberish and should be OCR'd."""
    if not text:
        return True
    if "(cid:" in text:
        return True
    if text.count("�") / max(len(text), 1) > 0.02:
        return True
    if alpha is None:
        alpha = alpha_ratio(text)
    return alpha < GARBAGE_ALPHA


def _new_result(filename: str, mime: str, size: int) -> dict:
    return {"filename": filename or "(no name)", "mime": mime, "size": size,
            "method": None, "chars": 0, "ocr_conf": None, "pages": None,
            "alpha_ratio": None, "needs_vision": False, "native_garbage": False,
            "flag": "ok", "error": None, "text": ""}


def _set_text(res: dict, text: str):
    res["chars"] = len(text)
    res["alpha_ratio"] = alpha_ratio(text)
    res["text"] = text


def _ocr_images(images) -> tuple[str, float]:
    import pytesseract
    from pytesseract import Output
    texts, confs = [], []
    for img in images:
        if img.mode != "L":
            img = img.convert("L")
        data = pytesseract.image_to_data(img, lang=OCR_LANG, config=OCR_CONFIG,
                                          output_type=Output.DICT)
        words = []
        for w, conf in zip(data["text"], data["conf"], strict=False):
            try:
                ci = float(conf)
            except (TypeError, ValueError):
                ci = -1
            if w.strip() and ci >= 0:
                words.append(w)
                confs.append(ci)
        texts.append(" ".join(words))
    mean_conf = round(sum(confs) / len(confs), 1) if confs else 0.0
    return "\n".join(texts).strip(), mean_conf


def _is_decorative_image(width: int, height: int, size: int) -> str | None:
    """Return a skip-flag if the image is junk (logo/icon/banner), else None."""
    if size < MIN_IMG_BYTES:
        return "skipped_tiny_image"
    if (width * height) < MIN_IMG_PIXELS:
        return "skipped_small_image"
    if min(width, height) and max(width, height) / min(width, height) > BANNER_ASPECT:
        return "skipped_banner_image"   # fix #1: wide signature/marketing banners
    return None


def _apply_vision_gate(res: dict):
    """fix #2: when low-confidence OCR is flagged for vision, drop the noisy text."""
    if res["needs_vision"]:
        res["flag"] = "needs_vision"
        res["text"] = f"[needs AI Vision: {res['filename']}]"


def file_ext(filename: str) -> str:
    fn = (filename or "").strip().lower()
    return fn.rsplit(".", 1)[-1] if "." in fn else ""


def extract_attachment(filename: str, mime: str, data: bytes) -> dict:
    """Extract text from one attachment. Returns a result dict incl. `text`."""
    ext = file_ext(filename)
    mime = (mime or "").lower()
    res = _new_result((filename or "").strip(), mime, len(data))
    try:
        if ext in JUNK_EXT:
            res.update(method="skipped", flag="skipped_junk")
            return res

        if mime == "application/pdf" or ext == "pdf":
            return _extract_pdf(res, data)

        if mime.startswith("image/") or ext in {"png", "jpg", "jpeg", "tif",
                                                 "tiff", "bmp", "webp", "gif"}:
            return _extract_image(res, data)

        if ext == "docx" or "wordprocessingml" in mime:
            return _extract_docx(res, data)

        if ext in {"xlsx", "xlsm"} or "spreadsheetml" in mime:
            return _extract_xlsx(res, data)

        if ext in {"txt", "csv", "log", "md"} or mime.startswith("text/"):
            res["method"] = "text"
            _set_text(res, data.decode("utf-8", errors="replace"))
            return res

        res.update(method="unsupported", flag="unsupported")
        return res
    except Exception as e:  # never let one attachment kill the email
        res.update(error=f"{type(e).__name__}: {e}", flag="error")
        return res


def _extract_pdf(res: dict, data: bytes) -> dict:
    import pdfplumber
    text, npages = "", 0
    try:
        with pdfplumber.open(io.BytesIO(data)) as pdf:
            npages = len(pdf.pages)
            text = "\n".join((p.extract_text() or "") for p in pdf.pages).strip()
    except Exception as e:
        res["error"] = f"pdfplumber: {e}"
    res["pages"] = npages
    ypp = (len(text) / npages) if npages else 0
    ar = alpha_ratio(text)
    garbage = bool(text) and looks_garbage(text, ar)
    if text and ypp >= TEXT_PDF_MIN_CHARS_PER_PAGE and not garbage:
        res["method"] = "pdf-text"
        _set_text(res, text)
        return res
    # no text OR garbage -> OCR
    res["native_garbage"] = garbage
    from pdf2image import convert_from_bytes
    imgs = convert_from_bytes(data, dpi=OCR_DPI, first_page=1, last_page=OCR_MAX_PAGES)
    otext, conf = _ocr_images(imgs)
    res["method"] = "pdf-ocr"
    res["ocr_conf"] = conf
    _set_text(res, otext)
    res["needs_vision"] = (conf < NEEDS_VISION_CONF) or (len(otext) < 20)
    if not res["needs_vision"]:
        res["flag"] = ("garbage_native_ocr" if garbage
                       else ("ocr_truncated" if npages > OCR_MAX_PAGES else "ok"))
    _apply_vision_gate(res)
    return res


def _extract_image(res: dict, data: bytes) -> dict:
    from PIL import Image
    img = Image.open(io.BytesIO(data))
    skip = _is_decorative_image(img.width, img.height, len(data))
    if skip:
        res.update(method="skipped", flag=skip, pages=1)
        return res
    otext, conf = _ocr_images([img])
    res["method"] = "image-ocr"
    res["ocr_conf"] = conf
    res["pages"] = 1
    _set_text(res, otext)
    res["needs_vision"] = (conf < NEEDS_VISION_CONF) or (len(otext) < 10)
    _apply_vision_gate(res)
    return res


def _extract_docx(res: dict, data: bytes) -> dict:
    import docx
    d = docx.Document(io.BytesIO(data))
    parts = [p.text for p in d.paragraphs]
    for t in d.tables:
        for row in t.rows:
            parts.append("\t".join(c.text for c in row.cells))
    res["method"] = "docx"
    _set_text(res, "\n".join(parts))
    return res


def _extract_xlsx(res: dict, data: bytes) -> dict:
    import openpyxl
    wb = openpyxl.load_workbook(io.BytesIO(data), read_only=True, data_only=True)
    chunks = []
    for ws in wb.worksheets:
        chunks.append(f"[Sheet: {ws.title}]")
        for row in ws.iter_rows(values_only=True):
            cells = [str(c) for c in row if c is not None]
            if cells:
                chunks.append("\t".join(cells))
    res["method"] = "xlsx"
    _set_text(res, "\n".join(chunks))
    return res
