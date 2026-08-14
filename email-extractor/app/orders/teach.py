"""The teach-once loop: the warehouse advises a wording once, forever (#88).

Measured on the 30-email corpus (2026-07-31): only **15 distinct (customer, wording) pairs**
are behind every line a human would ever see — private nicknames ("Šiška", "Pletenka",
"jankove buchty"), sloppy abbreviations, and four variants that do not exist as ordered
("Dánske pečivo s jahodami" against a čučoriedka card). Teach the 15 and the tail closes.
The four keep asking, forever, and that is correct: shipping blueberry for strawberry is
worse than asking.

The channel is the user's decision (2026-07-31): **one click on the extractor dashboard**,
linked from the Odoo message the warehouse already reads. So an answer arrives as a card id
out of the offered candidates — never as free text to be parsed, never as a typo.

The answer lands in `item_memory(source='human')`, per CUSTOMER, and outranks every model
rung (`match.decide_without_model` → `human_taught`). It also overrides the weight guard: the
guard exists to stop the MODEL guessing, not to overrule the warehouse.
"""
from __future__ import annotations

import logging
import re
from collections.abc import Callable
from dataclasses import dataclass

from psycopg.types.json import Json

from . import dl_memory, dl_supplier_memory, memory, snapshot

log = logging.getLogger("orders.teach")


class NotACandidate(Exception):
    """The answer is not one of the cards that were offered."""


class AlreadyAnswered(Exception):
    """This question has been settled; a second answer would silently rewrite history."""


def ask(conn, message_id: str, customer_ean: str, customer_name: str, wording: str,
        quantity, unit: str, candidates: list[dict], delivery_date: str = "",
        reason: str = "", on_new=None) -> int | None:
    """Raise ONE question for this (customer, wording). Returns its id.

    Returns the EXISTING id when it is already open, and None when the wording has already
    been taught — the engine will resolve it from memory, so asking would be noise.

    `on_new` (#102) fires ONLY when a genuinely NEW question was just inserted — never on the
    "already open" or "already taught" paths — so a duplicate order for the same still-open
    wording does not spam a second Odoo message. Called with the full question dict; any
    failure is logged and swallowed, exactly like the main order report's own post — a
    notification failure must never break order processing.
    """
    key = memory.item_key(wording)
    if not (customer_ean and key):
        return None
    # Skip only when a HUMAN has already settled this wording. A thin machine-learned history
    # is NOT a reason to stay silent: it is below the ladder's 3-day bar, so the line can still
    # end up unmatched — and then nobody could ever teach it.
    recalled = memory.resolve(conn, customer_ean, wording)
    if recalled is not None and recalled.human:
        return None
    row = conn.execute(
        """INSERT INTO order_questions
               (message_id, customer_ean, customer_name, wording, item_key, quantity, unit,
                candidates, delivery_date, reason)
           VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
           ON CONFLICT (customer_ean, item_key) WHERE status = 'open' DO NOTHING
           RETURNING id""",
        (message_id, str(customer_ean), customer_name or "", str(wording), key, quantity,
         unit or "ks", Json(candidates or []), delivery_date or "", reason or ""),
    ).fetchone()
    if row:
        qid = int(row[0])
        log.info("asking the warehouse about %r for %s", wording, customer_ean)
        if on_new:
            try:
                on_new(get(conn, qid))
            except Exception:
                log.exception("notifying about new question %s (%r) failed", qid, wording)
        return qid
    existing = conn.execute(
        "SELECT id FROM order_questions WHERE customer_ean = %s AND item_key = %s"
        " AND status = 'open'", (str(customer_ean), key)).fetchone()
    return int(existing[0]) if existing else None


_COLS = ("id, message_id, customer_ean, customer_name, wording, quantity, unit, "
        "candidates, delivery_date, reason, status, answer_gtin, answer_card, "
        "answered_by, answered_at, created_at, kind, context, payload, answer")


def get(conn, qid: int) -> dict | None:
    row = conn.execute(
        f"SELECT {_COLS} FROM order_questions WHERE id = %s", (qid,)).fetchone()
    return _row(row) if row else None


def open_questions(conn, limit: int = 100, kinds: tuple[str, ...] | None = None) -> list[dict]:
    """`kinds` (#231, the DL-only nástenka) restricts the result to those `kind` values
    only — `None` (the default, every existing caller) keeps returning every kind, exactly
    as before. The filter is applied HERE, in SQL, not by the caller filtering the
    returned list — a role-scoped board must never even see a row of a kind it isn't
    allowed to, not just skip rendering it."""
    if kinds is not None:
        rows = conn.execute(
            f"""SELECT {_COLS} FROM order_questions WHERE status = 'open'
                AND kind = ANY(%s) ORDER BY created_at LIMIT %s""",
            (list(kinds), limit)).fetchall()
    else:
        rows = conn.execute(
            f"""SELECT {_COLS} FROM order_questions WHERE status = 'open'
                ORDER BY created_at LIMIT %s""", (limit,)).fetchall()
    return [_row(r) for r in rows]


def recently_taught(conn, limit: int = 20, kinds: tuple[str, ...] | None = None) -> list[dict]:
    """The mappings a human settled, newest first — this is what makes `undo` reachable.

    An answered question leaves the open list, so without this the warehouse had nowhere to
    correct a mis-click, and an undo nobody can reach is not an undo (found while verifying
    0.9.5 on the live box).

    `kinds` (#231): same optional kind-restriction as `open_questions` above.
    """
    if kinds is not None:
        rows = conn.execute(
            f"""SELECT {_COLS} FROM order_questions WHERE status = 'answered'
                AND kind = ANY(%s) ORDER BY answered_at DESC LIMIT %s""",
            (list(kinds), limit)).fetchall()
    else:
        rows = conn.execute(
            f"""SELECT {_COLS} FROM order_questions WHERE status = 'answered'
                ORDER BY answered_at DESC LIMIT %s""", (limit,)).fetchall()
    return [_row(r) for r in rows]


