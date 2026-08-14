"""The AI-orders question board — ask/answer/undo + held/taught listings (#268 krok 10).

Moved VERBATIM out of `app/httpapi.py` (no behavior change) — see the design comment on
#268 for exactly what moved and why. This is the RISKIEST step of the split: the block
carries ALL FOUR occurrences of the two-connection discipline against duplicate uploads
(`deps.db_tx()` commits a write+answer together; a SEPARATE autocommit `deps.db()`
performs the release/upload afterward, so a later unrelated failure can never roll back
an already-physically-delivered document — see each function's own docstring for the
full reasoning) plus the second, independent role-separation layer
(`_role_kinds(session.get("role"))`, guarding against a SKLAD_ROLE/SKLAD_DL_ROLE session
answering the OTHER role's question by guessing its id). Moved as ONE indivisible unit on
purpose — splitting it across files would force a reviewer to open two files to see one
commit/release sequence, exactly the auditability loss #268 exists to remove.

Security note (shared with httpapi_znalosti.py): `_gate()` (still in `httpapi.py`)
matches these routes by PATH STRING against `SKLAD_PATHS`/`SKLAD_DL_PATHS` — it runs
`before_request`, before Flask even picks a handler, and never looks at which Python
module a handler lives in. Moving this file changes nothing about that check, as long as
every `@app.get/post(...)` string below stays byte-identical to what it was before the
move.
"""
from __future__ import annotations

from flask import Flask, abort, jsonify, request, session
from psycopg.types.json import Json

from .httpapi_common import _EAN_STRIP_RE, Deps, _parse_emails_field
from .httpapi_security import _role_kinds
from .orders import dl_snapshot, snapshot


