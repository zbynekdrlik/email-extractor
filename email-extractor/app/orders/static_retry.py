"""Static-orders engine: safe automatic ORION upload retry (#372).

Ports the DL engine's #239 shape (`dl_retry` / `_check_landed` tri-state, one bounded
retry, one shared success tail) to `static_worker._ship`. See
`.claude/rules/n8n-workflow-edits.md`'s "Never auto-retry an upload whose failure could
have left bytes on the target (#239)" section for the incident history this design exists
to avoid, and #372 for the static-orders live incident (msg 8447, a bare `EOFError()` on
`upload(KARMEN_1811_26_002.txt)`) it fixes.

Two differences from the DL side, both deliberate and driven by the static engine's own
shape (NOT re-implementations — the reused primitives are imported below):

1. **Transient classification is TYPE + phrase, not phrase-only.** `dl_retry.TRANSIENT_RE`
   is an n8n-world FRASE list (rate limit / socket hang up / econnreset …). The #372 live
   incident was a BARE `EOFError()` whose `str()` is `""` — `TRANSIENT_RE` structurally
   cannot match it (verified). So classifying purely on `str(e)` would miss the very
   incident this fix exists for. `is_upload_transient` therefore also treats the connection-
   level exception TYPES a paramiko SFTP upload actually raises as transient — `EOFError`,
   `TimeoutError` (== `socket.timeout` on py3.10+), `ConnectionError` (base of
   `ConnectionResetError`/`BrokenPipeError`/`ConnectionAbortedError`) — while REUSING
   `TRANSIENT_RE` for any failure that does carry a transient phrase. `PermissionError` and
   a bare `OSError` are NOT in the tuple, so a genuine ORION-side refusal never retries
   (#372 test (d)). Over-classifying "transient" is the SAFE direction: the presence check
   below — not the classification — is what actually prevents a double upload, so a
   wrongly-transient permanent failure just does one wasted presence check + one retry and
   lands in the same alert+release end state as today. `paramiko.SSHException` is an
   acknowledged residual (not in the tuple, not imported here): it falls to alert+release,
   the conservative direction, exactly as today.

2. **The document's stable identity on ORION is the WHOLE static filename.** Unlike the
   AI-orders `ORDER_…` name (`edi.filename`, appends `_<HHMMSSmmm>`) or the DESADV name
   (`desadv_edi.filename`, same), `static_edi._filename` is `<PARTNER>_<orderNumber>_
   <prevPart>.txt` — fully deterministic, NO per-attempt timestamp — so the exact filename
   IS the identity. The presence check matches that whole name in `in`/`archCodex`/
   `unconfirmed` (static uploads to `in`, never `in_DL`), tolerating ORION's own Z-/Z-Z-
   archCodex rename via the SHARED `desadv_edi.matches_wire_name` (EXACT match — the whole
   static name is the identity, so `startswith` would be too loose; the Z-/Z-Z- tolerance
   still lives in exactly one place, never a second copy of the prefix logic). And because
   the static name does NOT encode content or delivery date, a same-order-number correction
   can produce the SAME filename with different content — `_name_collision` refuses to trust
   a presence match when ANY DIFFERENT-content `edi_sent` row occupies that name (confirmed
   OR still-unconfirmed — the dangerous case is a run that crashed between `sftp.rename` and
   `confirm_sent`, leaving its bytes on ORION under an UNCONFIRMED row), so an ambiguous
   match falls to alert+release instead of silently confirming (and dropping) our order.
   That is the static analogue of `desadv.has_confirmed_collision`, whose own collision
   source (a truncated stable prefix) does not apply here — the static identity is never
   truncated. RESIDUAL (same class `desadv.has_confirmed_collision` acknowledges): an
   occupant whose `edi_sent` row was fully DELETED (an earlier `_alert_and_release` while
   its bytes stayed on ORION) is undetectable via `edi_sent` alone — bounded by the manual
   re-send absence-proof procedure, not by this guard.
"""
from __future__ import annotations

import logging

from . import desadv_edi, edi
from .dl_retry import _is_transient  # the SAME transient-FRASE classifier (uses TRANSIENT_RE)

log = logging.getLogger("orders.static_worker")

# Connection-level SFTP failures a paramiko upload raises that are safely retryable-after-
# a-presence-check but that `TRANSIENT_RE` cannot see in `str(e)` (the msg 8447 `EOFError()`
# has an empty str). Deliberately NOT `OSError` (too broad — its subclass `PermissionError`
# is a genuine, non-retryable refusal: #372 test (d)).
_TRANSIENT_UPLOAD_EXC: tuple[type[BaseException], ...] = (
    EOFError, TimeoutError, ConnectionError)

