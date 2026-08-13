"""The `/znalosti` page + its 12 knowledge-DB CRUD routes (#268 krok 9).

Moved VERBATIM out of `app/httpapi.py` (no behavior change) — see the design comment on
#268 for exactly what moved and why. Direct curation of wording->card knowledge without
waiting for the pipeline to raise an `order_questions` row first (#104, #127/#128, #221):
products/clients (AI-orders catalog, on `snapshot.py`'s own versioning line) and
dl-products/dl-suppliers (DL catalog, on `dl_snapshot.py`'s own separate versioning
line), plus global/customer-alias wording maps (`memory.py`), all layered as overrides
on top of their own frozen snapshot.

Security note (the whole reason #268 exists): `_gate()` (still in `httpapi.py`) matches
these routes by PATH STRING against `SKLAD_ZNALOSTI_API`/`SKLAD_DL_ZNALOSTI_API` — it
runs `before_request`, before Flask even picks a handler, and never looks at which
Python module a handler lives in. Moving this file changes nothing about that check, as
long as every `@app.get/post/delete(...)` string below stays byte-identical to what it
was before the move.
"""
from __future__ import annotations

from flask import Flask, jsonify, request, session

from . import __version__
from .httpapi_common import _EAN_STRIP_RE, Deps, _fold, _parse_emails_field
from .httpapi_templates import ZNALOSTI_HTML
from .orders import dl_snapshot, memory, snapshot


