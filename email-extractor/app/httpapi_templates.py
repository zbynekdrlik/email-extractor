"""Dashboard/login page templates as raw HTML/CSS/JS strings (#268 krok 4).

Moved VERBATIM out of `app/httpapi.py` (no behavior change) — see the design comment on
#268 for exactly what moved and why. Pure string constants, no Flask, no DB, no other
`app.*` import — a leaf module, importable stand-alone. `httpapi.py`'s `login` and
`dashboard` routes render these constants by importing them back.

#449 lane 8: the three warehouse pages (`ASK_HTML`/`ASK_DL_HTML`/`ZNALOSTI_HTML` +
the shared `_ASK_HTML_TEMPLATE`) were RETIRED — the unified nástenka (`app/board/`,
epic #441) now owns every one of those surfaces, and `/otazky`/`/otazky-dl`/`/znalosti`
are pure redirects to it. Only `LOGIN_HTML` + `DASH_HTML` remain here.
"""
from __future__ import annotations

LOGIN_HTML = r"""<!doctype html><html lang="sk"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>Prihlásenie</title>
<style>
 body{font:15px/1.5 -apple-system,Segoe UI,Roboto,sans-serif;background:#0d1117;color:#e6edf3;
      display:flex;align-items:center;justify-content:center;height:100vh;margin:0}
 form{background:#161b22;border:1px solid #30363d;border-radius:12px;padding:28px 26px;width:300px}
 h1{font-size:17px;margin:0 0 16px}
 input{width:100%;box-sizing:border-box;padding:9px 11px;border:1px solid #30363d;border-radius:7px;
       background:#0d1117;color:#e6edf3;font:inherit;margin-bottom:12px}
 button{width:100%;padding:9px;border:0;border-radius:7px;background:#1f6feb;color:#fff;font:inherit;
        font-weight:600;cursor:pointer}
 .err{background:#3d1418;border:1px solid #cf222e;color:#ffb3ba;border-radius:7px;padding:7px 10px;
      margin-bottom:12px;font-size:13px}
</style></head><body>
<form method="post" action="/login">
  <h1>📬 Email dashboard</h1>
  <!--ERR-->
  <input type="password" name="password" placeholder="heslo" autofocus autocomplete="current-password">
  <button type="submit">Prihlásiť sa</button>
</form></body></html>"""


