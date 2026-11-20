"""Add the SmartAttach link attachments (#506) to an ALREADY-ingested message.

A message ingested before #506 (or whose link download failed at ingest) carries only the
IceWarp notice text. This re-parses its stored raw.eml with the current `process_raw`, appends
the link attachments it now finds (stored files + `attachments` rows, idx after the existing
ones) and refreshes `messages.combined_text` so the engines read the downloaded file. It never
touches the existing attachment rows, never the order/claim state — a reprocess is a separate,
deliberate step (`POST /api/message/<id>/reprocess`).

    python -m app.reextract <messages.id> [<messages.id> ...]
"""
from __future__ import annotations

import argparse
import hashlib
import logging
from pathlib import Path

from . import config, db, process, store
from .process import _combined_text

log = logging.getLogger("reextract")


def add_link_attachments(conn, cfg, msg_id: int, fetch=None) -> dict:
    """Append the link attachments of message `msg_id`. Idempotent: a link whose file is
    already among the message's attachments (same filename) is not added twice."""
    row = conn.execute("SELECT message_id, raw_eml_path FROM messages WHERE id = %s",
                       (msg_id,)).fetchone()
    if not row:
        raise ValueError(f"message {msg_id} not found")
    identity, raw_path = row
    raw = Path(raw_path).read_bytes()
    rec = process.process_raw(raw, **({"fetch": fetch} if fetch else {}))
    existing = conn.execute(
        "SELECT idx, filename, flag, needs_vision, extracted_text FROM attachments "
        "WHERE message_id = %s ORDER BY idx", (identity,)).fetchall()
    have = {(r[1] or "") for r in existing}
    link_atts = [a for a in rec["attachments"]
                 if a.get("method") == "link" or a.get("_link")]
    new = [a for a in link_atts if a["filename"] not in have]
    if not new:
        log.info("reextract %s: nothing new (%d link attachment(s) already stored)",
                 msg_id, len(link_atts))
        return {"id": msg_id, "added": 0}
    start = (max(r[0] for r in existing) + 1) if existing else 0
    mid = store.safe_id(identity)
    d = Path(cfg.data_dir) / mid
    d.mkdir(parents=True, exist_ok=True)
    # an earlier failed download's note is superseded by the file that now arrived — kept as
    # a row (history) but flagged `skipped_*` so the rebuilt text no longer says it failed
    got_names = {a["filename"] for a in new if a.get("_link")}
    superseded = {f"{n}.odkaz.txt" for n in got_names}
    with conn.transaction():
        for off, a in enumerate(new):
            i = start + off
            data = a.get("_data") or b""
            path = d / f"att{i}__{store._safe_name(a.get('filename', ''), i)}"
            path.write_bytes(data)
            conn.execute(
                """INSERT INTO attachments (message_id, idx, filename, mime, size, sha256,
                       method, ocr_conf, pages, chars, needs_vision, flag, file_path, file_url,
                       extracted_text)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                tuple(db._no_nul(v) for v in (
                    identity, i, a.get("filename"), a.get("mime"), a.get("size"),
                    hashlib.sha256(data).hexdigest(), a.get("method"), a.get("ocr_conf"),
                    a.get("pages"), a.get("chars"), a.get("needs_vision"), a.get("flag"),
                    str(path), f"{cfg.public_base_url}/files/{mid}/{i}", a.get("text"))))
        if superseded:
            conn.execute("UPDATE attachments SET flag = 'skipped_link_superseded' "
                         "WHERE message_id = %s AND flag = 'link_failed' AND filename = ANY(%s)",
                         (identity, list(superseded)))
        rows = conn.execute(
            "SELECT filename, flag, needs_vision, extracted_text FROM attachments "
            "WHERE message_id = %s ORDER BY idx", (identity,)).fetchall()
        allatts = [{"filename": r[0], "flag": r[1] or "ok", "needs_vision": bool(r[2]),
                    "text": r[3] or ""} for r in rows]
        combined = _combined_text(rec["headers"], rec["body_text"], allatts)
        conn.execute(
            "UPDATE messages SET combined_text = %s, has_attachments = true, "
            "needs_vision = needs_vision OR %s WHERE id = %s",
            (db._no_nul(combined), any(a.get("needs_vision") for a in new), msg_id))
        db.log_event(conn, identity, "extractor", "reextracted", "ok",
                     outcome=f"doplnené prílohy z odkazu ({len(new)})",
                     detail={"files": [a["filename"] for a in new],
                             "flags": [a.get("flag") for a in new]},
                     rollup=False)
    log.info("reextract %s: added %d link attachment(s): %s", msg_id, len(new),
             [(a["filename"], a.get("flag"), a.get("chars")) for a in new])
    return {"id": msg_id, "added": len(new),
            "files": [(a["filename"], a.get("flag"), a.get("chars")) for a in new]}


def main() -> None:   # pragma: no cover - CLI wiring
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    ap = argparse.ArgumentParser()
    ap.add_argument("ids", nargs="+", type=int)
    args = ap.parse_args()
    cfg = config.Config.load()
    conn = db.connect(cfg.pg_dsn)
    db.init_schema(conn)
    for i in args.ids:
        print(add_link_attachments(conn, cfg, i))


if __name__ == "__main__":   # pragma: no cover
    main()