def answer(conn, qid: int, gtin: str, card: str, by: str = "") -> dict:
    """Settle a question and teach it. The card must be one that was offered, OR one that
    exists in the current catalog (#149 — the warehouse's full-catalog search on /otazky).

    Teaches on TWO layers (#102): the existing per-customer mapping (unchanged — it is what
    makes THIS customer's answer instant), and a GLOBAL one for every other customer who ever
    types the same wording. The global write is best-effort (`memory.remember_global`'s own
    `ON CONFLICT DO NOTHING`) — if this wording is already taught globally, the per-customer
    row still lands and, being checked first in the ladder, is exactly what lets a customer's
    OWN mapping override the global one.
    """
    q = get(conn, qid)
    if not q:
        raise NotACandidate(f"question {qid} does not exist")
    if q["status"] != "open":
        raise AlreadyAnswered(
            f"question {qid} was answered on {q['answered_at']} with {q['answer_gtin']}")
    offered = {str(c.get("gtin")) for c in q["candidates"]}
    if str(gtin) not in offered and str(gtin) not in snapshot.catalog_gtin_set(conn):
        raise NotACandidate(
            f"{gtin} was not offered for {q['wording']!r} and is not in the catalog")

    memory.remember(conn, q["customer_ean"], q["wording"], str(gtin), card or "",
                    _today(conn), source="human")
    memory.remember_global(conn, q["wording"], str(gtin), card or "", question_id=qid,
                           taught_by=by)
    conn.execute(
        """UPDATE order_questions
              SET status = 'answered', answer_gtin = %s, answer_card = %s,
                  answered_by = %s, answered_at = now()
            WHERE id = %s""", (str(gtin), card or "", by or "", qid))
    log.info("taught %r -> %s for %s (by %s)", q["wording"], gtin, q["customer_ean"], by)
    return get(conn, qid) or {}


def undo(conn, qid: int) -> dict:
    """Take a mistaken answer back: drop the mapping and ask again.

    Without this a mis-click was permanent AND invisible — a taught wording is never asked
    about again and decides the line with no model call, so nothing would ever contradict it.
    Only what a HUMAN taught is removed; real deliveries are evidence and stay.

    Also retracts the GLOBAL mapping (#102) — but ONLY the row THIS question created
    (`memory.forget_global` checks `question_id`). A different customer's later, redundant
    answer to the same wording never owns the global row (it was already taught), so undoing
    THAT answer must not erase a mapping some OTHER question is responsible for.

    A `kind='customer'` question (#159) is entirely different data to unteach: its real
    pick was remembered via `snapshot.remember_customer_email`, not `item_memory` — review
    finding on PR #161: without this branch, undoing a mis-picked customer only reopened
    the question while the wrong sender-address binding stayed live in
    `customer_overrides` forever, silently mis-routing every future order from that
    address. "neviem, kto to je" (`answer_gtin=""`) never remembered anything, so it is a
    harmless reopen either way.
    """
    q = get(conn, qid)
    if not q:
        raise NotACandidate(f"question {qid} does not exist")
    if q.get("kind") == "customer":
        if q.get("answer_gtin"):
            from . import snapshot
            snapshot.forget_customer_email(
                conn, q["answer_gtin"], (q.get("context") or {}).get("sender_email", ""))
            snapshot.rebuild_from_overrides(conn)
        conn.execute(
            """UPDATE order_questions
                  SET status = 'open', answer_gtin = NULL, answer_card = NULL,
                      answered_by = NULL, answered_at = NULL, reminder_sent_at = NULL,
                      escalated_at = NULL
                WHERE id = %s""", (qid,))
        log.warning("customer teaching taken back for question %s (%s)", qid,
                    (q.get("context") or {}).get("sender_email", ""))
        return get(conn, qid) or {}
    conn.execute(
        "DELETE FROM item_memory WHERE customer_ean = %s AND item_key = %s"
        " AND source = 'human'", (q["customer_ean"], memory.item_key(q["wording"])))
    memory.forget_global(conn, q["wording"], qid)
    conn.execute(
        """UPDATE order_questions
              SET status = 'open', answer_gtin = NULL, answer_card = NULL,
                  answered_by = NULL, answered_at = NULL, reminder_sent_at = NULL,
                  escalated_at = NULL
            WHERE id = %s""", (qid,))
    log.warning("teaching taken back for %r (%s)", q["wording"], q["customer_ean"])
    return get(conn, qid) or {}


def _row(r) -> dict:
    return {"id": int(r[0]), "message_id": r[1], "customer_ean": r[2],
            "customer_name": r[3] or "", "wording": r[4], "quantity": r[5],
            "unit": r[6] or "", "candidates": r[7] or [], "delivery_date": r[8] or "",
            "reason": r[9] or "", "status": r[10], "answer_gtin": r[11],
            "answer_card": r[12], "answered_by": r[13], "answered_at": r[14],
            "created_at": r[15], "kind": r[16] or "item", "context": r[17] or {},
            "payload": r[18] or {}, "answer": r[19] or {}}