DASH_HTML = r"""<!doctype html><html lang="sk"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Email dashboard</title>
<style>
 *{box-sizing:border-box}
 body{font:13px/1.45 -apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif;margin:0;
      background:#f6f8fa;color:#1f2328;height:100vh;display:flex;flex-direction:column;overflow:hidden}
 a{color:#0969da}
 header{background:#0d1117;color:#fff;padding:8px 14px;display:flex;align-items:center;gap:10px;flex-wrap:wrap}
 header b{font-size:14px;white-space:nowrap}
 header input,header select{font:inherit;padding:5px 8px;border:1px solid #30363d;border-radius:6px;
      background:#161b22;color:#e6edf3}
 #q{min-width:220px;flex:1}
 .live{display:flex;align-items:center;gap:5px;font-size:12px;color:#3fb950;cursor:pointer;white-space:nowrap}
 .ver{color:#6e7681;font-size:11px;white-space:nowrap}
 .chips{display:flex;gap:6px;padding:7px 14px;background:#fff;border-bottom:1px solid #d0d7de;flex-wrap:wrap}
 .chip{border:0;border-radius:11px;padding:3px 10px;font:inherit;font-size:11px;cursor:pointer}
 .chip.active{outline:2px solid #0969da}
 .c-total{background:#ddf4ff;color:#0969da}.c-done{background:#dafbe1;color:#1a7f37}
 .c-review{background:#fff8c5;color:#7d4e00}.c-error{background:#ffebe9;color:#cf222e}
 .c-processing{background:#eaeef2;color:#57606a}.c-onfix{background:#ffe3f1;color:#bf3989}
 .tabs{display:flex;gap:4px;padding:6px 14px 0;background:#fff;border-bottom:1px solid #d0d7de}
 .tab{border:1px solid #d0d7de;border-bottom:0;border-radius:7px 7px 0 0;background:#f6f8fa;
      padding:5px 12px;cursor:pointer;font:inherit}
 .tab.active{background:#fff;font-weight:600}
 main{flex:1;display:flex;min-height:0}
 #list{width:42%;max-width:560px;border-right:1px solid #d0d7de;overflow:auto;background:#fff}
 .row{padding:7px 11px;border-bottom:1px solid #eaeef2;border-left:3px solid transparent;cursor:pointer}
 .row:hover{background:#f0f6ff}.row.sel{background:#eef4ff;border-left-color:#1f6feb}
 .row.s-done{border-left-color:#1a7f37}.row.s-review{border-left-color:#7d4e00}
 .row.s-error{border-left-color:#cf222e}.row.s-processing{border-left-color:#57606a}
 .row .t{display:flex;justify-content:space-between;gap:8px}
 .row .f{font-weight:600}.row .when{color:#57606a;font-size:11px;white-space:nowrap}
 .row .sub{color:#1f2328;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
 .pill{border-radius:9px;padding:1px 7px;font-size:11px;background:#ddf4ff;color:#0969da}
 .out{font-size:11px}.ok{color:#1a7f37}.rev{color:#7d4e00}.err{color:#cf222e}
 #detail{flex:1;overflow:auto;padding:14px 16px}
 .muted{color:#57606a}.lbl{font-size:11px;color:#57606a;text-transform:uppercase;letter-spacing:.04em;margin:14px 0 6px}
 .badge{border-radius:11px;padding:2px 9px;font-size:11px}
 .b-ok{background:#dafbe1;color:#1a7f37}.b-review{background:#fff8c5;color:#7d4e00}
 .b-error{background:#ffebe9;color:#cf222e}.b-none{background:#eaeef2;color:#57606a}
 .tl{border-left:2px solid #d0d7de;padding-left:13px;margin-left:4px}
 .tl .ev{margin-bottom:9px;position:relative}
 .tl .dot{position:absolute;left:-18px;top:2px;width:9px;height:9px;border-radius:50%;background:#57606a}
 .tl .d-ok{background:#1a7f37}.tl .d-review{background:#7d4e00}.tl .d-error{background:#cf222e}
 .att{background:#fff;border:1px solid #d0d7de;border-radius:7px;padding:6px 9px;margin:5px 0;font-size:12px}
 pre{background:#f6f8fa;border:1px solid #eaeef2;border-radius:6px;padding:9px;white-space:pre-wrap;
     word-break:break-word;max-height:280px;overflow:auto;font-size:12px;margin:0}
 .actions{display:flex;gap:7px;flex-wrap:wrap;margin:14px 0;align-items:center}
 button,select.act{font:inherit;padding:6px 11px;border:1px solid #d0d7de;border-radius:6px;background:#fff;cursor:pointer}
 .btn-blue{background:#0969da;color:#fff;border-color:#0969da;font-weight:600}
 .btn-red{background:#cf222e;color:#fff;border-color:#cf222e;font-weight:600}
 .fixrow{background:#fff;border:1px solid #d0d7de;border-radius:8px;padding:9px 11px;margin:8px 14px}
 .fixrow.resolved{opacity:.6}
 #ov{display:none;position:fixed;inset:0;background:rgba(0,0,0,.45);z-index:30;align-items:center;justify-content:center}
 #modal{background:#fff;border-radius:10px;width:440px;max-width:92vw;padding:16px}
 #modal h3{margin:0 0 10px}#modal label{display:block;margin:8px 0 3px;font-size:12px;color:#57606a}
 #modal select,#modal textarea{width:100%;font:inherit;padding:7px;border:1px solid #d0d7de;border-radius:6px}
 .empty{color:#57606a;padding:30px;text-align:center}
</style></head><body>
<header>
  <b>📬 Email dashboard</b>
  <input id="q" placeholder="hľadať: odosielateľ, predmet, telo, príloha…">
  <select id="fcat"><option value="">kategória</option></select>
  <select id="fstate"><option value="">stav</option>
    <option value="done">hotové</option><option value="review">review</option>
    <option value="error">chyba</option><option value="processing">spracúva</option>
    <option value="onfix">na oprave</option></select>
  <input id="ffrom" type="date" title="od">
  <input id="fto" type="date" title="do">
  <span class="live" id="livetog">● <span id="livelbl">LIVE</span></span>
  <span class="ver" data-testid="version">v__VERSION__</span>
  <span class="ver" id="spendBadge" data-testid="spend" title="náklady objednávkového automatu za tento mesiac"></span>
  <span class="ver" id="reliabilityBadge" data-testid="reliability" title="spoľahlivosť AI objednávok — dní od posledného potvrdeného incidentu, včerajší prehľad rozhodnutí"></span>
  <a class="ver" href="/logout">odhlásiť</a>
</header>
<div class="chips" id="chips"></div>
<div class="tabs">
  <button class="tab active" id="tabMails" onclick="setView('mails')">Maily</button>
  <button class="tab" id="tabFix" onclick="setView('fix')">Fix fronta</button>
  <button class="tab" id="tabImap" onclick="setView('imap')">Neprijaté <span id="imapBadge"></span></button>
  <button class="tab" id="tabAsk" onclick="setView('ask')">Otázky skladu <span id="askBadge"></span></button>
  <button class="tab" id="tabDiscarded" onclick="setView('discarded')">Zahodené AI <span id="discardedBadge"></span></button>
</div>
<main>
  <div id="list"></div>
  <div id="detail"><div class="empty">Vyber mail vľavo.</div></div>
</main>
<div id="ov" onclick="if(event.target.id=='ov')closeModal()"><div id="modal"></div></div>
<script>
const E=s=>(s==null?'':String(s)).replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
let CATS=[],sel=null,view='mails',timer=null,live=true,counts={};
async function api(path,opts){const r=await fetch(path,Object.assign({headers:{'Content-Type':'application/json'}},opts));
  if(r.status===401){location.href='/login';throw new Error('auth')}
  if(!r.ok){let m='';try{m=(await r.json()).error||''}catch(e){}
    throw new Error(m||('chyba '+r.status))}
  return r.json()}
function tsShort(s){if(!s)return '';return s.replace('T',' ').slice(5,16)}
function params(){const p=new URLSearchParams();
  if(q.value.trim())p.set('q',q.value.trim());
  if(fcat.value)p.set('category',fcat.value);
  if(fstate.value)p.set('state',fstate.value);
  if(ffrom.value)p.set('from',ffrom.value);
  if(fto.value)p.set('to',fto.value);
  return p}
async function loadList(){
  let d;try{d=await api('/api/messages?'+params())}catch(e){return}
  if(!CATS.length){CATS=d.categories;for(const c of CATS){const o=document.createElement('option');o.value=o.textContent=c;fcat.appendChild(o)}}
  counts=d.counts;renderChips();
  const L=document.getElementById('list');
  if(view!=='mails')return;
  if(!d.items.length){L.innerHTML='<div class="empty">Žiadne maily pre tento filter.</div>';return}
  L.innerHTML=d.items.map(it=>{
    const isRev=it.proc_status==='review'||it.proc_status==='partial';
    const st=it.processed?'done':(isRev?'review':it.proc_status==='error'?'error':it.processing?'processing':'');
    const out=it.on_fix?'<span class="out" style="color:#bf3989">🔧 na oprave</span>':
      (it.proc_outcome?'<span class="out '+(it.proc_status==='error'?'err':isRev?'rev':'ok')+'">'+E(it.proc_outcome)+'</span>':'');
    return '<div class="row s-'+st+(sel===it.id?' sel':'')+'" onclick="openDetail('+it.id+')">'+
      '<div class="t"><span class="f">#'+it.id+' '+E(it.from||'')+'</span><span class="when">'+tsShort(it.last_event_at||it.created_at)+'</span></div>'+
      '<div class="sub">'+(it.has_attachments?'📎 ':'')+E(it.subject||'(bez predmetu)')+'</div>'+
      '<div><span class="pill">'+E(it.category||'—')+'</span> '+out+'</div></div>'}).join('')}
function renderChips(){const c=counts;const C=document.getElementById('chips');
  const def=[['','c-total','spolu',c.total],['done','c-done','✓ hotové',c.done],['review','c-review','⚠ review',c.review],
    ['error','c-error','✗ chyba',c.error],['processing','c-processing','… spracúva',c.processing],['onfix','c-onfix','🔧 na oprave',c.on_fix]];
  C.innerHTML=def.map(([v,cl,lbl,n])=>'<button class="chip '+cl+(fstate.value===v?' active':'')+'" onclick="setState(\''+v+'\')">'+lbl+' '+(n||0)+'</button>').join('')}
function setState(v){fstate.value=v;loadList()}
async function openDetail(id){
  sel=id;document.querySelectorAll('.row').forEach(r=>r.classList.toggle('sel',r.getAttribute('onclick').includes('('+id+')')));
  const D=document.getElementById('detail');D.innerHTML='<div class="empty">načítavam…</div>';
  let m;try{m=await api('/api/message/'+id)}catch(e){D.innerHTML='<div class="empty">chyba</div>';return}
  const badge=m.proc_status?('<span class="badge b-'+(m.proc_status==='ok'?'ok':(m.proc_status==='review'||m.proc_status==='partial')?'review':m.proc_status==='error'?'error':'none')+'">'+E(m.proc_status)+'</span>'):
    (m.processed?'<span class="badge b-ok">hotové</span>':'<span class="badge b-none">nové</span>');
  const fb='/files/'+encodeURIComponent(m.message_id);
  const evs=(m.events||[]).map(e=>'<div class="ev"><span class="dot d-'+(e.status==='ok'?'ok':e.status==='review'?'review':e.status==='error'?'error':'')+'"></span>'+
    '<b>'+E(e.stage)+'</b> <span class="muted">'+tsShort(e.ts)+(e.workflow?' · '+E(e.workflow):'')+'</span>'+(e.outcome?'<br>'+E(e.outcome):'')+'</div>').join('')
    ||'<div class="muted">žiadne udalosti zatiaľ</div>';
  const atts=(m.attachments||[]).map(a=>'<div class="att"><b>'+E(a.filename)+'</b> <span class="muted">'+E(a.mime)+' · '+Math.round((a.size||0)/1024)+' KB · '+E(a.method||'')+(a.ocr_conf!=null?' · OCR '+a.ocr_conf+'%':'')+'</span>'+
    (a.needs_vision?' <span class="pill" style="background:#ffe3f1;color:#bf3989">VISION</span>':'')+
    ' <a target=_blank href="'+fb+'/'+a.idx+'">otvoriť</a></div>').join('')||'<div class="muted">žiadne prílohy</div>';
  const fixes=(m.fixes||[]).filter(f=>f.status==='open'||f.status==='in_progress').map(f=>'<div class="att" style="border-color:#bf3989">🔧 <b>'+E(f.problem_type)+'</b>'+(f.expected_category?' → '+E(f.expected_category):'')+(f.description?' — '+E(f.description):'')+' <span class="muted">('+E(f.status)+')</span></div>').join('');
  const opts=CATS.map(c=>'<option'+(c===m.category?' selected':'')+'>'+c+'</option>').join('');
  D.innerHTML='<div class="t" style="display:flex;justify-content:space-between;align-items:flex-start">'+
      '<div><b style="font-size:15px">#'+m.id+' — '+E(m.subject||'(bez predmetu)')+'</b>'+
      '<div class="muted">'+E(m.from_name||'')+' &lt;'+E(m.from_addr||'')+'&gt; · '+E(m.sent_at||'')+'</div></div>'+badge+'</div>'+
    '<div class="actions">'+
      '<label class="muted">kategória: <select class="act" onchange="doReclassify('+m.id+',this.value)">'+opts+'</select></label>'+
      '<button onclick="doReprocess('+m.id+')">⟳ spustiť znova</button>'+
      '<a class="ver" style="color:#0969da" target=_blank href="/eml/'+encodeURIComponent(m.message_id)+'">📄 .eml</a>'+
      '<button class="btn-red" onclick="openFix('+m.id+')">🔧 dať na opravu</button></div>'+
    (fixes?'<div>'+fixes+'</div>':'')+
    '<div class="lbl">Časová os spracovania</div><div class="tl">'+evs+'</div>'+
    '<div class="lbl">Prílohy ('+(m.attachments||[]).length+')</div>'+atts+
    '<div class="lbl">Telo</div><pre>'+E(m.body_text||'(prázdne)')+'</pre>'+
    '<div class="lbl">combined_text (čo videla AI)</div><pre>'+E(m.combined_text||'')+'</pre>'}
async function doReclassify(id,cat){try{await api('/api/message/'+id+'/reclassify',{method:'POST',body:JSON.stringify({category:cat})});await loadList();await openDetail(id)}catch(e){alert(e.message||'chyba')}}
async function doReprocess(id){try{await api('/api/message/'+id+'/reprocess',{method:'POST'});await loadList();await openDetail(id)}catch(e){alert(e.message||'chyba')}}
function openFix(id){
  const opts=CATS.map(c=>'<option value="'+c+'">'+c+'</option>').join('');
  document.getElementById('modal').innerHTML='<h3>🔧 Dať na opravu — #'+id+'</h3>'+
    '<label>Čo je zle?</label><select id="fxtype" onchange="document.getElementById(\'fxcatwrap\').style.display=this.value===\'mis_sorted\'?\'block\':\'none\'">'+
      '<option value="mis_processed">zle spracované</option><option value="mis_sorted">zle zaradené (sortnuté)</option><option value="other">iné</option></select>'+
    '<div id="fxcatwrap" style="display:none"><label>Správna kategória</label><select id="fxcat">'+opts+'</select></div>'+
    '<label>Poznámka pre Clauda</label><textarea id="fxdesc" rows="3" placeholder="čo presne je zle / aké by malo byť správne"></textarea>'+
    '<div class="actions"><button class="btn-red" onclick="submitFix('+id+')">Odoslať na opravu</button><button onclick="closeModal()">zrušiť</button></div>';
  document.getElementById('ov').style.display='flex'}
async function submitFix(id){
  const t=document.getElementById('fxtype').value;
  const body={problem_type:t,description:document.getElementById('fxdesc').value};
  if(t==='mis_sorted')body.expected_category=document.getElementById('fxcat').value;
  try{await api('/api/message/'+id+'/fix',{method:'POST',body:JSON.stringify(body)});closeModal();await loadList();await openDetail(id)}catch(e){alert('chyba')}}
function closeModal(){document.getElementById('ov').style.display='none'}
async function loadFix(){const D=document.getElementById('detail'),L=document.getElementById('list');
  L.innerHTML='';let d;try{d=await api('/api/fix-queue')}catch(e){return}
  if(!d.items.length){D.innerHTML='<div class="empty">Fix fronta je prázdna 🎉</div>';return}
  D.innerHTML='<div class="lbl">Fix fronta ('+d.total+')</div>'+d.items.map(f=>{
    const open=f.status==='open'||f.status==='in_progress';
    return '<div class="fixrow'+(open?'':' resolved')+'">'+
      '<div class="t" style="display:flex;justify-content:space-between"><b>🔧 #'+f.id+' — '+E(f.problem_type)+(f.expected_category?' → '+E(f.expected_category):'')+'</b><span class="muted">'+E(f.status)+'</span></div>'+
      '<div class="muted">mail #'+(f.msg_id||'?')+' · '+E(f.from||'')+' · '+E(f.subject||'')+'</div>'+
      (f.description?'<div>'+E(f.description)+'</div>':'')+
      (f.resolution?'<div class="ok">→ '+E(f.resolution)+'</div>':'')+
      (open?'<div class="actions"><button onclick="openDetail('+(f.msg_id||'null')+');setView(\'mails\')">otvoriť mail</button>'+
        '<button class="btn-blue" onclick="resolveFix('+f.id+',\'fixed\')">označiť opravené</button>'+
        '<button onclick="resolveFix('+f.id+',\'wontfix\')">neopravím</button></div>':'')+'</div>'}).join('')}
async function loadImap(){const D=document.getElementById('detail'),L=document.getElementById('list');
  L.innerHTML='';let d;try{d=await api('/api/imap-failures')}catch(e){return}
  const b=document.getElementById('imapBadge');
  b.textContent=d.total?String(d.total):'';b.style.color='#f85149';
  if(!d.items.length){D.innerHTML='<div class="empty">Všetky maily sa podarilo prijať 🎉</div>';return}
  D.innerHTML='<div class="lbl">Maily, ktoré sa nepodarilo prijať ('+d.pending+' sa ešte skúša, '+d.skipped+' vzdané)</div>'+
    d.items.map(f=>'<div class="fixrow'+(f.skipped?'':' resolved')+'">'+
      '<div class="t" style="display:flex;justify-content:space-between"><b>'+(f.skipped?'⛔ vzdané':'🔄 skúša sa')+
      ' — '+E(f.folder)+' UID '+f.uid+'</b><span class="muted">'+f.attempts+'/'+d.max_attempts+' pokusov</span></div>'+
      '<div class="muted">prvýkrát '+tsShort(f.first_seen)+' · naposledy '+tsShort(f.last_seen)+'</div>'+
      '<div class="err">'+E(f.last_error||'')+'</div>'+
      (f.skipped?'<div class="muted">Tento mail v systéme NIE JE. Treba ho vytiahnuť ručne z mailu (schránka, UID '+f.uid+') alebo opraviť príčinu a znížiť watermark.</div>':'')+
      '</div>').join('')}
async function resolveFix(fid,status){const res=status==='fixed'?(prompt('Poznámka k oprave (voliteľné):')||''):'';
  try{await api('/api/fix/'+fid+'/resolve',{method:'POST',body:JSON.stringify({status,resolution:res})});await loadFix()}catch(e){alert('chyba')}}
function setView(v){view=v;document.getElementById('tabMails').classList.toggle('active',v==='mails');
  document.getElementById('tabFix').classList.toggle('active',v==='fix');
  document.getElementById('tabImap').classList.toggle('active',v==='imap');
  document.getElementById('tabAsk').classList.toggle('active',v==='ask');
  document.getElementById('tabDiscarded').classList.toggle('active',v==='discarded');
  if(v==='fix'){loadFix()}else if(v==='imap'){loadImap()}
  else if(v==='ask'){showSkladLink();loadAsk()}
  else if(v==='discarded'){loadDiscarded()}
  else{document.getElementById('detail').innerHTML='<div class="empty">Vyber mail vľavo.</div>';loadList()}}
function tick(){if(live&&document.getElementById('ov').style.display!=='flex'){
  if(view==='mails')loadList();else if(view==='imap')loadImap();
  else if(view==='ask')loadAsk();else if(view==='discarded')loadDiscarded();else loadFix()}}
async function loadDiscarded(){const D=document.getElementById('detail'),L=document.getElementById('list');
  L.innerHTML='';let d;try{d=await api('/api/orders/discarded')}catch(e){return}
  const b=document.getElementById('discardedBadge');b.textContent=d.total?String(d.total):'';b.style.color='#6e7681';
  if(!d.items.length){D.innerHTML='<div class="empty">AI zatiaľ nič nezahodila (14 dní).</div>';return}
  D.innerHTML='<div class="lbl">Zahodené AI (14 dní): '+d.total+'</div>'+
    '<table style="width:100%;border-collapse:collapse;font-size:12px"><thead><tr>'+
    '<th style="text-align:left;padding:5px;border-bottom:1px solid #d0d7de">Kedy</th>'+
    '<th style="text-align:left;padding:5px;border-bottom:1px solid #d0d7de">Odosielateľ</th>'+
    '<th style="text-align:left;padding:5px;border-bottom:1px solid #d0d7de">Predmet</th>'+
    '<th style="text-align:left;padding:5px;border-bottom:1px solid #d0d7de">Dôvod</th>'+
    '<th style="padding:5px;border-bottom:1px solid #d0d7de"></th></tr></thead><tbody>'+
    d.items.map(it=>'<tr>'+
      '<td style="padding:5px;border-bottom:1px solid #eaeef2;white-space:nowrap">'+tsShort(it.discarded_at)+'</td>'+
      '<td style="padding:5px;border-bottom:1px solid #eaeef2">'+E(it.from||'')+'</td>'+
      '<td style="padding:5px;border-bottom:1px solid #eaeef2">'+E(it.subject||'(bez predmetu)')+'</td>'+
      '<td style="padding:5px;border-bottom:1px solid #eaeef2">'+E(it.reason||'')+'</td>'+
      '<td style="padding:5px;border-bottom:1px solid #eaeef2"><button onclick="doRestore('+it.id+')">Nie je to na zahodenie → daj na nástenku</button></td>'+
      '</tr>').join('')+'</tbody></table>'}
async function doRestore(id){try{await api('/api/message/'+id+'/restore',{method:'POST'});await loadDiscarded()}catch(e){alert(e.message||'chyba')}}
const SKLAD_LINK="__SKLADLINK__";
const DL_SKLAD_LINK="__DLSKLADLINK__";
function skladLinkRow(label,url){const w=document.createElement('div');w.className='row';
  const h=document.createElement('div');h.className='sub';h.textContent=label;
  const a=document.createElement('a');a.href=url;a.textContent=url;
  a.target='_blank';a.rel='noopener';a.style.wordBreak='break-all';
  w.appendChild(h);w.appendChild(a);return w}
function showSkladLink(){const D=document.getElementById('detail');D.textContent='';
  D.appendChild(skladLinkRow(
    'Odkaz pre predaj (objednávky) — otvorí sa bez hesla, dá sa dať do Odoo aj do záložiek:',
    SKLAD_LINK));
  D.appendChild(skladLinkRow(
    'Odkaz pre sklad (dodacie listy) — samostatná nástenka, len dodacie listy:',
    DL_SKLAD_LINK))}
let askRender=0;
async function loadAsk(){const L=document.getElementById('list');
  // Every render gets a number. A fetch that comes back after a newer render started must not
  // append to it, or the list doubles (seen live on 0.9.7).
  const mine=++askRender;
  L.innerHTML='';let d;try{d=await api('/api/orders/questions')}catch(e){return}
  if(mine!==askRender)return;
  if(!d.items.length){const e0=document.createElement('div');e0.className='empty';
    e0.textContent='Nič nečaká \u2014 automat si vie poradiť sám.';L.appendChild(e0);
    await loadHeld(mine);return loadTaught(mine)}   // nothing waiting is the NORMAL state: the undo must still be here
  for(const q of d.items){const el=document.createElement('div');el.className='row';
    const head=document.createElement('div');const b=document.createElement('b');
    // #159: a 'customer' question asks WHO placed the order, not WHICH card a wording is
    if(q.kind==='customer'){const ctx=q.context||{};
      b.textContent='Nezn\u00e1my z\u00e1kazn\u00edk: '+(ctx.sender_email||q.wording);head.appendChild(b);
      const who=document.createElement('div');who.className='sub';
      who.textContent=[ctx.sender_name,ctx.company_name,ctx.delivery_address_guess]
        .filter(Boolean).join(' \u00b7 ')+' \u00b7 dodanie '+(q.delivery_date||'?');
      const why=document.createElement('div');why.className='sub';why.textContent=q.reason||'';
      const acts=document.createElement('div');acts.className='acts';
      for(const c of (q.candidates||[])){const bt=document.createElement('button');bt.className='btn';
        const addr=[c.street,c.city].filter(Boolean).join(', ');
        bt.textContent=(c.name||c.ean_edi)+(addr?' ('+addr+')':'')+(c.address_match?' \u2713':'');
        bt.onclick=()=>answerCustomerIt(q.id,c.ean_edi,c.name||'');acts.appendChild(bt)}
      const ub=document.createElement('button');ub.className='btn';ub.textContent='Neviem, kto to je';
      ub.onclick=()=>answerCustomerIt(q.id,'','',true);acts.appendChild(ub);
      // #369: the third escape — not an order at all, teach a mail_rules ignore rule.
      const nob=document.createElement('button');nob.className='btn';
      nob.textContent='Nie je to objednávka — takéto maily ignoruj';
      nob.onclick=()=>answerCustomerNotOrderIt(q.id);acts.appendChild(nob);
      head.appendChild(who);head.appendChild(why);head.appendChild(acts);
      el.appendChild(head);L.appendChild(el);continue}
    // #164/#202: ONE generic renderer for every OTHER new kind (mail/date/line, and DL's
    // own dl_item/dl_supplier) — the candidates carry their own {value,label}; a universal
    // "Neviem" escape posts {"choice":"unknown"} through the same dispatch endpoint (stays
    // open, never silent).
    if(q.kind==='mail'||q.kind==='date'||q.kind==='line'||q.kind==='dl_item'||q.kind==='dl_supplier'){
      const titles={mail:'Je to vôbec objednávka?',date:'Ktorý deň dodávky platí?',
        line:'Platí ešte táto položka?',dl_item:'Ktorá karta je táto DL položka?',
        dl_supplier:'Ktorý dodávateľ?'};
      b.textContent=titles[q.kind]||q.kind;head.appendChild(b);
      const who=document.createElement('div');who.className='sub';who.textContent=q.wording||'';
      const why=document.createElement('div');why.className='sub';why.textContent=q.reason||'';
      const acts=document.createElement('div');acts.className='acts';
      for(const c of (q.candidates||[])){const bt=document.createElement('button');bt.className='btn';
        bt.textContent=c.label||c.value;bt.onclick=()=>answerGenericIt(q.id,c.value);acts.appendChild(bt)}
      const ub=document.createElement('button');ub.className='btn';ub.textContent='Neviem';
      ub.onclick=()=>answerGenericIt(q.id,'unknown');acts.appendChild(ub);
      // #365: dl_item can also be answered "nemá kartu — pošli bez tejto položky" (ships the
      // doc WITHOUT this line). Confirm first — it sends an incomplete document to ORION.
      if(q.kind==='dl_item'){const sw=document.createElement('button');sw.className='btn';
        sw.textContent='Nemá kartu — pošli bez';
        sw.onclick=()=>{if(confirm('Naozaj poslať dodací list BEZ tejto položky? Doklad '
          +'odíde do ORIONu neúplný.'))answerGenericIt(q.id,'ship_without')};acts.appendChild(sw)}
      head.appendChild(who);head.appendChild(why);head.appendChild(acts);
      el.appendChild(head);L.appendChild(el);continue}
    b.textContent=q.wording;head.appendChild(b);
    head.appendChild(document.createTextNode(' \u00b7 '+(q.quantity||'')+' '+(q.unit||'')));
    const who=document.createElement('div');who.className='sub';
    who.textContent=(q.customer_name||q.customer_ean)+' \u00b7 dodanie '+(q.delivery_date||'?');
    const why=document.createElement('div');why.className='sub';why.textContent=q.reason||'';
    // #360: množstvo + cena/MJ for this line, prefilled + editable. teachIt() reads them on
    // answer — the confirmed quantity ships, the price is a verification value only (no ORION
    // price field).
    const flds=document.createElement('div');flds.className='sub';
    flds.appendChild(document.createTextNode('množstvo: '));
    const qi=document.createElement('input');qi.id='oqty_'+q.id;qi.type='text';qi.inputMode='decimal';
    qi.style.width='70px';qi.value=(q.quantity!=null?q.quantity:'');flds.appendChild(qi);
    flds.appendChild(document.createTextNode('    cena/MJ: '));
    const pi=document.createElement('input');pi.id='oprice_'+q.id;pi.type='text';pi.inputMode='decimal';
    pi.style.width='80px';pi.placeholder='€';pi.value=(q.unit_price!=null?q.unit_price:'');
    flds.appendChild(pi);
    flds.appendChild(document.createTextNode('  (cena sa neposiela do ORIONu — len kontrola)'));
    const acts=document.createElement('div');acts.className='acts';
    for(const c of q.candidates){const bt=document.createElement('button');bt.className='btn';
      bt.textContent=c.name||c.gtin;            // textContent: a name may contain quotes
      bt.onclick=()=>teachIt(q.id,c.gtin,c.name||'');acts.appendChild(bt)}
    head.appendChild(who);head.appendChild(why);head.appendChild(flds);head.appendChild(acts);
    el.appendChild(head);L.appendChild(el)}
  await loadHeld(mine);loadTaught(mine)}
async function loadHeld(token){const L=document.getElementById('list');let d;
  // #93: orders waiting on an answer, so nothing waits invisibly \u2014 each one names its
  // own delivery date, the deadline this project promises it will ship by regardless.
  try{d=await api('/api/orders/held')}catch(e){return}
  if(token!==askRender||!d.items.length)return;
  const h=document.createElement('div');h.className='sub';h.style.padding='8px 10px';
  h.textContent='Objednávky čakajúce na odpoveď \u2014 odošlú sa po odpovedi, najneskôr v deň dodania:';
  L.appendChild(h);
  for(const o of d.items){const el=document.createElement('div');el.className='row';
    const head=document.createElement('div');const b=document.createElement('b');
    b.textContent=o.customer_name||o.customer_ean||'(neznámy zákazník)';head.appendChild(b);
    const who=document.createElement('div');who.className='sub';
    who.textContent='dodanie '+(o.delivery_date||'?')+(o.order_number?' \u00b7 obj. '+o.order_number:'')
      +' \u00b7 '+o.question_ids.length+' \u00d7 otázka';
    head.appendChild(who);el.appendChild(head);L.appendChild(el)}}
async function loadTaught(token){const L=document.getElementById('list');let d;
  try{d=await api('/api/orders/taught')}catch(e){return}
  if(token!==askRender)return;              // a newer render owns the list now
  if(!d.items.length)return;
  const h=document.createElement('div');h.className='sub';h.style.padding='8px 10px';
  h.textContent='Naposledy naučené \u2014 keby bol klik omylom, dá sa vrátiť:';L.appendChild(h);
  for(const t of d.items){const el=document.createElement('div');el.className='row';
    const w=document.createElement('div');const b=document.createElement('b');
    b.textContent=t.wording;w.appendChild(b);
    w.appendChild(document.createTextNode(' \u2192 '+(t.answer_card==='not_order'?'nie je objedn\u00e1vka':(t.answer_card||t.answer_gtin))));
    const who=document.createElement('div');who.className='sub';
    who.textContent=(t.customer_name||t.customer_ean);
    const acts=document.createElement('div');acts.className='acts';
    const bt=document.createElement('button');bt.className='btn';bt.textContent='vrátiť';
    bt.onclick=()=>undoIt(t.id);acts.appendChild(bt);
    w.appendChild(who);w.appendChild(acts);el.appendChild(w);L.appendChild(el)}}
async function undoIt(qid){try{await api('/api/orders/question/'+qid+'/undo',{method:'POST'});
  await loadAsk();await askBadgeRefresh()}catch(e){alert(e.message||'chyba')}}
async function teachIt(qid,gtin,card){try{
  const body={gtin:gtin,card:card};
  const qi=document.getElementById('oqty_'+qid),pi=document.getElementById('oprice_'+qid);
  if(qi)body.quantity=qi.value;if(pi)body.unit_price=pi.value;   // #360: confirmed qty+price
  await api('/api/orders/question/'+qid+'/answer',
    {method:'POST',body:JSON.stringify(body)});await loadAsk();await askBadgeRefresh()}
  catch(e){alert(e.message||'chyba')}}
async function answerCustomerIt(qid,ean_edi,name,unknown){try{await api('/api/orders/question/'+qid+'/answer',
  {method:'POST',body:JSON.stringify(unknown?{unknown:true}:{ean_edi:ean_edi,name:name})});
  await loadAsk();await askBadgeRefresh()}catch(e){alert(e.message||'chyba')}}
async function answerCustomerNotOrderIt(qid){try{await api('/api/orders/question/'+qid+'/answer',
  {method:'POST',body:JSON.stringify({not_order:true})});
  await loadAsk();await askBadgeRefresh()}catch(e){alert(e.message||'chyba')}}
async function answerGenericIt(qid,choice){try{await api('/api/orders/question/'+qid+'/answer',
  {method:'POST',body:JSON.stringify({choice:choice})});await loadAsk();await askBadgeRefresh()}
  catch(e){alert(e.message||'chyba')}}
async function askBadgeRefresh(){try{const d=await api('/api/orders/questions');
  const b=document.getElementById('askBadge');b.textContent=d.items.length?String(d.items.length):'';
  b.style.color='#d29922'}catch(e){}}
async function spendBadgeRefresh(){try{const d=await api('/api/orders/spend');
  const b=document.getElementById('spendBadge');
  b.textContent=d.cost_eur.toFixed(2)+' \u20ac / '+d.cap_eur.toFixed(0)+' \u20ac \u00b7 bez modelu '+d.free_pct+' %';
  b.style.color=(d.cap_eur&&d.cost_eur>d.cap_eur)?'#f85149':'#6e7681'}catch(e){}}
async function imapBadgeRefresh(){try{const d=await api('/api/imap-failures');
  const b=document.getElementById('imapBadge');b.textContent=d.total?String(d.total):'';b.style.color='#f85149'}catch(e){}}
async function discardedBadgeRefresh(){try{const d=await api('/api/orders/discarded');
  const b=document.getElementById('discardedBadge');b.textContent=d.total?String(d.total):'';b.style.color='#6e7681'}catch(e){}}
async function reliabilityBadgeRefresh(){try{const d=await api('/api/orders/digest');
  const b=document.getElementById('reliabilityBadge');
  const since=d.days_since_incident;
  const y=d.yesterday||{};
  const sinceTxt=(since==null)?'bez záznamu incidentu':(since+' '+(since===1?'deň':(since>=2&&since<=4?'dni':'dní'))+' bez incidentu');
  b.textContent=sinceTxt+(y.items?(' · včera '+y.deterministic+'/'+y.llm+'/'+y.review+' (isté/AI/kontrola)'):'');
  b.style.color=(since!=null&&since<3)?'#f85149':'#6e7681'}catch(e){}}
document.getElementById('livetog').onclick=()=>{live=!live;document.getElementById('livetog').style.color=live?'#3fb950':'#6e7681';document.getElementById('livelbl').textContent=live?'LIVE':'pauza'};
let deb;q.oninput=()=>{clearTimeout(deb);deb=setTimeout(loadList,350)};
for(const el of [fcat,fstate,ffrom,fto])el.onchange=loadList;
loadList();imapBadgeRefresh();spendBadgeRefresh();askBadgeRefresh();reliabilityBadgeRefresh();discardedBadgeRefresh();setInterval(askBadgeRefresh,30000);timer=setInterval(tick,5000);setInterval(imapBadgeRefresh,30000);setInterval(spendBadgeRefresh,60000);setInterval(reliabilityBadgeRefresh,60000);setInterval(discardedBadgeRefresh,30000);
</script></body></html>"""
