"""Internal HTTP API + human review UI.

- /health, /version
- /files/<mid>/<idx>, /eml/<mid>  (originals for n8n AI-Vision / forwarding)
- /review                          (human review web page)
- /review/list, /review/correct   (review API: list classified mails, correct a category)
- /review/processed               (terminal workflows mark a mail processed; optional — n8n can also UPDATE directly)

A human correction sets category := corrected, keeps original_category, marks
human_reviewed, and resets processed=false so the terminal workflow re-handles it.
"""
from __future__ import annotations

import threading
from pathlib import Path

import psycopg
from flask import Flask, abort, jsonify, request, send_file

from . import __version__
from .store import safe_id

CATEGORIES = ["ai_orders", "invoices", "reklamacie", "dodacie_listy",
              "static_orders", "human_processing", "no_processing"]


def create_app(cfg) -> Flask:
    app = Flask(__name__)
    data_dir = Path(cfg.data_dir)

    def _auth():
        if cfg.api_token:
            tok = request.args.get("token") or request.headers.get("X-Token")
            if tok != cfg.api_token:
                abort(403)

    def _db():
        return psycopg.connect(cfg.pg_dsn, autocommit=True)

    # ---- health / files ----
    @app.get("/health")
    def health():
        return jsonify(ok=True, version=__version__)

    @app.get("/version")
    def version():
        return __version__

    @app.get("/files/<mid>/<int:idx>")
    def get_file(mid: str, idx: int):
        _auth()
        matches = sorted((data_dir / safe_id(mid)).glob(f"att{idx}__*"))
        if not matches:
            abort(404)
        return send_file(matches[0])

    @app.get("/eml/<mid>")
    def get_eml(mid: str):
        _auth()
        path = data_dir / safe_id(mid) / "raw.eml"
        if not path.exists():
            abort(404)
        return send_file(path, mimetype="message/rfc822")

    # ---- review API ----
    @app.get("/review/list")
    def review_list():
        _auth()
        cat = request.args.get("category", "")
        proc = request.args.get("processed", "")
        q = (request.args.get("q", "") or "").strip()
        try:
            offset = max(0, int(request.args.get("offset", 0)))
        except ValueError:
            offset = 0
        where, params = [], []
        if cat:
            where.append("category = %s")
            params.append(cat)
        if proc in ("true", "false"):
            where.append("processed = %s")
            params.append(proc == "true")
        if q:
            where.append("(from_addr ILIKE %s OR subject ILIKE %s)")
            params += [f"%{q}%", f"%{q}%"]
        wsql = ("WHERE " + " AND ".join(where)) if where else ""
        with _db() as c:
            counts = dict(c.execute(
                "SELECT COALESCE(category,'(none)'), count(*) FROM messages GROUP BY category"
            ).fetchall())
            total = c.execute(f"SELECT count(*) FROM messages {wsql}", params).fetchone()[0]
            rows = c.execute(
                f"""SELECT id, sent_at, from_addr, subject, category, original_category,
                           human_reviewed, processed, left(combined_text, 400)
                    FROM messages {wsql}
                    ORDER BY id DESC LIMIT 50 OFFSET %s""",
                params + [offset],
            ).fetchall()
        items = [{
            "id": r[0], "sent_at": r[1], "from": r[2], "subject": r[3],
            "category": r[4], "original_category": r[5], "human_reviewed": r[6],
            "processed": r[7], "snippet": r[8],
        } for r in rows]
        return jsonify(total=total, offset=offset, counts=counts,
                       categories=CATEGORIES, items=items)

    @app.post("/review/correct")
    def review_correct():
        _auth()
        body = request.get_json(force=True, silent=True) or {}
        mid, cat = body.get("id"), body.get("category")
        if not isinstance(mid, int) or cat not in CATEGORIES:
            abort(400)
        with _db() as c:
            c.execute(
                """UPDATE messages
                   SET original_category = COALESCE(original_category, category),
                       category = %s, human_reviewed = true, corrected_at = now(),
                       processed = false, processed_at = NULL, processed_by = NULL
                   WHERE id = %s""",
                (cat, mid),
            )
        return jsonify(ok=True, id=mid, category=cat)

    @app.post("/review/processed")
    def review_processed():
        _auth()
        body = request.get_json(force=True, silent=True) or {}
        mid = body.get("id")
        by = body.get("by") or "workflow"
        if not isinstance(mid, int):
            abort(400)
        with _db() as c:
            c.execute(
                "UPDATE messages SET processed = true, processed_at = now(), processed_by = %s WHERE id = %s",
                (by, mid),
            )
        return jsonify(ok=True, id=mid)

    @app.get("/review")
    def review_page():
        return REVIEW_HTML

    return app