# --- #159: the customer-half of the SAME teach-once loop — "who is this customer?" ----
#
# Shares the exact same table/index/dashboard/undo machinery `ask`/`answer` above use for
# products — only `kind='customer'` differs. `customer_ean` is always '' for this kind
# (the customer is exactly what is unknown), so the dedupe key is `item_key`, built from
# the SENDER ADDRESS rather than a product wording: a second unresolved order from the
# same address, before the first is answered, reuses the SAME open question.

def ask_customer(conn, message_id: str, sender_email: str, candidates: list[dict],
                 delivery_date: str, context: dict, on_new=None) -> int | None:
    """Raise ONE 'who is this customer?' question. Returns its id — the EXISTING id when
    one is already open for this sender address, and None when there is no message_id at
    all to key ANYTHING on (should not happen in practice).

    The dedupe key is the address itself, case/whitespace-normalized only — review
    finding on PR #161: reusing `memory.item_key` (a FUZZY normalizer built for product
    WORDINGS, which folds '.', '-', '_', '@' all to the same blank separator) let two
    genuinely different real addresses collapse onto the SAME open question. An e-mail
    address needs EXACT identity, never fuzzy matching.

    #164: when there is no address at all to key on (every address field blank), fall
    back to a per-MESSAGE key (`cust:<message_id>`) instead of refusing outright — the
    warehouse can still be asked "who is this?" and the order can still be HELD for an
    answer, instead of silently falling through to a reject with no path forward
    (`pipeline._run`'s old "no sender address" dead end, inventory row 4).
    """
    key = str(sender_email or "").strip().lower()
    if not key:
        key = f"cust:{message_id}" if message_id else ""
    if not key:
        return None
    row = conn.execute(
        """INSERT INTO order_questions
               (message_id, customer_ean, customer_name, wording, item_key, kind,
                candidates, delivery_date, reason, context)
           VALUES (%s, '', '', %s, %s, 'customer', %s, %s, %s, %s)
           ON CONFLICT (customer_ean, item_key) WHERE status = 'open' DO NOTHING
           RETURNING id""",
        (message_id, sender_email, key, Json(candidates or []), delivery_date or "",
         "Zákazník nebol nájdený v tabuľke zákazníkov", Json(context or {}))).fetchone()
    if row:
        qid = int(row[0])
        log.info("asking the warehouse who %r is", sender_email)
        if on_new:
            try:
                on_new(get(conn, qid))
            except Exception:
                log.exception("notifying about new customer question %s (%r) failed",
                              qid, sender_email)
        return qid
    existing = conn.execute(
        "SELECT id FROM order_questions WHERE customer_ean = '' AND item_key = %s"
        " AND status = 'open'", (key,)).fetchone()
    return int(existing[0]) if existing else None


def add_candidate(conn, qid: int, cand: dict) -> None:
    """Extend an OPEN question's stored candidate list with one more entry (#234) — used
    when the warehouse answers a customer question with a pick that was never in the
    frozen candidate set (a brand-new customer just created, or an existing one found via
    the live search box). Keeps `answer_customer`'s "a pick must have been offered"
    invariant intact while leaving a durable audit trail: the answered EAN is present in
    the question's own recorded candidates.

    `candidates` is `JSONB NOT NULL DEFAULT '[]'::jsonb` (`db.py`), so a jsonb `||` array
    concat is correct. A no-op (silently) once the question is no longer open."""
    conn.execute(
        "UPDATE order_questions SET candidates = candidates || %s::jsonb WHERE id = %s"
        " AND status = 'open'", (Json([cand]), qid))


def answer_customer(conn, qid: int, ean_edi: str, name: str, by: str = "") -> dict:
    """Settle a 'who is this customer?' question. `ean_edi=""` means the warehouse said
    "neviem, kto to je / nie je to ani jeden z nich" — the question is still marked
    answered (so it stops asking and the taught list shows it was handled), but the
    caller (`hold.release_unknown_customer`) is what turns the held order into a visible
    'review' outcome instead of shipping it; this function never ships anything itself.

    A real pick must be one of the offered candidates — unlike `answer()`'s product half
    there is no "or anything in the catalog" escape hatch, because the whole point of this
    question is that the customer is not yet reliably known at all.
    """
    q = get(conn, qid)
    if not q:
        raise NotACandidate(f"question {qid} does not exist")
    if q.get("kind") != "customer":
        raise NotACandidate(f"question {qid} is not a customer question")
    if q["status"] != "open":
        raise AlreadyAnswered(
            f"question {qid} was answered on {q['answered_at']} with {q['answer_gtin']}")
    if ean_edi:
        offered = {str(c.get("ean_edi")) for c in q["candidates"]}
        if str(ean_edi) not in offered:
            raise NotACandidate(f"{ean_edi} was not offered for question {qid}")
    # #234 review finding: the "already answered" check above is a Python-level read
    # from an EARLIER select, not a WHERE-clause guard on this write — two genuinely
    # racing answers to the SAME question could both pass it and the second UPDATE
    # would silently overwrite the first's answer (no `status='open'` predicate to fail
    # against). Guard the write itself and re-check on 0 rows affected.
    row = conn.execute(
        """UPDATE order_questions
              SET status = 'answered', answer_gtin = %s, answer_card = %s,
                  answered_by = %s, answered_at = now()
            WHERE id = %s AND status = 'open'
            RETURNING id""", (str(ean_edi or ""), name or "", by or "", qid)).fetchone()
    if not row:
        lost = get(conn, qid) or {}
        raise AlreadyAnswered(
            f"question {qid} was answered on {lost.get('answered_at')} with "
            f"{lost.get('answer_gtin')}")
    log.info("customer question %s answered: %r (%s) by %s", qid, ean_edi, name, by)
    return get(conn, qid) or {}


