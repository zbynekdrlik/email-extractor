"""Internal HTTP API + human review UI.

- /health, /version
- /files/<mid>/<idx>, /eml/<mid>            (originals for n8n AI-Vision / forwarding)
- /review                                   (human review web page)
- /review/list, /review/detail, /review/correct, /review/confirm, /review/processed

Human actions:
- confirm  -> review_status='confirmed', category unchanged (this one is right)
- correct  -> review_status='corrected', category := new, original_category kept,
              processed reset so the terminal workflow re-handles it
Both set human_reviewed=true. Confirmed + corrected = labelled set to score/tune the classifier.
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

    @app.get("/review/list")
    def review_list():
        _auth()
        cat = request.args.get("category", "")
        proc = request.args.get("processed", "")
        rev = request.args.get("reviewed", "")   # '', 'no', 'confirmed', 'corrected'
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
        if rev == "no":
            where.append("review_status IS NULL")
        elif rev in ("confirmed", "corrected"):
            where.append("review_status = %s")
            params.append(rev)
        if q:
            where.append("(from_addr ILIKE %s OR subject ILIKE %s)")
            params += [f"%{q}%", f"%{q}%"]
        wsql = ("WHERE " + " AND ".join(where)) if where else ""
        with _db() as c:
            counts = dict(c.execute(
                "SELECT COALESCE(category,'(none)'), count(*) FROM messages GROUP BY category").fetchall())
            grand = c.execute("SELECT count(*) FROM messages").fetchone()[0]
            reviewed = c.execute("SELECT count(*) FROM messages WHERE review_status IS NOT NULL").fetchone()[0]
            total = c.execute(f"SELECT count(*) FROM messages {wsql}", params).fetchone()[0]
            rows = c.execute(
                f"""SELECT id, sent_at, from_addr, subject, category, original_category,
                           review_status, processed, has_attachments
                    FROM messages {wsql}
                    ORDER BY id DESC LIMIT 50 OFFSET %s""", params + [offset]).fetchall()
        items = [{
            "id": r[0], "sent_at": r[1], "from": r[2], "subject": r[3],
            "category": r[4], "original_category": r[5], "review_status": r[6],
            "processed": r[7], "has_attachments": r[8],
        } for r in rows]
        return jsonify(total=total, offset=offset, counts=counts, grand=grand,
                       reviewed=reviewed, categories=CATEGORIES, items=items)

    @app.get("/review/detail")
    def review_detail():
        _auth()
        try:
            mid = int(request.args.get("id"))
        except (TypeError, ValueError):
            abort(400)
        with _db() as c:
            m = c.execute(
                """SELECT id, message_id, from_addr, from_name, to_addrs, cc_addrs,
                          subject, sent_at, body_text, combined_text, category,
                          original_category, needs_vision, processed, review_status
                   FROM messages WHERE id = %s""", (mid,)).fetchone()
            if not m:
                abort(404)
            atts = c.execute(
                """SELECT idx, filename, mime, size, method, ocr_conf, pages,
                          needs_vision, flag, left(extracted_text, 6000)
                   FROM attachments WHERE message_id = %s ORDER BY idx""", (m[1],)).fetchall()
        return jsonify(
            id=m[0], message_id=m[1], from_addr=m[2], from_name=m[3], to_addrs=m[4],
            cc_addrs=m[5], subject=m[6], sent_at=m[7], body_text=m[8], combined_text=m[9],
            category=m[10], original_category=m[11], needs_vision=m[12], processed=m[13],
            review_status=m[14], categories=CATEGORIES,
            attachments=[{
                "idx": a[0], "filename": a[1], "mime": a[2], "size": a[3], "method": a[4],
                "ocr_conf": a[5], "pages": a[6], "needs_vision": a[7], "flag": a[8],
                "extracted_text": a[9],
            } for a in atts])

    @app.post("/review/confirm")
    def review_confirm():
        _auth()
        body = request.get_json(force=True, silent=True) or {}
        mid = body.get("id")
        if not isinstance(mid, int):
            abort(400)
        with _db() as c:
            c.execute(
                "UPDATE messages SET human_reviewed = true, review_status = 'confirmed', corrected_at = now() WHERE id = %s",
                (mid,))
        return jsonify(ok=True, id=mid, review_status="confirmed")

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
                       category = %s, human_reviewed = true, review_status = 'corrected',
                       corrected_at = now(), processed = false, processed_at = NULL, processed_by = NULL
                   WHERE id = %s""", (cat, mid))
        return jsonify(ok=True, id=mid, category=cat, review_status="corrected")

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
                (by, mid))
        return jsonify(ok=True, id=mid)

    @app.get("/review")
    def review_page():
        return REVIEW_HTML

    return app