def register(app: Flask, deps: Deps) -> None:
    @app.get("/api/orders/questions")
    def api_orders_questions():
        """The wordings waiting for the warehouse (#88) — one per (customer, wording).

        #231: a SKLAD_ROLE/SKLAD_DL_ROLE session (the unauthenticated nástenka links)
        only ever sees ITS OWN kinds (`_role_kinds`) — a full dash_password login is
        unrestricted, unchanged from before this ticket.
        """
        from .orders import teach
        with deps.db() as c:
            return jsonify(items=teach.open_questions(c, kinds=_role_kinds(session.get("role"))))

    def _api_orders_answer_new_customer(qid: int, q: dict, nc: dict):
        """#234: the customer genuinely does not exist anywhere yet — the warehouse
        creates it in CODEX first (source of truth), then types the same few fields in
        here, prefilled from the mail. Same two-connection discipline as the branch below:
        the customer write, the `teach.add_candidate` audit trail, and `teach.
        answer_customer` commit together in ONE transaction; the release (a REAL external
        upload) runs afterward on its own autocommit connection, never inside the same
        rollback-able transaction — see `_api_orders_answer_customer`'s own docstring for
        why.
        """
        from .orders import hold, report, teach

        ean = _EAN_STRIP_RE.sub("", str(nc.get("ean_edi") or ""))
        if not ean:
            return jsonify(error="Bez EAN kódu EDI sa zákazník nedá uložiť — nájdeš ho v "
                                 "CODEXe pri odberateľovi."), 400
        if not ean.isdigit():
            return jsonify(error="EAN kód EDI musí byť len číslice."), 400
        name = str(nc.get("name") or "").strip()
        if not name:
            return jsonify(error="chýba názov"), 400

        ctx = q.get("context") or {}
        emails = _parse_emails_field(nc.get("emails"))
        ctx_email = str(ctx.get("sender_email") or "").strip().lower()
        if ctx_email and ctx_email not in [e.lower() for e in emails]:
            emails.append(ctx_email)
        city = str(nc.get("city") or "").strip()
        street = str(nc.get("street") or "").strip()
        zip_ = str(nc.get("zip") or "").strip()

        try:
            with deps.db_tx() as c:
                # #234 review finding: an unconditional 409 here (no "confirm and
                # proceed anyway" escape hatch) — the earlier draft had one
                # (`confirm_existing`), but it was reachable with no real caller and
                # would have bypassed the exact EAN-uniqueness guarantee this ticket
                # exists to add (`upsert_customer`'s own reclaim raises `DuplicateEan`
                # for a DIFFERENT street rather than silently inserting a second row —
                # #248 tightened this from a silent duplicate to a raised conflict; a
                # forced "confirm anyway" here would still bypass it either way). The
                # card's own reaction to a 409 is "Doplniť e-mail k <name>", which
                # re-posts through the EXISTING-customer path below — never a forced
                # re-submit of new_customer.
                existing = [r for r in snapshot.customers_for_management(c)
                           if str(r.get("ean_edi") or "") == ean]
                if existing:
                    hit = existing[0]
                    return jsonify(
                        error=f"EAN {ean} už má zákazník {hit.get('name', '')}.",
                        existing={"ean_edi": hit.get("ean_edi", ""),
                                 "name": hit.get("name", ""),
                                 "street": hit.get("street", ""),
                                 "override_id": hit.get("override_id")}), 409
                snapshot.upsert_customer(c, override_id=None, orig_ean_edi=None,
                                         orig_street=None, ean_edi=ean, name=name,
                                         emails=emails, city=city, street=street,
                                         zip_=zip_)
                snapshot.rebuild_from_overrides(c)
                teach.add_candidate(c, qid, {"ean_edi": ean, "name": name, "city": city,
                                             "street": street, "address_match": False,
                                             "source": "new"})
                answered = teach.answer_customer(c, qid, ean_edi=ean, name=name, by="sklad")
                report.log_event(
                    c, q["message_id"], stage="review", status="ok",
                    outcome=f"Sklad doplnil nového zákazníka {name} ({ean})",
                    detail={"question_id": qid, "ean_edi": ean}, rollup=False)
        except teach.AlreadyAnswered as e:
            return jsonify(error=str(e)), 409
        except teach.NotACandidate as e:
            return jsonify(error=str(e)), 400
        except snapshot.InvalidCustomer as e:
            return jsonify(error=str(e)), 400
        except snapshot.DuplicateEan as e:
            # #248: the pre-check above already returns this same shape for the
            # sequential case; this is the LOSER of a genuine race — `upsert_customer`
            # detected it INSIDE the advisory lock, after the pre-check above had
            # already passed for both requests.
            return jsonify(
                error=f"EAN {ean} už má zákazník {e.existing.get('name', '')}.",
                existing=e.existing), 409

        sender_email = ctx.get("sender_email", "")
        with deps.db() as c2:
            snapshot.remember_customer_email(c2, ean, sender_email)
            hold.set_customer(c2, qid, ean, name)
            released = hold.release_for_question(c2, deps.cfg, qid)
        return jsonify(ok=True, question=answered, released=released,
                       customer={"ean_edi": ean, "name": name})

    def _api_orders_answer_customer(qid: int, q: dict, body: dict):
        """#159/#234: the customer-half of the same click — "this order belongs to THIS
        customer" (a frozen candidate button, OR a customer found via the live search box
        — #234), a brand-new customer typed in from CODEX (`new_customer`, #234), or
        "neviem, kto to je". A real pick durably remembers the sender address (#128's
        override mechanism) and releases through the SAME `_ship_one`/`edi.claim_send`
        ledger as the product half, now that the customer is known — `hold.set_customer`
        must land BEFORE `release_for_question`, which builds the `Matched` object
        straight from `held_orders.customer_ean`/`customer_name`. Both the remember-write
        and the release run on ONE autocommit connection, same reasoning as the product
        half's own docstring above (a real external upload must never share a
        rollback-able transaction with anything after it).
        """
        from .orders import hold, teach
        if isinstance(body.get("new_customer"), dict):
            return _api_orders_answer_new_customer(qid, q, body["new_customer"])
        unknown = bool(body.get("unknown"))
        ean_edi = "" if unknown else str(body.get("ean_edi") or "")
        name = "" if unknown else str(body.get("name") or "")
        if not unknown and not ean_edi:
            return jsonify(error="chýba zákazník"), 400
        try:
            with deps.db_tx() as c:
                # #234: a pick may come from the live search box over ALL current
                # customers, never just the frozen candidate set the question was asked
                # with — legitimise it server-side (never trust the client) before
                # answer_customer's own "must have been offered" check would refuse it.
                offered = {str(cd.get("ean_edi")) for cd in q["candidates"]}
                if not unknown and ean_edi not in offered:
                    hit = next((r for r in snapshot.customers_for_management(c)
                               if str(r.get("ean_edi") or "") == ean_edi
                               and r.get("ean_edi")), None)
                    if not hit:
                        return jsonify(error="Tento zákazník nie je v databáze."), 400
                    teach.add_candidate(c, qid, {
                        "ean_edi": hit["ean_edi"], "name": hit.get("name", ""),
                        "city": hit.get("city", ""), "street": hit.get("street", ""),
                        "address_match": False, "source": "search"})
                    name = name or hit.get("name", "")
                answered = teach.answer_customer(c, qid, ean_edi=ean_edi, name=name,
                                                 by="sklad")
        except teach.AlreadyAnswered as e:
            return jsonify(error=str(e)), 409
        except teach.NotACandidate as e:
            return jsonify(error=str(e)), 400
        if not ean_edi:
            with deps.db() as c2:
                released = hold.release_unknown_customer(c2, deps.cfg, qid)
            return jsonify(ok=True, question=answered, released=released)
        sender_email = (q.get("context") or {}).get("sender_email", "")
        with deps.db() as c2:
            snapshot.remember_customer_email(c2, ean_edi, sender_email)
            snapshot.rebuild_from_overrides(c2)
            hold.set_customer(c2, qid, ean_edi, name)
            released = hold.release_for_question(c2, deps.cfg, qid)
        return jsonify(ok=True, question=answered, released=released)

    def _api_orders_answer_new_dl_supplier(qid: int, q: dict, ns: dict):
        """#235: the DL-supplier half of the same "genuinely new, not just unoffered"
        card action #234 gave customers — HK LOAN (#236) is the concrete case. Validate
        the EAN-EDI (never forgettable — same helper #234 established, reused with
        `entity="dodávateľ"`), write the supplier, extend the question's OWN offered
        candidate set (`teach.add_candidate` — never bypass `_validate_dl_supplier`'s
        own check), then fall through to the SAME generic answer path every other
        dl_supplier pick already uses."""
        from .orders import teach
        ean = _EAN_STRIP_RE.sub("", str(ns.get("ean_edi") or ""))
        if not ean:
            return jsonify(error="Bez EAN kódu EDI sa dodávateľ nedá uložiť — nájdeš ho "
                                 "v CODEXe pri dodávateľovi."), 400
        if not ean.isdigit():
            return jsonify(error="EAN kód EDI musí byť len číslice."), 400
        name = str(ns.get("name") or "").strip()
        if not name:
            return jsonify(error="chýba názov"), 400
        emails = _parse_emails_field(ns.get("emails"))
        # `ask_dl_supplier`/`ask_generic` store the sender address in `payload`, not
        # `context` (that column is `customer`-kind-only, see `ask_customer`) — the
        # bug this fixes: reading `context` here always returns {} for a dl_supplier
        # question, so the sender's own address was silently never appended.
        ctx = q.get("payload") or {}
        ctx_email = str(ctx.get("sender_email") or "").strip().lower()
        if ctx_email and ctx_email not in [e.lower() for e in emails]:
            emails.append(ctx_email)
        city = str(ns.get("city") or "").strip()
        try:
            with deps.db_tx() as c:
                # Deep-review finding on #235 (mirrors #234's own new_customer collision
                # check above): `upsert_dl_supplier`'s advisory-lock reclaim only fires on
                # an EXACT (ean_edi, city) match against an un-overridden row — it checks
                # neither the frozen base snapshot nor an already-overridden supplier under
                # a DIFFERENT (or blank — city is optional in this quick form) city. Without
                # this check, entering an EAN that already belongs to a real supplier under
                # another city silently inserts a SECOND row sharing that ean_edi; both then
                # land in dl_suppliers_for_management and dl_match.py picks whichever comes
                # first, possibly a stale name. So: refuse up front, same shape as the
                # customer path, ignoring city on purpose (the collision is on the EAN).
                #
                # #248 (was "Residual, independent review, same PR" — CLOSED): this read
                # happens BEFORE `upsert_dl_supplier`'s own advisory lock is taken, so two
                # genuinely SIMULTANEOUS "new supplier" submissions for the SAME
                # never-before-seen EAN under DIFFERENT city values could both pass this
                # fast-path check before either commits. The race is no longer open,
                # though: `upsert_dl_supplier`'s own reclaim SELECT (inside the lock) is
                # now scoped by ean_edi alone, so it tells a genuine retry (same city)
                # apart from a real second submission (different city) and raises
                # `DuplicateEan` for the latter — caught below, same 409 shape this
                # fast-path check already returns. A DB-level partial unique index
                # (db.py's #248 migration) backstops both paths. See `upsert_dl_supplier`'s
                # own docstring for the full trace; `upsert_customer`'s #248 fix is the
                # identical shape, mirrored for `street` instead of `city`.
                existing = [r for r in dl_snapshot.dl_suppliers_for_management(c)
                           if str(r.get("ean_edi") or "") == ean]
                if existing:
                    hit = existing[0]
                    return jsonify(
                        error=f"EAN {ean} už má dodávateľ {hit.get('name', '')}.",
                        existing={"ean_edi": hit.get("ean_edi", ""),
                                 "name": hit.get("name", ""),
                                 "city": hit.get("city", ""),
                                 "override_id": hit.get("override_id")}), 409
                dl_snapshot.upsert_dl_supplier(
                    c, override_id=None, orig_ean_edi=None, orig_city=None,
                    ean_edi=ean, name=name, emails=emails, city=city)
                dl_snapshot.dl_rebuild_from_overrides(c)
                teach.add_candidate(c, qid, {"value": ean, "label": name})
        except snapshot.InvalidCustomer as e:
            return jsonify(error=str(e)), 400
        except snapshot.DuplicateEan as e:
            return jsonify(
                error=f"EAN {ean} už má dodávateľ {e.existing.get('name', '')}.",
                existing=e.existing), 409
        with deps.db() as c2:
            q2 = teach.get(c2, qid)
        if q2 is None:
            return jsonify(error="Otázka už neexistuje."), 404
        return _api_orders_answer_generic(qid, q2, {"choice": ean, "by": "sklad"})

    def _api_orders_answer_new_dl_item(qid: int, q: dict, ni: dict):
        """#235: the DL-item half — a genuinely new catalog card with no GTIN in Codex
        yet (the #236 "Soľ jedlá..." case). Same shape as the supplier branch above."""
        from .orders import teach
        gtin = _EAN_STRIP_RE.sub("", str(ni.get("gtin") or ""))
        if not gtin:
            return jsonify(error="Bez GTIN sa karta nedá uložiť — nájdeš ho v CODEXe "
                                 "pri produkte."), 400
        if not gtin.isdigit():
            return jsonify(error="GTIN musí byť len číslice."), 400
        name = str(ni.get("name") or "").strip()
        if not name:
            return jsonify(error="chýba názov"), 400
        with deps.db_tx() as c:
            dl_snapshot.upsert_dl_catalog_card(c, gtin, name)
            dl_snapshot.dl_rebuild_from_overrides(c)
            teach.add_candidate(c, qid, {"value": gtin, "label": name})
        with deps.db() as c2:
            q2 = teach.get(c2, qid)
        if q2 is None:
            return jsonify(error="Otázka už neexistuje."), 404
        return _api_orders_answer_generic(qid, q2, {"choice": gtin, "by": "sklad"})

    def _api_orders_answer_generic(qid: int, q: dict, body: dict):
        """#164: the SAME dispatch endpoint, generalized for kinds beyond item/customer
        (mail/date/line, and #235's dl_item/dl_supplier) — a UNIFIED `{"choice": ...,
        "by": ...}` body, routed through `teach.KINDS[q['kind']]`. `choice` blank/
        `"unknown"` is the universal escape hatch (constraint 5 of #164): the question
        stays OPEN and visible instead of being silently marked answered with nothing.

        #235: a `new_supplier`/`new_item` body (mirrors `customer`'s own `new_customer`
        branch) means the pick genuinely does not exist yet — dispatched BEFORE the
        open/kind checks below, same as `_api_orders_answer_customer` does for
        `new_customer`."""
        if q.get("kind") == "dl_supplier" and isinstance(body.get("new_supplier"), dict):
            return _api_orders_answer_new_dl_supplier(qid, q, body["new_supplier"])
        if q.get("kind") == "dl_item" and isinstance(body.get("new_item"), dict):
            return _api_orders_answer_new_dl_item(qid, q, body["new_item"])
        from .orders import teach
        kind = teach.KINDS.get(q.get("kind", ""))
        if not kind:
            return jsonify(error=f"neznámy druh otázky: {q.get('kind')!r}"), 400
        if q.get("status") != "open":
            return jsonify(error=f"otázka {qid} je už zodpovedaná"), 409
        raw = body.get("choice")
        choice = "" if raw in (None, "unknown") else str(raw)
        by = str(body.get("by") or "sklad")
        # Deep-review finding (independent review, same PR): dlSupplierSearchBox/
        # dlItemSearchBox (#235) search over the FULL current DL supplier/catalog list,
        # not just this question's frozen candidates — but `_validate_dl_supplier`/
        # `_validate_dl_item` only ever accept an OFFERED value, so a search hit (or the
        # new collision-reclaim button in newDlSupplierForm above) that was never in the
        # original candidate set was silently rejected with 400 "nebolo ponúknuté", even
        # though it IS a real, current supplier/card — the "live search over everything"
        # promise this ticket's own design comment describes was structurally unreachable.
        # Mirrors what `_api_orders_answer_customer` already does for its OWN search box
        # (legitimise server-side before validating) — scoped to the two DL kinds only;
        # mail/date/line have no search box and keep the strict offered-only check as-is.
        offered = {str(c.get("value")) for c in (q.get("candidates") or [])}
        if choice and choice not in offered and q.get("kind") in ("dl_supplier", "dl_item"):
            with deps.db() as clook:
                if q.get("kind") == "dl_supplier":
                    hit = next((r for r in dl_snapshot.dl_suppliers_for_management(clook)
                               if str(r.get("ean_edi") or "") == choice), None)
                else:
                    hit = next((r for r in dl_snapshot.dl_catalog_for_management(clook)
                               if str(r.get("gtin") or "") == choice), None)
                if hit:
                    cand = {"value": choice, "label": hit.get("name", "")}
                    teach.add_candidate(clook, qid, cand)
                    q = dict(q, candidates=[*(q.get("candidates") or []), cand])
        try:
            kind.validate(q, choice, by)
        except teach.NotACandidate as e:
            return jsonify(error=str(e)), 400
        if not choice:
            # #305 (variant A): the EXPLICIT „Neviem" payload ({"choice":"unknown"}) on a
            # DL question is NOT the #164 stays-open escape it is for mail/date/line — it
            # ODLOŽ (defers) the whole delivery note. Terminal + message-level, one
            # transaction, nothing to ORION (`dl_worker.close_message_sklad_unknown`), the
            # sibling of #307's not_warehouse. We key on the explicit `unknown` value, not
            # merely an absent choice, so a stray empty body stays a harmless no-op and can
            # never trigger an accidental terminal defer (review finding). Every other kind
            # keeps the universal escape (the question stays open + visible).
            if raw == "unknown" and q.get("kind") in ("dl_item", "dl_supplier"):
                from .orders import dl_worker
                with deps.db_tx() as c:
                    res = dl_worker.close_message_sklad_unknown(c, qid)
                return jsonify(ok=True, sklad_unknown=True, closed=res.get("closed", 0))
            return jsonify(ok=True, question=q, released=[])
        # Same split as the item/customer branches above (review finding on PR #116,
        # reused here): the answer itself commits in its own transaction; `apply` (which
        # for `date` releases a held order — a REAL external upload) runs afterward on an
        # autocommit connection, so a later, unrelated failure can never roll back an
        # already-physically-uploaded document.
        #
        # Deep-review finding on #235: the `q.get("status") != "open"` check above is a
        # Python-level read from an EARLIER select (the `q` this function was called
        # with), not a WHERE-clause guard on this write — same class of race
        # `answer_customer` (teach.py) was already hardened against on #234's own review.
        # The new_supplier/new_item branches now route through here too, so two
        # concurrent answers to the same question could both pass the check above and
        # the second write would silently overwrite the first's `answered_by`/
        # `answered_at`. Guard the write itself and re-check on 0 rows affected.
        with deps.db_tx() as c:
            row = c.execute(
                """UPDATE order_questions
                      SET status = 'answered', answer = %s, answered_by = %s,
                          answered_at = now()
                    WHERE id = %s AND status = 'open'
                    RETURNING id""", (Json({"choice": choice}), by, qid)).fetchone()
        if not row:
            return jsonify(error=f"otázka {qid} je už zodpovedaná"), 409
        with deps.db() as c2:
            extra = kind.apply(c2, deps.cfg, q, choice, by) or {}
        with deps.db() as c3:
            answered = teach.get(c3, qid)
        return jsonify(ok=True, question=answered, released=extra.get("released", []))

    @app.post("/api/orders/question/<int:qid>/answer")
    def api_orders_answer(qid: int):
        """One click: this wording IS this card. Taught for that customer, forever. Or,
        for a `kind='customer'` question (#159), this order belongs to THIS customer.

        If this was the LAST open question an order was held for (#93), the answer also
        releases it — the document is built and uploaded right here, once.

        The release runs on its OWN autocommit connection, deliberately NOT inside the
        `teach.answer` transaction above (review finding on PR #116): `hold.release_for_
        question` claims the `edi_sent` ledger row and then calls the real, external
        `upload()` — if that claim lived inside a rollback-able transaction and something
        AFTER the upload later failed (e.g. `report.log_event`), the whole transaction,
        INCLUDING the ledger claim, would roll back even though the document had already
        been physically delivered to ORION — a retry would then see no claim and upload a
        SECOND document, exactly the #81.1 defect this feature exists to prevent. Autocommit
        makes the claim durable the instant it is written, matching the same safe pattern
        `worker.tick` / `hold.release_due` already use (proven by
        `test_the_edi_ledger_itself_refuses_a_repeated_release_not_just_the_status_flag`).
        """
        from .orders import hold, teach
        body = request.get_json(silent=True) or {}
        with deps.db() as c0:
            q0 = teach.get(c0, qid)
        if not q0:
            return jsonify(error="otázka neexistuje"), 404
        # #231: a SKLAD_ROLE/SKLAD_DL_ROLE session may answer only ITS OWN kinds — the
        # id-based endpoint is otherwise shared, so this is the real boundary that keeps
        # the two nástenka links from reaching each other's agenda by guessing an id.
        allowed = _role_kinds(session.get("role"))
        if allowed is not None and q0.get("kind", "item") not in allowed:
            abort(403)
        # #307: "netýka sa skladu" — the whole mail is not a warehouse delivery note
        # (a režíjna faktúra / promo, the KLEŠČ case). Terminal + message-level: close
        # every open DL question of the message, mark it handled WITHOUT EDI, upload
        # nothing (see dl_worker.close_message_not_warehouse). DL kinds only — the AI/
        # orders board already has its own "Toto nie je objednávka" (mail `not_order`).
        if (q0.get("kind") in ("dl_item", "dl_supplier")
                and body.get("not_warehouse") is True):
            from .orders import dl_worker
            # No external side effect (no upload) — so the three writes (close questions,
            # mark message handled, log the skip event) run in ONE transaction, the
            # project's "must-land-together, autocommit only for external side effects"
            # pattern. A rollback here is safe precisely because nothing was shipped.
            with deps.db_tx() as c:
                res = dl_worker.close_message_not_warehouse(c, qid)
            return jsonify(ok=True, not_warehouse=True, closed=res.get("closed", 0))
        if q0.get("kind") == "customer":
            return _api_orders_answer_customer(qid, q0, body)
        if q0.get("kind") in ("mail", "date", "line", "dl_item", "dl_supplier"):
            return _api_orders_answer_generic(qid, q0, body)

        gtin, card = str(body.get("gtin") or ""), str(body.get("card") or "")
        if not gtin:
            return jsonify(error="chýba karta"), 400
        try:
            with deps.db_tx() as c:
                q = teach.answer(c, qid, gtin=gtin, card=card, by="sklad")
        except teach.AlreadyAnswered as e:
            return jsonify(error=str(e)), 409
        except teach.NotACandidate as e:
            return jsonify(error=str(e)), 400
        with deps.db() as c2:
            released = hold.release_for_question(c2, deps.cfg, qid)
        return jsonify(ok=True, question=q, released=released)

    @app.get("/api/orders/held")
    def api_orders_held():
        """Orders waiting on an answer, with their delivery date (#93) — so nothing waits
        invisibly: every one of these also has an open question on /otazky."""
        from .orders import hold
        with deps.db() as c:
            items = hold.list_held(c)
        return jsonify(items=[{
            "id": i["id"], "customer_name": i["customer_name"], "customer_ean": i["customer_ean"],
            "delivery_date": i["delivery_date"], "order_number": i["order_number"],
            "question_ids": i["question_ids"],
            "created_at": i["created_at"].isoformat() if i["created_at"] else None,
        } for i in items])

    @app.get("/api/orders/taught")
    def api_orders_taught():
        """What the warehouse has already taught — so a mis-click can be corrected.

        #231: role-scoped exactly like `/api/orders/questions` above.
        """
        from .orders import teach
        with deps.db() as c:
            return jsonify(items=teach.recently_taught(c, kinds=_role_kinds(session.get("role"))))

    @app.post("/api/orders/question/<int:qid>/undo")
    def api_orders_undo(qid: int):
        """Take a mistaken teaching back — it would otherwise decide that line forever.

        Routed through the SAME `teach.KINDS[kind].undo` every OTHER dispatch in this file
        already uses (#202 review pass — the previous `mail`-only special case silently left
        `dl_item`/`dl_supplier` on the bare `teach.undo` fallback, which never touches
        `dl_item_memory`/`dl_supplier_memory` at all: an undone DL teaching would reopen the
        question but keep the wrong mapping live). Behavior-preserving for every existing kind
        — `item`/`customer`/`date`/`line`'s own registered `undo` already delegates to the
        exact same `teach.undo(conn, qid)` call this replaces; `mail` is the one kind whose
        registered `undo` does more (retracts its own `mail_rules` row), and it already went
        through the registry before this change too.
        """
        from .orders import teach
        try:
            with deps.db_tx() as c:
                q0 = teach.get(c, qid)
                if not q0:
                    return jsonify(error="otázka neexistuje"), 404
                # #231: same role/kind boundary as the answer endpoint above.
                allowed = _role_kinds(session.get("role"))
                if allowed is not None and q0.get("kind", "item") not in allowed:
                    abort(403)
                kind = teach.KINDS.get(q0.get("kind", "item"))
                q = kind.undo(c, q0) if kind else teach.undo(c, qid)
        except teach.NotACandidate as e:
            return jsonify(error=str(e)), 404
        return jsonify(ok=True, question=q)