def _today(conn):
    return conn.execute("SELECT current_date").fetchone()[0]


# --- #164: the generalized funnel — every OTHER board-resolvable dead end (mail/date/
# line) shares this SAME table/index/dashboard machinery, keyed on a synthetic
# `item_key` per kind instead of a real customer_ean/wording pair. Never a bespoke table
# per new kind — the diff that would repeat is exactly what let the customer question
# stay unteachable for as long as it did (#159's own history).

def _sender_norm(email: str) -> str:
    return str(email or "").strip().lower()


_SUBJECT_NOISE = re.compile(r"[\d./\-:]+")


def subject_key(subject: str) -> str:
    """A subject with dates/order numbers stripped — a 'mail_rules' pattern key.

    Two order confirmations from the same sender differ only in the date/number they
    carry; folding those away is what lets ONE taught rule cover every future mail of the
    same shape instead of matching on nothing.
    """
    s = _SUBJECT_NOISE.sub(" ", str(subject or "").lower())
    return re.sub(r"\s+", " ", s).strip()


def ask_generic(conn, kind: str, message_id: str, key: str, wording: str,
                candidates: list[dict], reason: str, payload: dict,
                delivery_date: str = "", on_new=None) -> int | None:
    """Raise ONE question of a NEW (#164) kind — mail/date/line — reusing the exact
    `order_questions` table, dedup index and dashboard `item`/`customer` already use.
    `customer_ean` is always '' (none of these kinds is scoped to a resolved customer);
    the caller-built synthetic `key` (e.g. `f"date:{message_id}"`) is what the existing
    partial-unique `(customer_ean, item_key) WHERE status='open'` index dedupes on."""
    if not (message_id and key):
        return None
    row = conn.execute(
        """INSERT INTO order_questions
               (message_id, customer_ean, customer_name, wording, item_key, kind,
                candidates, delivery_date, reason, payload)
           VALUES (%s, '', '', %s, %s, %s, %s, %s, %s, %s)
           ON CONFLICT (customer_ean, item_key) WHERE status = 'open' DO NOTHING
           RETURNING id""",
        (message_id, wording, key, kind, Json(candidates or []), delivery_date or "",
         reason or "", Json(payload or {}))).fetchone()
    if row:
        qid = int(row[0])
        log.info("asking the warehouse a %r question for %s (%r)", kind, message_id, wording)
        if on_new:
            try:
                on_new(get(conn, qid))
            except Exception:
                log.exception("notifying about new %r question %s failed", kind, qid)
        return qid
    existing = conn.execute(
        "SELECT id FROM order_questions WHERE customer_ean = '' AND item_key = %s"
        " AND status = 'open'", (key,)).fetchone()
    return int(existing[0]) if existing else None


def ask_mail(conn, message_id: str, sender_email: str, subject: str, reason: str = "",
            on_new=None) -> int | None:
    """Row 2 of #164's inventory + the invariant backstop: 'is this even an order?'.
    ONE question per message (`mail:<message_id>`)."""
    return ask_generic(
        conn, "mail", message_id, f"mail:{message_id}", subject or "(bez predmetu)",
        [{"value": "not_order", "label": "Toto nie je objednávka"},
         {"value": "manual", "label": "Je to objednávka — spracujem ručne"}],
        reason or "AI nenašla v e-maile žiadnu objednávku",
        {"sender_norm": _sender_norm(sender_email), "subject_key": subject_key(subject),
         "sender_email": sender_email or "", "subject": subject or ""},
        on_new=on_new)


def ask_date(conn, message_id: str, dates: list[str], reason: str,
            delivery_date: str = "", on_new=None) -> int | None:
    """Row 3: the subject/body delivery-date conflict. ONE question per message
    (`date:<message_id>`) — the whole email disagrees about ONE day, not one per order."""
    return ask_generic(
        conn, "date", message_id, f"date:{message_id}", reason,
        [{"value": d, "label": d} for d in dates], reason, {"dates": dates},
        delivery_date=delivery_date, on_new=on_new)


def ask_line(conn, message_id: str, wording: str, quantity, unit: str, reason: str,
            on_new=None) -> int | None:
    """Row 8 (`report.py`'s `unverified` phantom-item safeguard): 'does this line still
    apply?'. Keyed on the wording, so the SAME phantom line across several orders in one
    mail collapses onto one question instead of asking N times."""
    key = f"line:{message_id}:{memory.item_key(wording)}"
    return ask_generic(
        conn, "line", message_id, key, wording,
        [{"value": "keep", "label": "Áno, platí"},
         {"value": "drop", "label": "Nie, vynechať"}],
        reason, {"quantity": quantity, "unit": unit or "ks"}, on_new=on_new)


# --- the kind registry ------------------------------------------------------------
#
# Two things this makes structurally impossible to skip (#164's own design comment):
# (1) a kind with no `learns` line at all (the dataclass field is mandatory — a new kind
#     that forgets to state it fails at import, not silently in production);
# (2) an `apply` with no escape hatch — `tests/test_orders_teach.py` iterates KINDS and
#     asserts every kind offers a blank/"unknown" choice that never raises.

