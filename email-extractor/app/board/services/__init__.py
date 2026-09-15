"""Board service layer (#442). Logic + SQL for the nástenka; NO Flask objects here.

Routes (the `board` blueprint) parse input and call these; services own `db.connect()`
access and every query. This package is deliberately import-light at module top (only its
own leaf submodules + stdlib/psycopg) so `orders.teach` can import `services.audit` lazily
without any risk of an import cycle through `app.board`.
"""