REVIEW_HTML = """<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Email triedenie — kontrola</title>
<style>
 body{font:14px/1.4 -apple-system,Segoe UI,Roboto,sans-serif;margin:0;background:#f6f8fa;color:#1f2328}
 header{background:#24292f;color:#fff;padding:10px 16px;position:sticky;top:0;z-index:5}
 .wrap{padding:14px 16px}
 .bar{display:flex;gap:8px;flex-wrap:wrap;align-items:center;margin-bottom:12px}
 select,input,button{font:inherit;padding:6px 8px;border:1px solid #d0d7de;border-radius:6px;background:#fff}
 button{cursor:pointer}
 .chips span{display:inline-block;background:#eaeef2;border-radius:12px;padding:2px 8px;margin:2px;font-size:12px}
 table{border-collapse:collapse;width:100%;background:#fff;border:1px solid #d0d7de;border-radius:8px;overflow:hidden}
 th,td{padding:8px 10px;border-bottom:1px solid #eaeef2;text-align:left;vertical-align:top;font-size:13px}
 th{background:#f6f8fa;position:sticky;top:44px}
 tr.corrected{background:#fff8c5}
 .muted{color:#57606a;font-size:12px}
 .snip{color:#57606a;font-size:12px;max-width:420px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
 .badge{font-size:11px;padding:1px 6px;border-radius:10px}
 .p1{background:#1a7f37;color:#fff}.p0{background:#d0d7de}
 .catsel{min-width:140px}
</style></head><body>
<header><b>Email triedenie — ľudská kontrola</b> &nbsp;<span id="stats" class="muted"></span></header>
<div class="wrap">
 <div class="bar">
   <label>Kategória:
     <select id="fcat"><option value="">— všetky —</option></select></label>
   <label>Stav:
     <select id="fproc"><option value="">— všetky —</option><option value="false">nespracované</option><option value="true">spracované</option></select></label>
   <input id="fq" placeholder="hľadať odosielateľ/predmet" size="24">
   <button onclick="load(0)">Filtrovať</button>
   <span id="pager"></span>
 </div>
 <div class="chips" id="chips"></div>
 <table><thead><tr><th>id</th><th>dátum</th><th>od</th><th>predmet</th><th>kategória (oprav)</th><th>stav</th><th>náhľad</th></tr></thead>
 <tbody id="rows"></tbody></table>
</div>
<script>
const token = new URLSearchParams(location.search).get('token') || '';
const H = {'Content-Type':'application/json','X-Token':token};
const CATS = ["ai_orders","invoices","reklamacie","dodacie_listy","static_orders","human_processing","no_processing"];
let offset = 0;
function esc(s){return (s||'').replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]))}
async function load(off){
  offset = off||0;
  const p = new URLSearchParams({category:document.getElementById('fcat').value, processed:document.getElementById('fproc').value, q:document.getElementById('fq').value, offset});
  const r = await fetch('/review/list?'+p+'&token='+encodeURIComponent(token));
  if(!r.ok){document.getElementById('rows').innerHTML='<tr><td colspan=7>chyba '+r.status+' (token?)</td></tr>';return}
  const d = await r.json();
  // category filter options
  const fcat=document.getElementById('fcat'); if(fcat.options.length<=1){CATS.forEach(c=>{const o=document.createElement('option');o.value=o.textContent=c;fcat.appendChild(o)})}
  document.getElementById('stats').textContent = 'spolu '+d.total;
  document.getElementById('chips').innerHTML = Object.entries(d.counts).map(([k,v])=>'<span>'+esc(k)+': '+v+'</span>').join('');
  document.getElementById('pager').innerHTML = (offset>0?'<button onclick=\"load('+(offset-50)+')\">‹</button> ':'')+'<span class=muted>'+(offset+1)+'–'+(offset+d.items.length)+'</span>'+(d.items.length===50?' <button onclick=\"load('+(offset+50)+')\">›</button>':'');
  document.getElementById('rows').innerHTML = d.items.map(it=>{
    const sel = '<select class=catsel onchange=\"correct('+it.id+',this.value,this)\">'+CATS.map(c=>'<option'+(c===it.category?' selected':'')+'>'+c+'</option>').join('')+'</select>'+(it.original_category&&it.original_category!==it.category?' <span class=muted>(pôv. '+esc(it.original_category)+')</span>':'');
    return '<tr id=r'+it.id+(it.human_reviewed?' class=corrected':'')+'><td>'+it.id+'</td><td class=muted>'+esc((it.sent_at||'').slice(0,16))+'</td><td>'+esc(it.from)+'</td><td>'+esc(it.subject)+'</td><td>'+sel+'</td><td><span class="badge '+(it.processed?'p1':'p0')+'">'+(it.processed?'spracované':'nespr.')+'</span></td><td class=snip title=\"'+esc(it.snippet)+'\">'+esc(it.snippet)+'</td></tr>';
  }).join('');
}
async function correct(id,category,el){
  el.disabled=true;
  const r = await fetch('/review/correct?token='+encodeURIComponent(token),{method:'POST',headers:H,body:JSON.stringify({id,category})});
  el.disabled=false;
  if(r.ok){const tr=document.getElementById('r'+id);tr.classList.add('corrected');tr.querySelector('.badge').className='badge p0';tr.querySelector('.badge').textContent='nespr.';}
  else alert('chyba '+r.status);
}
load(0);
</script></body></html>"""


def start(cfg) -> None:
    app = create_app(cfg)
    threading.Thread(
        target=lambda: app.run(host="0.0.0.0", port=cfg.http_port, threaded=True),
        daemon=True,
    ).start()