@dataclass(frozen=True)
class QuestionKind:
    name: str
    present: Callable[[dict], dict]
    validate: Callable[[dict, str, str], None]
    apply: Callable[[object, object, dict, str, str], dict]
    undo: Callable[[object, dict], dict]
    learns: str
    deadline_shippable: bool


def _present(q: dict, title: str, body: str, options: list[dict]) -> dict:
    return {"qid": q["id"], "kind": q.get("kind", "item"), "title": title, "body": body,
            "options": options, "unknown_label": "Neviem"}


# --- item (#88, unchanged behaviour — wrapped literally) ---

def _present_item(q: dict) -> dict:
    who = q.get("customer_name") or q.get("customer_ean") or ""
    body = f"{who} · dodanie {q.get('delivery_date') or '?'}\n{q.get('reason') or ''}"
    options = [{"value": c.get("gtin"), "label": c.get("name") or c.get("gtin")}
              for c in (q.get("candidates") or [])]
    return _present(q, q.get("wording", ""), body, options)


def _validate_item(q: dict, choice: str, by: str) -> None:
    return None  # `answer()` itself validates against the offered/catalog set


def _apply_item(conn, cfg, q: dict, choice: str, by: str) -> dict:
    """`choice` is the picked gtin. httpapi's LIVE dispatch still calls `answer()`
    directly with the click's own `card` text (a cosmetic label only) rather than through
    this registry entry — see teach.py's module docstring reference in the #164 design
    comment for why: the register's single-string `choice` contract (shared with mail/
    date/line) has no slot for a second field, and `card` is display-only (never used to
    decide anything), so this wrapper is the CORRECT, tested reference the register's own
    consistency checks (KINDS iteration, escape-hatch test) exercise — not yet httpapi's
    live path for item/customer, which keeps its full-fidelity gtin+card body unchanged."""
    from . import hold
    answered = answer(conn, q["id"], gtin=choice, card="", by=by)
    released = hold.release_for_question(conn, cfg, q["id"])
    return {"question": answered, "released": released}


def _undo_item(conn, q: dict) -> dict:
    return undo(conn, q["id"])


# --- customer (#159, unchanged behaviour — wrapped literally) ---

def _present_customer(q: dict) -> dict:
    ctx = q.get("context") or {}
    body = (f"{ctx.get('sender_name', '')} · {ctx.get('company_name', '')} · "
           f"dodanie {q.get('delivery_date') or '?'}\n{q.get('reason') or ''}")
    options = [{"value": c.get("ean_edi"), "label": c.get("name") or c.get("ean_edi")}
              for c in (q.get("candidates") or [])]
    return _present(q, ctx.get("sender_email") or q.get("wording", ""), body, options)


def _validate_customer(q: dict, choice: str, by: str) -> None:
    return None  # `answer_customer()` itself validates against the offered set


def _apply_customer(conn, cfg, q: dict, choice: str, by: str) -> dict:
    from . import hold
    answered = answer_customer(conn, q["id"], ean_edi=choice, name="", by=by)
    if not choice:
        released = hold.release_unknown_customer(conn, cfg, q["id"])
    else:
        sender_email = (q.get("context") or {}).get("sender_email", "")
        snapshot.remember_customer_email(conn, choice, sender_email)
        snapshot.rebuild_from_overrides(conn)
        hold.set_customer(conn, q["id"], choice, "")
        released = hold.release_for_question(conn, cfg, q["id"])
    return {"question": answered, "released": released}


def _undo_customer(conn, q: dict) -> dict:
    return undo(conn, q["id"])


# --- mail (#164): "is this even an order?" ---

def _present_mail(q: dict) -> dict:
    ctx = q.get("payload") or {}
    body = f"{ctx.get('sender_email', '')}\n{q.get('reason') or ''}"
    return _present(q, q.get("wording", ""), body, list(q.get("candidates") or []))


def _validate_mail(q: dict, choice: str, by: str) -> None:
    if choice and choice not in ("not_order", "manual"):
        raise NotACandidate(f"{choice!r} nie je platná odpoveď pre otázku "
                            f"{q['id']}")


def _apply_mail(conn, cfg, q: dict, choice: str, by: str) -> dict:
    from . import report
    payload = q.get("payload") or {}
    sender_norm, key = payload.get("sender_norm", ""), payload.get("subject_key", "")
    if choice == "not_order":
        action, outcome = "ignore", "Sklad potvrdil: toto nie je objednávka"
        proc_by = "ai_orders_mail_rule"
    else:
        action = "manual"
        outcome = ("Sklad potvrdil: je to objednávka — spracuj ručne v "
                  "ORIONe")
        proc_by = "ai_orders_mail_rule"
    conn.execute(
        """INSERT INTO mail_rules (sender_norm, subject_key, action, question_id)
               VALUES (%s, %s, %s, %s)
           ON CONFLICT (sender_norm, subject_key)
               DO UPDATE SET action = EXCLUDED.action, question_id = EXCLUDED.question_id
           """, (sender_norm, key, action, q["id"]))
    conn.execute(
        """UPDATE messages SET processed = true, processed_at = now(),
               processed_by = %s, processing_at = NULL
            WHERE message_id = %s""", (proc_by, q["message_id"]))
    report.log_event(conn, q["message_id"], stage="review",
                     status="ok" if action == "ignore" else "review", outcome=outcome,
                     detail={"question_id": q["id"], "mail_rule": action})
    return {}