# Static orders upload to `in` (never `in_DL`); once imported they move to the shared
# `archCodex`, or to `unconfirmed` on an import failure — the three folders a static EDI
# can legitimately be sitting in.
_PRESENCE_FOLDERS = ("in", "archCodex", "unconfirmed")


def is_upload_transient(e: BaseException) -> bool:
    """Is this ORION upload failure a transient one worth a presence-check + one bounded
    retry? Combines the connection-level exception TYPES above with `dl_retry`'s own
    `TRANSIENT_RE` phrase match — see this module's docstring for why the TYPE check is
    load-bearing (the msg 8447 bare `EOFError()`)."""
    return isinstance(e, _TRANSIENT_UPLOAD_EXC) or _is_transient(str(e))


def _present_on_orion(dirs: dict, filename: str) -> bool:
    # EXACT match (not `startswith`): the whole static filename is the identity, so a
    # `…_007.txt.bak`-style artifact from manual ORION ops must NOT count as a presence
    # match — a static false-positive silently CONFIRMS and drops the order (#372 review
    # 🔵-4). The Z-/Z-Z- archCodex-rename tolerance is still the SHARED desadv_edi helper.
    for folder in _PRESENCE_FOLDERS:
        for name in (dirs or {}).get(folder) or ():
            if desadv_edi.matches_wire_name(name, filename):
                return True
    return False


def _name_collision(conn, filename: str, content: str) -> bool:
    """Is a DIFFERENT-content `edi_sent` row occupying this exact filename? The static
    filename encodes partner/order/store, NOT content or delivery date, so a same-order-
    number CORRECTION can share it with a genuinely different document. When such a row
    exists, a bare presence match on the name cannot prove OUR (different-content) bytes
    landed — confirming would silently DROP this order (the #239 mirror: silent LOSS, not
    duplication). So the check must distrust the name whenever ANY OTHER-content row holds
    it, not only a CONFIRMED one (#372 review 🟡-1): the dangerous case is precisely a run
    for order A that crashed BETWEEN `sftp.rename` and `confirm_sent` — A's bytes are on
    ORION under the name, its `edi_sent` row is still UNCONFIRMED (`uploaded_at NULL`), and
    a correction A′ failing transiently would otherwise be confirmed off A's bytes. Our OWN
    claim row is excluded by the `content_sha256 <> %s` inequality, so this never
    false-triggers on ourselves.

    RESIDUAL (documented, same class `desadv.has_confirmed_collision` acknowledges): an
    occupant whose `edi_sent` row was fully DELETED by an earlier `_alert_and_release`
    while its bytes stayed on ORION is structurally undetectable via `edi_sent` alone —
    that window is bounded by the manual re-send absence-proof procedure, not by this
    guard."""
    chash = edi.content_hash(content)
    row = conn.execute(
        "SELECT 1 FROM edi_sent WHERE filename = %s AND content_sha256 <> %s LIMIT 1",
        (str(filename or ""), chash)).fetchone()
    return row is not None


def check_landed(conn, cfg, list_dirs, filename: str, content: str) -> bool | None:
    """#372 (mirrors `dl_retry._check_landed`): after a TRANSIENT static-upload failure, is
    the document already on ORION under this exact (stable) filename — the "bytes landed,
    only the reply was lost" case `upload.put()`'s temp-write+rename makes provable?

    Returns:
    - `True`  — present AND trustworthy (no different-content confirmed row occupies the
      name): confirm the SAME claim, NEVER re-upload.
    - `False` — genuinely absent everywhere it could sit: exactly ONE bounded retry is safe.
    - `None`  — the check itself could not be attempted (the SFTP connection that just
      failed the upload is very likely down for a follow-up listing too), OR a presence
      match was found but is NOT trustworthy (`_name_collision`). Either way NO retry
      and NO confirm — fall back to the pre-#372 alert+release path. A blind retry with no
      absence proof, or a blindly-trusted false-positive match, is exactly the v0.9.70
      duplicate-delivery incident (or its silent-loss mirror) this design prevents.
    """
    try:
        dirs = list_dirs(cfg)
        landed = _present_on_orion(dirs, filename)
        collision = landed and _name_collision(conn, filename, content)
    except Exception:
        log.warning("static upload retry: could not check ORION presence for %s — no safe "
                    "retry possible", filename, exc_info=True)
        return None
    if not landed:
        return False
    if collision:
        log.warning(
            "static upload retry: a presence match for %s collided with a DIFFERENT "
            "already-confirmed order sharing the same filename — refusing to trust it, "
            "falling back to the safe alert path instead of confirming the wrong order",
            filename)
        return None
    return True