def register(app: Flask, deps: Deps) -> None:
    # ---- /znalosti (#104): direct curation of wording->card knowledge, without waiting
    # for the pipeline to raise an order_questions row first (the ask/answer/undo flow
    # above only ever reacts to what the pipeline already asked about). ----

    @app.get("/znalosti")
    @app.get("/znalosti/<ean>")
    def znalosti_page(ean: str = ""):
        # #235: the DL product/supplier boxes call `/api/znalosti/dl-products`/
        # `dl-suppliers` — SKLAD_ROLE (the orders-only warehouse link) no longer has API
        # access to those (SKLAD_ZNALOSTI_API narrowed, see this ticket's own boundary
        # requirement). Rendering the boxes anyway would fire two 401s the instant the
        # page loads for that role (a real, dirty browser-console failure, caught by the
        # existing Playwright coverage) — so a non-admin session gets the page WITHOUT
        # them; a real dash_password login (`session["auth"]`) is unaffected.
        dl_boxes = ("    W.appendChild(dlProductsBox());\n"
                   "    W.appendChild(dlSuppliersBox());\n") if session.get("auth") else ""
        return (ZNALOSTI_HTML.replace("__VERSION__", __version__)
               .replace("__DL_BOXES__", dl_boxes))

    def _current_catalog(c):
        sid = snapshot.latest_snapshot_id(c)
        return snapshot.load_catalog(c, sid) if sid else []

    def _current_customers(c):
        sid = snapshot.latest_snapshot_id(c)
        return snapshot.load_customers(c, sid) if sid else []

    def _customer_name(c, ean: str) -> str:
        for row in _current_customers(c):
            if row["ean_edi"] == ean:
                return row["name"]
        return ""

    @app.get("/api/znalosti/catalog")
    def api_znalosti_catalog():
        q = _fold((request.args.get("q") or "").strip())
        with deps.db() as c:
            rows = _current_catalog(c)
        if q:
            rows = [r for r in rows if q in _fold(r["name"]) or q in _fold(r["gtin"])]
        return jsonify(items=[{"gtin": r["gtin"], "name": r["name"]} for r in rows[:30]])

    @app.get("/api/znalosti/customers")
    def api_znalosti_customers():
        q = _fold((request.args.get("q") or "").strip())
        with deps.db() as c:
            rows = _current_customers(c)
        if q:
            rows = [r for r in rows if q in _fold(r["name"]) or q in _fold(r["ean_edi"])]
        return jsonify(items=[{"ean_edi": r["ean_edi"], "name": r["name"]} for r in rows[:30]])

    @app.get("/api/znalosti/global")
    def api_znalosti_global():
        with deps.db() as c:
            return jsonify(items=memory.list_global_aliases(c))

    @app.post("/api/znalosti/global")
    def api_znalosti_global_add():
        body = request.get_json(silent=True) or {}
        wording, gtin = str(body.get("wording") or "").strip(), str(body.get("gtin") or "")
        if not (wording and gtin):
            return jsonify(error="chýba znenie alebo karta"), 400
        with deps.db() as c:
            rid = memory.add_global_alias(c, wording, gtin, str(body.get("card") or ""),
                                          by="sklad")
        if rid is None:
            return jsonify(error="toto znenie je už globálne priradené"), 409
        return jsonify(ok=True, id=rid)

    @app.delete("/api/znalosti/global/<int:rid>")
    def api_znalosti_global_delete(rid: int):
        with deps.db() as c:
            ok = memory.delete_global_row(c, rid)
        return jsonify(ok=True) if ok else (jsonify(error="nenájdené"), 404)

    @app.get("/api/znalosti/customer/<ean>")
    def api_znalosti_customer(ean: str):
        with deps.db() as c:
            # #128: the FIRST matching row when several share this EAN — same fallback
            # `_customer_name` above already accepts, and the /znalosti/<ean> edit form
            # only ever addresses one at a time from this page.
            record = next((r for r in snapshot.customers_for_management(c)
                          if r["ean_edi"] == ean), None)
            return jsonify(customer_name=_customer_name(c, ean), record=record,
                           items=memory.list_customer_aliases(c, ean))

    @app.post("/api/znalosti/customer/<ean>")
    def api_znalosti_customer_add(ean: str):
        body = request.get_json(silent=True) or {}
        wording, gtin = str(body.get("wording") or "").strip(), str(body.get("gtin") or "")
        if not (wording and gtin):
            return jsonify(error="chýba znenie alebo karta"), 400
        with deps.db() as c:
            rid = memory.add_customer_alias(c, ean, wording, gtin, str(body.get("card") or ""))
        if rid is None:
            return jsonify(error="toto znenie je už tomuto zákazníkovi priradené"), 409
        return jsonify(ok=True, id=rid)

    @app.delete("/api/znalosti/customer/<ean>/<int:rid>")
    def api_znalosti_customer_delete(ean: str, rid: int):
        with deps.db() as c:
            ok = memory.delete_item_memory_row(c, rid, ean)
        return jsonify(ok=True) if ok else (jsonify(error="nenájdené"), 404)

    # ---- /znalosti (#127/#128): direct add/edit/retire of the product cards and
    # customers themselves, layered as overrides ON TOP of the frozen base snapshot —
    # an override always wins, and is versioned the same way
    # (snapshot.rebuild_from_overrides freezes a new snapshot immediately, so the
    # change is visible on this same page right away — no network call, no periodic
    # refresh to wait for; the sheet itself is never read at all since #129). ----

    @app.get("/api/znalosti/products")
    def api_znalosti_products():
        q = _fold((request.args.get("q") or "").strip())
        with deps.db() as c:
            rows = snapshot.catalog_for_management(c)
        if q:
            rows = [r for r in rows if q in _fold(r["name"]) or q in _fold(r["gtin"])]
        rows.sort(key=lambda r: _fold(r["name"]))
        return jsonify(items=rows[:50])

    @app.post("/api/znalosti/products")
    def api_znalosti_products_upsert():
        body = request.get_json(silent=True) or {}
        gtin = str(body.get("gtin") or "").strip()
        name = str(body.get("name") or "").strip()
        if not (gtin and name):
            return jsonify(error="chýba GTIN alebo názov"), 400
        with deps.db() as c:
            snapshot.upsert_catalog_card(c, gtin, name)
            snapshot.rebuild_from_overrides(c)
        return jsonify(ok=True)

    @app.delete("/api/znalosti/products/<gtin>")
    def api_znalosti_products_retire(gtin: str):
        with deps.db() as c:
            ok = snapshot.retire_catalog_card(c, gtin)
            if ok:
                snapshot.rebuild_from_overrides(c)
        return jsonify(ok=True) if ok else (jsonify(error="nenájdené"), 404)

    @app.get("/api/znalosti/clients")
    def api_znalosti_clients():
        q = _fold((request.args.get("q") or "").strip())
        with deps.db() as c:
            rows = snapshot.customers_for_management(c)
        if q:
            rows = [r for r in rows if q in _fold(r["name"]) or q in _fold(r["ean_edi"])]
        rows.sort(key=lambda r: _fold(r["name"]))
        return jsonify(items=rows[:50])

    @app.post("/api/znalosti/clients")
    def api_znalosti_clients_upsert():
        body = request.get_json(silent=True) or {}
        name = str(body.get("name") or "").strip()
        if not name:
            return jsonify(error="chýba názov"), 400
        # #234: the identical EAN validation the question-card "new customer" flow uses —
        # the EAN must never be forgettable, whichever screen a customer is added from.
        ean = _EAN_STRIP_RE.sub("", str(body.get("ean_edi") or ""))
        if not ean:
            return jsonify(error="Bez EAN kódu EDI sa zákazník nedá uložiť — nájdeš ho v "
                                 "CODEXe pri odberateľovi."), 400
        if not ean.isdigit():
            return jsonify(error="EAN kód EDI musí byť len číslice."), 400
        from .orders import hold
        try:
            with deps.db() as c:
                rid = snapshot.upsert_customer(
                    c, override_id=body.get("override_id"),
                    orig_ean_edi=body.get("orig_ean_edi"),
                    orig_street=body.get("orig_street"),
                    ean_edi=ean, name=name,
                    emails=_parse_emails_field(body.get("emails")),
                    city=str(body.get("city") or "").strip(),
                    street=str(body.get("street") or "").strip(),
                    zip_=str(body.get("zip") or "").strip())
                snapshot.rebuild_from_overrides(c)
                # #234: this save may be exactly what an ALREADY-OPEN customer question
                # was waiting for (the customer was added on /znalosti instead of on the
                # card) — never leave that order stuck until the periodic worker sweep
                # catches up.
                hold.retry_unknown_customer_questions(c, deps.cfg)
        except snapshot.DuplicateEan as e:
            # #248 review finding: this admin dashboard save funnels through the SAME
            # `upsert_customer` as the warehouse question-card flow, so it can raise the
            # same conflict — same 409 shape either way.
            return jsonify(
                error=f"EAN {ean} už má zákazník {e.existing.get('name', '')}.",
                existing=e.existing), 409
        return jsonify(ok=True, id=rid)

    @app.delete("/api/znalosti/clients")
    def api_znalosti_clients_retire():
        body = request.get_json(silent=True) or {}
        with deps.db() as c:
            ok = snapshot.retire_customer(
                c, override_id=body.get("override_id"),
                orig_ean_edi=body.get("orig_ean_edi"), orig_street=body.get("orig_street"))
            if ok:
                snapshot.rebuild_from_overrides(c)
        return jsonify(ok=True) if ok else (jsonify(error="nenájdené"), 404)

    # ---- /znalosti (#221): direct add/edit/retire of the DL catalog cards + suppliers,
    # mirroring the #127/#128 products/clients routes above 1:1 but on DL's own separate
    # dl_snapshots versioning line (see dl_snapshot.py's module docstring for why). ----

    @app.get("/api/znalosti/dl-products")
    def api_znalosti_dl_products():
        q = _fold((request.args.get("q") or "").strip())
        with deps.db() as c:
            rows = dl_snapshot.dl_catalog_for_management(c)
        if q:
            rows = [r for r in rows if q in _fold(r["name"]) or q in _fold(r["gtin"])]
        rows.sort(key=lambda r: _fold(r["name"]))
        return jsonify(items=rows[:50])

    @app.post("/api/znalosti/dl-products")
    def api_znalosti_dl_products_upsert():
        body = request.get_json(silent=True) or {}
        gtin = str(body.get("gtin") or "").strip()
        name = str(body.get("name") or "").strip()
        if not (gtin and name):
            return jsonify(error="chýba GTIN alebo názov"), 400
        with deps.db() as c:
            dl_snapshot.upsert_dl_catalog_card(
                c, gtin, name, doplnok=str(body.get("doplnok") or "").strip(),
                mass=dl_snapshot.parse_number(body.get("mass")),
                sklad=str(body.get("sklad") or "").strip(),
                cena=dl_snapshot.parse_number(body.get("cena")))
            dl_snapshot.dl_rebuild_from_overrides(c)
        return jsonify(ok=True)

    @app.delete("/api/znalosti/dl-products/<gtin>")
    def api_znalosti_dl_products_retire(gtin: str):
        with deps.db() as c:
            ok = dl_snapshot.retire_dl_catalog_card(c, gtin)
            if ok:
                dl_snapshot.dl_rebuild_from_overrides(c)
        return jsonify(ok=True) if ok else (jsonify(error="nenájdené"), 404)

    @app.get("/api/znalosti/dl-suppliers")
    def api_znalosti_dl_suppliers():
        q = _fold((request.args.get("q") or "").strip())
        with deps.db() as c:
            rows = dl_snapshot.dl_suppliers_for_management(c)
        if q:
            rows = [r for r in rows if q in _fold(r["name"]) or q in _fold(r["ean_edi"])]
        rows.sort(key=lambda r: _fold(r["name"]))
        return jsonify(items=rows[:50])

    @app.post("/api/znalosti/dl-suppliers")
    def api_znalosti_dl_suppliers_upsert():
        body = request.get_json(silent=True) or {}
        name = str(body.get("name") or "").strip()
        if not name:
            return jsonify(error="chýba názov"), 400
        # #235: the same EAN-cannot-be-forgotten guarantee #234 gave customers, reusing
        # the SAME `_EAN_STRIP_RE` constant (not a second copy) — an early, precise 400
        # before ever reaching the DB layer. `dl_snapshot.upsert_dl_supplier` ALSO
        # enforces this unconditionally (defense in depth, any future caller).
        ean = _EAN_STRIP_RE.sub("", str(body.get("ean_edi") or ""))
        if not ean:
            return jsonify(error="Bez EAN kódu EDI sa dodávateľ nedá uložiť — nájdeš ho "
                                 "v CODEXe pri dodávateľovi."), 400
        if not ean.isdigit():
            return jsonify(error="EAN kód EDI musí byť len číslice."), 400
        try:
            with deps.db() as c:
                rid = dl_snapshot.upsert_dl_supplier(
                    c, override_id=body.get("override_id"),
                    orig_ean_edi=body.get("orig_ean_edi"), orig_city=body.get("orig_city"),
                    ean_edi=ean, name=name,
                    emails=_parse_emails_field(body.get("emails")),
                    city=str(body.get("city") or "").strip())
                dl_snapshot.dl_rebuild_from_overrides(c)
        except snapshot.DuplicateEan as e:
            # #248 review finding: mirrors the customer endpoint's own fix above — this
            # admin dashboard save funnels through the SAME `upsert_dl_supplier` as the
            # warehouse question-card flow.
            return jsonify(
                error=f"EAN {ean} už má dodávateľ {e.existing.get('name', '')}.",
                existing=e.existing), 409
        return jsonify(ok=True, id=rid)

    @app.delete("/api/znalosti/dl-suppliers")
    def api_znalosti_dl_suppliers_retire():
        body = request.get_json(silent=True) or {}
        with deps.db() as c:
            ok = dl_snapshot.retire_dl_supplier(
                c, override_id=body.get("override_id"),
                orig_ean_edi=body.get("orig_ean_edi"), orig_city=body.get("orig_city"))
            if ok:
                dl_snapshot.dl_rebuild_from_overrides(c)
        return jsonify(ok=True) if ok else (jsonify(error="nenájdené"), 404)