def _undo_mail(conn, q: dict) -> dict:
    """Retract only the rule THIS question created (mirrors `global_item_memory`'s own
    `question_id`-scoped undo) and reopen the question."""
    conn.execute("DELETE FROM mail_rules WHERE question_id = %s", (q["id"],))
    conn.execute(
        """UPDATE order_questions
              SET status = 'open', answer = NULL, answered_by = NULL, answered_at = NULL,
                  reminder_sent_at = NULL, escalated_at = NULL
            WHERE id = %s""", (q["id"],))
    return get(conn, q["id"]) or {}


# --- date (#164): "which day is real?" ---

def _present_date(q: dict) -> dict:
    return _present(q, "Ktorý deň dodávky platí?",
                    q.get("reason") or "", list(q.get("candidates") or []))


def _validate_date(q: dict, choice: str, by: str) -> None:
    if not choice:
        return
    from . import edi
    if not edi._format_date(choice).strip():
        raise NotACandidate(f"{choice!r} nevyzerá ako platný dátum "
                            f"(DD.MM.RRRR)")


def _apply_date(conn, cfg, q: dict, choice: str, by: str) -> dict:
    from . import hold
    hold.set_delivery_date(conn, q["id"], choice)
    released = hold.release_for_question(conn, cfg, q["id"])
    return {"released": released}


def _undo_date(conn, q: dict) -> dict:
    conn.execute(
        """UPDATE order_questions
              SET status = 'open', answer = NULL, answered_by = NULL, answered_at = NULL,
                  reminder_sent_at = NULL, escalated_at = NULL
            WHERE id = %s""", (q["id"],))
    return get(conn, q["id"]) or {}


# --- line (#164): report.py's `unverified` phantom-item safeguard ---

def _present_line(q: dict) -> dict:
    payload = q.get("payload") or {}
    body = (f"{q.get('quantity') or payload.get('quantity') or ''} "
           f"{payload.get('unit', '')} — {q.get('reason') or ''}")
    return _present(q, q.get("wording", ""), body, list(q.get("candidates") or []))


def _validate_line(q: dict, choice: str, by: str) -> None:
    if choice and choice not in ("keep", "drop"):
        raise NotACandidate(f"{choice!r} nie je platná odpoveď pre otázku "
                            f"{q['id']}")


def _apply_line(conn, cfg, q: dict, choice: str, by: str) -> dict:
    """No held order and no decision to touch (an unverified item never made it into any
    order's decisions) — the value of asking is purely that a fabricated line never ships
    silently and a human confirmation is on record, per `learns` below."""
    from . import report
    outcome = ("Sklad potvrdil: riadok naozaj patril do objednávky — treba ju "
              "pridať ručne" if choice == "keep" else
              "Sklad potvrdil: riadok do objednávky nepatril — správne "
              "vynechaný")
    report.log_event(conn, q["message_id"], stage="review", status="ok", outcome=outcome,
                     detail={"question_id": q["id"], "wording": q.get("wording", "")})
    return {}


def _undo_line(conn, q: dict) -> dict:
    conn.execute(
        """UPDATE order_questions
              SET status = 'open', answer = NULL, answered_by = NULL, answered_at = NULL,
                  reminder_sent_at = NULL, escalated_at = NULL
            WHERE id = %s""", (q["id"],))
    return get(conn, q["id"]) or {}


# --- dl_item / dl_supplier (#202, DL migration F3): the nástenka half of the matching
# ladder in app/orders/dl_match.py. Reuse `ask_generic`/the SAME dashboard machinery
# mail/date/line already use (#164's "never a bespoke table per new kind" — see that
# section's own header comment) — `customer_ean` stays '' and the real scoping identity
# (supplier EAN / sender address) lives in the synthetic `item_key` + `payload`, exactly
# like `ask_mail`/`ask_customer` already do for THEIR own non-customer-EAN identities.
#
# Unlike mail/date/line, these two DO write durable state a future document can read back
# (`dl_item_memory(source='human')` / `dl_supplier_memory`) — closer in spirit to item/
# customer. They are wired into httpapi's GENERIC dispatch (not item/customer's bespoke
# literal-body path), so — unlike item/customer's `_validate_*` no-ops, which defer to
# `answer()`'s own DB-backed check — `_validate_dl_item`/`_validate_dl_supplier` do REAL
# checking here, against the OFFERED candidate set (no DB access in `validate`'s own
# signature, same constraint `_validate_mail`/`_validate_line` already work within).
#
# `api_orders_questions()` returns `open_questions()`'s RAW rows straight to the browser —
# no kind ever routes through its own `.present()` in the live path (verified: nothing in
# httpapi.py calls it; `.present()` is exercised only by direct unit tests, same "correct
# reference, not the live shape" caveat `_apply_item`'s own docstring already states for
# item/customer). The shared `genericQuestionCard` JS (mail/date/line, ASK_HTML + DASH_HTML)
# reads `candidates[].value`/`.label` straight off that RAW stored JSON — so `ask_dl_item`/
# `ask_dl_supplier` must store candidates in `{value, label}` shape themselves, exactly like
# `ask_mail`/`ask_date`/`ask_line` already do, not the richer gtin/name shape a caller
# naturally has on hand (that shape is accepted as the FUNCTION's own input and translated
# here — the caller never has to know the storage contract).

def _offered_values(q: dict) -> set[str]:
    return {str(c.get("value")) for c in (q.get("candidates") or [])}