REVIEW_HTML = r"""<!doctype html><html><head><meta charset="utf-8">
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
 th,td{padding:7px 10px;border-bottom:1px solid #eaeef2;text-align:left;vertical-align:middle;font-size:13px}
 th{background:#f6f8fa;position:sticky;top:44px}
 tr.confirmed{background:#e6ffec}tr.corrected{background:#fff8c5}
 tr.row:hover{background:#f0f6ff}
 .muted{color:#57606a;font-size:12px}
 .subj{max-width:380px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;cursor:pointer}
 .badge{font-size:11px;padding:1px 6px;border-radius:10px}
 .p1{background:#1a7f37;color:#fff}.p0{background:#d0d7de}.nv{background:#bf3989;color:#fff;margin-left:4px}
 .catsel{min-width:150px}
 .ok{background:#1a7f37;color:#fff;border-color:#1a7f37;font-weight:600}
 .stbadge{font-size:11px;padding:1px 6px;border-radius:10px}.sc{background:#1a7f37;color:#fff}.sx{background:#9a6700;color:#fff}
 #ov{display:none;position:fixed;inset:0;background:rgba(0,0,0,.45);z-index:20}
 #modal{background:#fff;max-width:900px;margin:24px auto;border-radius:10px;max-height:90vh;overflow:auto}
 #modal .mh{position:sticky;top:0;background:#24292f;color:#fff;padding:12px 16px;display:flex;justify-content:space-between;align-items:center}
 #modal .mb{padding:16px}#modal h3{margin:14px 0 6px;font-size:14px}
 #modal pre{background:#f6f8fa;border:1px solid #eaeef2;border-radius:6px;padding:10px;white-space:pre-wrap;word-break:break-word;max-height:320px;overflow:auto;font-size:12px}
 .kv{font-size:13px;margin:2px 0}.kv b{display:inline-block;min-width:70px;color:#57606a}
 .att{border:1px solid #d0d7de;border-radius:8px;padding:10px;margin:8px 0}
 .x{cursor:pointer;font-size:20px;background:none;border:none;color:#fff}
 a.btn{display:inline-block;text-decoration:none;background:#0969da;color:#fff;padding:4px 8px;border-radius:6px;font-size:12px;margin-right:6px}
</style></head><body>
<header><b>Email triedenie — ľudská kontrola</b> &nbsp;<span id="stats" class="muted"></span></header>
<div class="wrap">
 <div class="bar">
   <label>Kategória: <select id="fcat"><option value="">— všetky —</option></select></label>
   <label>Kontrola: <select id="frev"><option value="">— všetky —</option><option value="no">neskontrolované</option><option value="confirmed">potvrdené</option><option value="corrected">opravené</option></select></label>
   <label>Stav: <select id="fproc"><option value="">— všetky —</option><option value="false">nespracované</option><option value="true">spracované</option></select></label>
   <input id="fq" placeholder="hľadať odosielateľ/predmet" size="20">
   <button onclick="load(0)">Filtrovať</button>
   <span id="pager"></span>
 </div>
 <div class="chips" id="chips"></div>
 <table><thead><tr><th>kontrola</th><th>id</th><th>od</th><th>predmet (klik = detail)</th><th>kategória</th><th>spr.</th></tr></thead>
 <tbody id="rows"></tbody></table>
</div>
<div id="ov" onclick="if(event.target.id=='ov')closeM()"><div id="modal">
  <div class="mh"><span id="mtitle">Detail</span><button class="x" onclick="closeM()">×</button></div>
  <div class="mb" id="mbody"></div>
</div></div>
<script>
const token=new URLSearchParams(location.search).get('token')||'';
const H={'Content-Type':'application/json','X-Token':token};
const CATS=["ai_orders","invoices","reklamacie","dodacie_listy","static_orders","human_processing","no_processing"];
let offset=0;
function esc(s){return (s||'').toString().replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]))}
function catSelect(id,cur,big){return '<select class=catsel onchange="correct('+id+',this.value,this)"'+(big?' style="font-size:14px"':'')+'>'+CATS.map(c=>'<option'+(c===cur?' selected':'')+'>'+c+'</option>').join('')+'</select>'}
function stbadge(s){return s==='confirmed'?'<span class="stbadge sc">✓</span>':s==='corrected'?'<span class="stbadge sx">✎</span>':''}
async function load(off){
  offset=off||0;
  const p=new URLSearchParams({category:fcat.value,reviewed:frev.value,processed:fproc.value,q:fq.value,offset});
  const r=await fetch('/review/list?'+p+'&token='+encodeURIComponent(token));
  if(!r.ok){rows.innerHTML='<tr><td colspan=6>chyba '+r.status+' (token?)</td></tr>';return}
  const d=await r.json();
  if(fcat.options.length<=1){CATS.forEach(c=>{const o=document.createElement('option');o.value=o.textContent=c;fcat.appendChild(o)})}
  stats.textContent='skontrolované '+d.reviewed+' / '+d.grand+'  ·  výber: '+d.total;
  chips.innerHTML=Object.entries(d.counts).map(([k,v])=>'<span>'+esc(k)+': '+v+'</span>').join('');
  pager.innerHTML=(offset>0?'<button onclick="load('+(offset-50)+')">‹</button> ':'')+'<span class=muted>'+(offset+1)+'–'+(offset+d.items.length)+'</span>'+(d.items.length===50?' <button onclick="load('+(offset+50)+')">›</button>':'');
  rows.innerHTML=d.items.map(it=>'<tr class="row'+(it.review_status==='confirmed'?' confirmed':it.review_status==='corrected'?' corrected':'')+'" id=r'+it.id+'>'+
    '<td><button class="ok'+(it.review_status==='confirmed'?'':'')+'" onclick="confirm('+it.id+',this)">✓ OK</button></td>'+
    '<td onclick="detail('+it.id+')">'+it.id+' '+stbadge(it.review_status)+'</td>'+
    '<td onclick="detail('+it.id+')">'+esc(it.from)+'</td>'+
    '<td class=subj onclick="detail('+it.id+')">'+(it.has_attachments?'📎 ':'')+esc(it.subject)+'</td>'+
    '<td>'+catSelect(it.id,it.category)+(it.original_category&&it.original_category!==it.category?' <span class=muted>(pôv. '+esc(it.original_category)+')</span>':'')+'</td>'+
    '<td><span class="badge '+(it.processed?'p1':'p0')+'">'+(it.processed?'OK':'—')+'</span></td></tr>').join('');
}
async function confirm(id,el){
  el.disabled=true;
  const r=await fetch('/review/confirm?token='+encodeURIComponent(token),{method:'POST',headers:H,body:JSON.stringify({id})});
  el.disabled=false;
  if(r.ok){const tr=document.getElementById('r'+id);if(tr){tr.className='row confirmed';if(frev.value==='no'){tr.remove()}}}
  else alert('chyba '+r.status);
}
async function correct(id,category,el){
  el.disabled=true;
  const r=await fetch('/review/correct?token='+encodeURIComponent(token),{method:'POST',headers:H,body:JSON.stringify({id,category})});
  el.disabled=false;
  if(r.ok){const tr=document.getElementById('r'+id);if(tr){tr.className='row corrected';const b=tr.querySelector('.badge');if(b){b.className='badge p0';b.textContent='—';}if(frev.value==='no'){tr.remove()}}}
  else alert('chyba '+r.status);
}
async function detail(id){
  mbody.innerHTML='načítavam…';ov.style.display='block';
  const r=await fetch('/review/detail?id='+id+'&token='+encodeURIComponent(token));
  if(!r.ok){mbody.innerHTML='chyba '+r.status;return}
  const d=await r.json();
  mtitle.textContent='#'+d.id+' — '+(d.subject||'(bez predmetu)');
  const fb='/files/'+encodeURIComponent(d.message_id);
  const atts=(d.attachments||[]).map(a=>'<div class=att><div><b>'+esc(a.filename)+'</b> <span class=muted>'+esc(a.mime)+' · '+Math.round((a.size||0)/1024)+' KB · '+esc(a.method)+(a.ocr_conf!=null?' · OCR '+a.ocr_conf+'%':'')+'</span>'+(a.needs_vision?' <span class="badge nv">AI VISION</span>':'')+'</div>'+
    '<div style="margin:6px 0"><a class=btn target=_blank href="'+fb+'/'+a.idx+'?token='+encodeURIComponent(token)+'">Otvoriť súbor</a></div>'+
    '<pre>'+esc(a.extracted_text||'(žiadny text)')+'</pre></div>').join('')||'<div class=muted>žiadne prílohy</div>';
  mbody.innerHTML=
    '<div style="margin-bottom:8px"><button class=ok onclick="confirm('+d.id+',this);closeM()">✓ Správne</button> &nbsp; '+catSelect(d.id,d.category,true)+(d.original_category&&d.original_category!==d.category?' <span class=muted>(pôv. '+esc(d.original_category)+')</span>':'')+'</div>'+
    '<div class=kv><b>Od:</b> '+esc(d.from_name)+' &lt;'+esc(d.from_addr)+'&gt;</div>'+
    '<div class=kv><b>Komu:</b> '+esc((d.to_addrs||[]).join(', '))+'</div>'+
    ((d.cc_addrs||[]).length?'<div class=kv><b>Kópia:</b> '+esc(d.cc_addrs.join(', '))+'</div>':'')+
    '<div class=kv><b>Dátum:</b> '+esc(d.sent_at)+' &nbsp; <a class=btn target=_blank href="/eml/'+encodeURIComponent(d.message_id)+'?token='+encodeURIComponent(token)+'">Originál .eml</a></div>'+
    '<h3>Telo</h3><pre>'+esc(d.body_text||'(prázdne)')+'</pre>'+
    '<h3>Prílohy ('+(d.attachments||[]).length+')</h3>'+atts+
    '<h3>combined_text (čo videla AI)</h3><pre>'+esc(d.combined_text)+'</pre>';
}
function closeM(){ov.style.display='none'}
document.addEventListener('keydown',e=>{if(e.key==='Escape')closeM()});
load(0);
</script></body></html>"""


def start(cfg) -> None:
    app = create_app(cfg)
    threading.Thread(
        target=lambda: app.run(host="0.0.0.0", port=cfg.http_port, threaded=True),
        daemon=True,
    ).start()