def ask_dl_item(conn, message_id: str, supplier_ean: str, supplier_name: str, wording: str,
                quantity, unit: str, candidates: list[dict], delivery_date: str = "",
                reason: str = "", catalog_gtins=None, on_new=None) -> int | None:
    """Raise ONE 'ktorá karta je táto DL položka?' question, scoped per (supplier, wording) —
    a second DL from the same supplier with the same still-unresolved wording reuses the SAME
    open question, exactly like `ask()`'s own per-(customer, wording) dedupe.

    `candidates` takes the natural catalog shape (`{"gtin": ..., "name": ...}`, same as
    `dl_match.candidates()` returns) — translated to `{value, label}` before storage, see
    the section header comment above for why.

    Skips (returns `None`, asks nothing) when this exact (supplier, wording) is ALREADY
    human-taught — mirrors `ask()`'s own `recalled.human` pre-check (review finding on this
    issue's PR: without it, a future caller could raise a needless duplicate question for a
    wording `dl_memory.resolve()` would already answer for free).

    `catalog_gtins` (review finding, #240): MUST be passed by any live caller that has a
    current catalog snapshot on hand (`dl_worker._process_document` does) — without it,
    a wording taught with a GTIN that has since been RETIRED from the catalog is still
    reported as "already human-taught" here (this pre-check's OWN `dl_memory.resolve()`
    call, unlike the real matching decision in `dl_match.decide_item`, would otherwise
    apply NO catalog filter at all) and this function silently refuses to ask again —
    even though that stale mapping can never actually resolve the item (`decide_item`'s
    own, correctly-filtered lookup rejects it too). That combination is a genuine,
    permanent silent hang: the exact failure class this whole ticket exists to close,
    just for a taught-then-retired card instead of a never-taught one. `None` (the
    default) preserves the OLD, unfiltered behaviour for any caller with no catalog
    handy (e.g. a direct test)."""
    key = memory.item_key(wording)
    if not (message_id and supplier_ean and key):
        return None
    recalled = dl_memory.resolve(conn, supplier_ean, wording, catalog_gtins=catalog_gtins)
    if recalled is not None and recalled.human:
        return None
    options = [{"value": str(c.get("gtin")), "label": c.get("name") or str(c.get("gtin"))}
              for c in (candidates or [])]
    return ask_generic(
        conn, "dl_item", message_id, f"dlitem:{supplier_ean}:{key}", wording,
        options, reason or "Neznáme znenie položky na dodacom liste",
        {"supplier_ean": supplier_ean, "supplier_name": supplier_name or "",
         "quantity": quantity, "unit": unit or "ks"},
        delivery_date=delivery_date, on_new=on_new)


def _present_dl_item(q: dict) -> dict:
    payload = q.get("payload") or {}
    who = payload.get("supplier_name") or payload.get("supplier_ean") or ""
    body = f"{who} · dodanie {q.get('delivery_date') or '?'}\n{q.get('reason') or ''}"
    return _present(q, q.get("wording", ""), body, list(q.get("candidates") or []))


def _validate_dl_item(q: dict, choice: str, by: str) -> None:
    if choice and choice not in _offered_values(q):
        raise NotACandidate(f"{choice!r} nebolo ponúknuté pre otázku {q['id']}")


def _apply_dl_item(conn, cfg, q: dict, choice: str, by: str) -> dict:
    """A blank choice ('neviem, ktorá karta to je') teaches nothing — same honesty as
    `_apply_dl_supplier`'s own 'neviem' path (review finding, #240: added here for
    symmetry/defensiveness; `_api_orders_answer_generic`'s own blank-choice short-circuit
    already keeps this branch dead on the real HTTP path, but a future/direct caller
    should not be able to trigger a pointless reprocess attempt off an empty pick).

    #240: a REAL pick also gives the DOCUMENT that raised this question its own second
    chance to finish — `dl_worker.release_for_question` re-runs the message once every
    dl_item/dl_supplier question it still has open is answered. Lazy import: `dl_worker`
    imports `teach` at its own top, so a module-level import here would deadlock the same
    way `hold.py`'s own docstring already explains for its `pipeline` import."""
    if not choice:
        return {}
    payload = q.get("payload") or {}
    supplier_ean = payload.get("supplier_ean", "")
    card = next((c.get("label", "") for c in (q.get("candidates") or [])
                if str(c.get("value")) == str(choice)), "")
    dl_memory.remember(conn, supplier_ean, q.get("wording", ""), str(choice), card,
                       _today(conn), source="human")
    from . import dl_worker
    released = dl_worker.release_for_question(conn, cfg, q["id"])
    return {"released": released}


def _undo_dl_item(conn, q: dict) -> dict:
    """Only what a HUMAN taught is removed — same rule `undo()` already applies to
    `item_memory`; a genuine `source='ship'` delivery record is evidence and stays."""
    payload = q.get("payload") or {}
    conn.execute(
        "DELETE FROM dl_item_memory WHERE supplier_ean = %s AND item_key = %s "
        "AND source = 'human'", (payload.get("supplier_ean", ""), memory.item_key(
            q.get("wording", ""))))
    conn.execute(
        """UPDATE order_questions
              SET status = 'open', answer = NULL, answered_by = NULL, answered_at = NULL,
                  reminder_sent_at = NULL, escalated_at = NULL
            WHERE id = %s""", (q["id"],))
    return get(conn, q["id"]) or {}


def ask_dl_supplier(conn, message_id: str, sender_email: str, candidates: list[dict],
                    delivery_date: str = "", supplier_name: str = "",
                    supplier_city: str = "", on_new=None) -> int | None:
    """Raise ONE 'ktorý dodávateľ?' question — one per sender address, mirrors
    `ask_customer`'s own address-keyed dedupe exactly (R61's own supplier-whitelist miss).

    `candidates` takes the natural supplier shape (`{"ean_edi": ..., "name": ...}`, same as
    `dl_match.supplier_candidates()` returns) — translated to `{value, label}` before
    storage, same reasoning as `ask_dl_item` above.

    #314: `supplier_name`/`supplier_city` are the EXTRACTED document identity (the caller has
    them on hand as `doc.supplierName`/`doc.supplierCity`). Stored in the payload so a later
    'Netýka sa skladu' close (`dl_nonwarehouse.record_for_message`) can remember an
    UNREGISTERED non-warehouse supplier by its document name, not only its email address
    (req 3) — additive, defaulting to "" for every pre-#314 caller.

    Skips (returns `None`, asks nothing) when this exact address is ALREADY taught — same
    `ask_dl_item` reasoning above, using `dl_supplier_memory.resolve()` instead of
    `dl_memory.resolve()`."""
    key = str(sender_email or "").strip().lower()
    if not (message_id and key):
        return None
    if dl_supplier_memory.resolve(conn, sender_email) is not None:
        return None
    options = [{"value": str(c.get("ean_edi")), "label": c.get("name") or str(c.get("ean_edi"))}
              for c in (candidates or [])]
    return ask_generic(
        conn, "dl_supplier", message_id, f"dlsupplier:{key}", sender_email,
        options, "Dodávateľ nebol nájdený v databáze dodávateľov",
        {"sender_email": sender_email or "", "supplier_name": supplier_name or "",
         "supplier_city": supplier_city or ""},
        delivery_date=delivery_date, on_new=on_new)


def _present_dl_supplier(q: dict) -> dict:
    payload = q.get("payload") or {}
    body = f"dodanie {q.get('delivery_date') or '?'}\n{q.get('reason') or ''}"
    return _present(q, payload.get("sender_email") or q.get("wording", ""), body,
                    list(q.get("candidates") or []))


def _validate_dl_supplier(q: dict, choice: str, by: str) -> None:
    if choice and choice not in _offered_values(q):
        raise NotACandidate(f"{choice!r} nebolo ponúknuté pre otázku {q['id']}")


def _apply_dl_supplier(conn, cfg, q: dict, choice: str, by: str) -> dict:
    """A blank choice ('neviem, ktorý dodávateľ to je') teaches nothing — same honesty as
    `_apply_customer`'s own 'neviem' path; the document stays for manual review.

    #240: a REAL pick also gives the document that raised this question its own second
    chance to finish — see `_apply_dl_item`'s own docstring for why the import is lazy
    and what `release_for_question` actually does."""
    if not choice:
        return {}
    payload = q.get("payload") or {}
    name = next((c.get("label", "") for c in (q.get("candidates") or [])
                if str(c.get("value")) == str(choice)), "")
    dl_supplier_memory.remember(conn, payload.get("sender_email", ""), str(choice), name)
    from . import dl_worker
    released = dl_worker.release_for_question(conn, cfg, q["id"])
    return {"released": released}


def _undo_dl_supplier(conn, q: dict) -> dict:
    payload = q.get("payload") or {}
    dl_supplier_memory.forget(conn, payload.get("sender_email", ""))
    conn.execute(
        """UPDATE order_questions
              SET status = 'open', answer = NULL, answered_by = NULL, answered_at = NULL,
                  reminder_sent_at = NULL, escalated_at = NULL
            WHERE id = %s""", (q["id"],))
    return get(conn, q["id"]) or {}


KINDS: dict[str, QuestionKind] = {
    "item": QuestionKind(
        name="item", present=_present_item, validate=_validate_item, apply=_apply_item,
        undo=_undo_item, learns="item_memory(source='human') + global_item_memory",
        deadline_shippable=True),
    "customer": QuestionKind(
        name="customer", present=_present_customer, validate=_validate_customer,
        apply=_apply_customer, undo=_undo_customer,
        learns="customer_overrides.emails[] + rebuild (real pick); "
              "nothing: znovu sa spýtať je čestné (\"neviem\")",
        deadline_shippable=False),
    "mail": QuestionKind(
        name="mail", present=_present_mail, validate=_validate_mail, apply=_apply_mail,
        undo=_undo_mail, learns="mail_rules(sender_norm, subject_key)",
        deadline_shippable=False),
    "date": QuestionKind(
        name="date", present=_present_date, validate=_validate_date, apply=_apply_date,
        undo=_undo_date,
        learns="nothing: rozpor je vlastnosť jedného mailu, niet stabilného kľúča",
        deadline_shippable=False),
    "line": QuestionKind(
        name="line", present=_present_line, validate=_validate_line, apply=_apply_line,
        undo=_undo_line,
        learns="nothing: hodnota je, že sa nepošle vymyslený riadok bez potvrdenia",
        deadline_shippable=False),
    "dl_item": QuestionKind(
        name="dl_item", present=_present_dl_item, validate=_validate_dl_item,
        apply=_apply_dl_item, undo=_undo_dl_item, learns="dl_item_memory(source='human')",
        deadline_shippable=False),
    "dl_supplier": QuestionKind(
        name="dl_supplier", present=_present_dl_supplier, validate=_validate_dl_supplier,
        apply=_apply_dl_supplier, undo=_undo_dl_supplier,
        learns="dl_supplier_memory (real pick); nothing: znovu sa spýtať je čestné (\"neviem\")",
        deadline_shippable=False),
}
for _k, _v in KINDS.items():
    assert _v.name == _k and _v.learns, f"KINDS[{_k!r}] must declare a non-empty learns"
