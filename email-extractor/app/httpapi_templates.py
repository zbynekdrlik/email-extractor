"""Dashboard/nástenka page templates as raw HTML/CSS/JS strings (#268 krok 4).

Moved VERBATIM out of `app/httpapi.py` lines 1376-2593 (no behavior change) — see the
design comment on #268 for exactly what moved and why. Pure string constants, no Flask,
no DB, no other `app.*` import — a leaf module, importable stand-alone. `ASK_HTML` /
`ASK_DL_HTML` are built here from the shared `_ASK_HTML_TEMPLATE` via `.replace()`,
exactly as before the move; `httpapi.py`'s own routes (`login`, `dashboard`,
`otazky`/`otazky_dl`, `/znalosti`) render these constants by importing them back, so the
route bodies stay byte-identical to what they returned before this split.
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


# The warehouse's own page: reachable from the signed /sklad/<key> link with NO password,
# so it fetches ONLY the two question endpoints — nothing here may reach a mail, a file or
# the spend. Phone-sized buttons: it is answered from the floor, not from a desk.
# #231: ONE shared template for BOTH unauthenticated question boards (the orders-only
# `/otazky` and the NEW delivery-notes-only `/otazky-dl`) — they need the same rendering
# for every generic-kind card (`genericQuestionCard`, `dl_item`/`dl_supplier` included
# since #202) and the same "naposledy naučené" history, and the server already sends
# each role only its OWN kinds (`_role_kinds` in httpapi.py) — so the two pages differ
# only in title/heading and an optional "stavy" (states) strip the DL board asks for
# (ticket #231: "review fronta, história, stavy"). Building both from ONE literal string
# via `.replace()` keeps them from silently drifting apart the way two hand-maintained
# ~150-line copies inevitably would.
_ASK_HTML_TEMPLATE = r"""<!doctype html><html lang="sk"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>__TITLE__</title>
<style>
 *{box-sizing:border-box}
 body{font:15px/1.5 -apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif;margin:0;
      background:#f6f8fa;color:#1f2328}
 header{background:#161b22;color:#e6edf3;padding:12px 16px;display:flex;justify-content:space-between;
        align-items:center;position:sticky;top:0}
 h1{font-size:16px;margin:0}
 .ver{font-size:12px;color:#8b949e}
 .dl-alert-banner{background:#fff3cd;border-bottom:2px solid #d4a72c;color:#5c4813;
   padding:10px 14px;font-size:14px;font-weight:600;position:sticky;top:44px}
 .dl-alert-banner div{margin:3px 0}
 main{padding:14px 12px;max-width:760px;margin:0 auto}
 .q{background:#fff;border:1px solid #d0d7de;border-radius:12px;padding:14px;margin-bottom:14px}
 .who{font-size:13px;color:#57606a}
 .w{font-size:18px;font-weight:700;margin:4px 0 2px}
 .why{font-size:13px;color:#57606a;margin-bottom:10px}
 button{display:block;width:100%;text-align:left;padding:12px 14px;margin-top:8px;font:inherit;
        border:1px solid #1f6feb;border-radius:10px;background:#ddf4ff;color:#0969da;cursor:pointer}
 .t{background:#fff;border:1px solid #d0d7de;border-radius:10px;padding:10px 12px;margin-bottom:8px;
    display:flex;justify-content:space-between;align-items:center;gap:10px;font-size:14px}
 .t button{width:auto;margin:0;border-color:#d0d7de;background:#f6f8fa;color:#57606a;padding:7px 12px}
 h2{font-size:14px;color:#57606a;margin:22px 0 8px}
 .empty{color:#57606a;padding:14px 2px}
 .kb{display:block;font-size:13px;color:#57606a;margin-top:8px;text-decoration:none}
 .kb:hover{text-decoration:underline}
 .slabel{font-size:12px;color:#57606a;margin-top:12px}
 .search{width:100%;box-sizing:border-box;padding:9px 10px;margin-top:6px;border:1px solid #d0d7de;
         border-radius:8px;font:inherit}
 .sres-wrap{margin-top:4px}
 .sres{padding:10px 12px;border:1px solid #d0d7de;border-radius:8px;margin-top:4px;cursor:pointer;
       font-size:14px;background:#fff}
 .sres:hover{background:#ddf4ff}
 .sres.none{cursor:default;color:#57606a;background:transparent;border-style:dashed}
 .sres.none:hover{background:transparent}
 input{width:100%;padding:9px 10px;margin-top:6px;border:1px solid #d0d7de;border-radius:8px;font:inherit}
</style></head><body>
<header><h1>__HEADING__</h1><span class="ver" data-testid="version">v__VERSION__</span>__STATS_HEADER__</header>
__ALERT_BANNER__
<main id="wrap"><div class="empty">Nahrávam&hellip;</div></main>
<script>
async function api(u,o){const r=await fetch(u,Object.assign({headers:{'Content-Type':'application/json'}},o||{}));
  if(!r.ok){const b=await r.json().catch(()=>({}));const e=new Error(b.error||('HTTP '+r.status));e.body=b;throw e}
  return r.json()}
let render=0;
// #149: what the warehouse has typed into each open question's catalog search, keyed by
// question id — the list auto-refreshes every 5s (see setInterval below), and without this
// the whole card gets rebuilt from scratch mid-typing and wipes what was just typed.
const searchState={};
function el(t,cls,txt){const e=document.createElement(t);if(cls)e.className=cls;
  if(txt!==undefined)e.textContent=txt;return e}
function searchBox(q){
  const wrap=el('div');
  const inp=el('input','search');inp.placeholder='hľadaj v celom katalógu (názov karty)…';
  inp.value=searchState[q.id]||'';
  const res=el('div','sres-wrap');
  wrap.appendChild(inp);wrap.appendChild(res);
  // Same stale-response guard as load()'s `mine=++render`: a slower response for an
  // earlier keystroke must not overwrite a faster response for a later one.
  let seq=0;
  async function run(v){
    const mine=++seq;
    if(v.length<2){res.textContent='';return}
    let d;try{d=await api('/api/znalosti/catalog?q='+encodeURIComponent(v))}catch(e){return}
    if(mine!==seq)return;      // a later keystroke's response already landed — drop this one
    res.textContent='';
    if(!d.items.length){res.appendChild(el('div','sres none','žiadna zhoda'));return}
    for(const it of d.items){const b=el('div','sres',it.name+'  ('+it.gtin+')');
      b.onclick=()=>teach(q.id,it.gtin,it.name);res.appendChild(b)}
  }
  let t=null;
  inp.oninput=()=>{searchState[q.id]=inp.value;clearTimeout(t);
    t=setTimeout(()=>run(inp.value.trim()),200)};
  if(inp.value.trim().length>=2)run(inp.value.trim());
  return wrap;
}
// #360: množstvo + cena/MJ for THIS product line — prefilled from the extracted values,
// editable. When the sklad answers (a candidate button OR the search box), teach() reads
// these two inputs and sends them with the answer: the confirmed QUANTITY is what ships,
// the price is a verification value only (the ORION ORDER_ EDI has no price field).
function lineFields(q){
  const box=el('div');box.style.margin='6px 0';
  box.appendChild(el('span',null,'množstvo: '));
  const qi=el('input');qi.id='oqty_'+q.id;qi.type='text';qi.inputMode='decimal';
  qi.style.width='70px';qi.value=(q.quantity!=null?q.quantity:'');box.appendChild(qi);
  box.appendChild(el('span',null,'    cena/MJ: '));
  const pi=el('input');pi.id='oprice_'+q.id;pi.type='text';pi.inputMode='decimal';
  pi.style.width='80px';pi.placeholder='€';pi.value=(q.unit_price!=null?q.unit_price:'');
  box.appendChild(pi);
  const note=el('div',null,'cena sa neposiela do ORIONu — len kontrola');
  note.style.fontSize='11px';note.style.color='#57606a';box.appendChild(note);
  return box}
// #164/#202: ONE generic card for every kind BEYOND item/customer (mail/date/line, and
// DL's own dl_item/dl_supplier) — each candidate button posts {"choice": <value>} through
// the SAME dispatch endpoint, plus a universal "Neviem" escape that posts
// {"choice":"unknown"} (stays open, never silent).
const GENERIC_TITLE={mail:'Je to vôbec objednávka?',date:'Ktorý deň dodávky platí?',
  line:'Platí ešte táto položka?',dl_item:'Ktorá karta je táto DL položka?',
  dl_supplier:'Ktorý dodávateľ?'};
function genericQuestionCard(q){
  const c=el('div','q');
  c.appendChild(el('div','who',GENERIC_TITLE[q.kind]||q.kind));
  c.appendChild(el('div','w',q.wording||''));
  c.appendChild(el('div','why',q.reason||''));
  for(const opt of (q.candidates||[])){
    const b=el('button',null,opt.label||opt.value);
    b.onclick=()=>answerGeneric(q.id,opt.value);c.appendChild(b)}
  const nb=el('button',null,'Neviem');
  nb.style.borderColor='#d0d7de';nb.style.background='#f6f8fa';nb.style.color='#57606a';
  nb.onclick=()=>answerGeneric(q.id,'unknown');c.appendChild(nb);
  return c}
async function answerGeneric(qid,choice){try{await api('/api/orders/question/'+qid+'/answer',
  {method:'POST',body:JSON.stringify({choice:choice})});await load()}
  catch(e){alert(e.message||'chyba')}}
// #307: "netýka sa skladu" — terminal, message-level. Confirm first (it closes the whole
// mail and never sends it to ORION), then POST {not_warehouse:true}.
async function answerNotWarehouse(qid){
  if(!confirm('Označiť, že tento mail sa netýka skladu? Otázka sa zavrie, dodací list sa '
    +'NEPOŠLE do ORIONu.'))return;
  try{await api('/api/orders/question/'+qid+'/answer',
    {method:'POST',body:JSON.stringify({not_warehouse:true})});await load()}
  catch(e){alert(e.message||'chyba')}}
// #235: dl_supplier/dl_item get their OWN card (mirrors #234's customerQuestionCard
// below) — a live search over the CURRENT DL suppliers/catalog (not just the frozen
// candidates the question was asked with), plus a collapsed "this is genuinely new"
// form. mail/date/line stay on the plain genericQuestionCard above, unchanged.
function dlSupplierSearchBox(q){
  const wrap=el('div');
  const inp=el('input','search');inp.placeholder='hľadaj dodávateľa (názov alebo EAN)…';
  const res=el('div','sres-wrap');
  wrap.appendChild(inp);wrap.appendChild(res);
  let seq=0;
  async function run(v){
    const mine=++seq;
    if(v.length<2){res.textContent='';return}
    let d;try{d=await api('/api/znalosti/dl-suppliers?q='+encodeURIComponent(v))}catch(e){return}
    if(mine!==seq)return;
    res.textContent='';
    const hits=d.items.filter(it=>it.ean_edi);
    if(!hits.length){res.appendChild(el('div','sres none','žiadna zhoda'));return}
    for(const it of hits){
      const b=el('div','sres',it.name+(it.city?'  ('+it.city+')':'')+'  ('+it.ean_edi+')');
      b.onclick=()=>answerGeneric(q.id,it.ean_edi);res.appendChild(b)}
  }
  let t=null;
  inp.oninput=()=>{clearTimeout(t);t=setTimeout(()=>run(inp.value.trim()),200)};
  return wrap}
function dlItemSearchBox(q){
  const wrap=el('div');
  const inp=el('input','search');inp.placeholder='hľadaj v DL katalógu (názov karty)…';
  const res=el('div','sres-wrap');
  wrap.appendChild(inp);wrap.appendChild(res);
  let seq=0;
  async function run(v){
    const mine=++seq;
    if(v.length<2){res.textContent='';return}
    let d;try{d=await api('/api/znalosti/dl-products?q='+encodeURIComponent(v))}catch(e){return}
    if(mine!==seq)return;
    res.textContent='';
    if(!d.items.length){res.appendChild(el('div','sres none','žiadna zhoda'));return}
    for(const it of d.items){
      const b=el('div','sres',it.name+'  ('+it.gtin+')');
      b.onclick=()=>answerGeneric(q.id,it.gtin);res.appendChild(b)}
  }
  let t=null;
  inp.oninput=()=>{clearTimeout(t);t=setTimeout(()=>run(inp.value.trim()),200)};
  return wrap}
function newDlSupplierForm(q){
  const ctx=q.payload||q.context||{};
  const box=el('div');box.style.marginTop='12px';
  const toggle=el('button',null,'➕ Nový dodávateľ (najprv EAN kód na karte v Codexe)');
  toggle.style.borderColor='#d0d7de';toggle.style.background='#f6f8fa';toggle.style.color='#57606a';
  const form=el('div');form.style.display='none';
  const ean=el('input');ean.placeholder='EAN kód EDI *';
  const name=el('input');name.placeholder='názov firmy *';
  const emails=el('input');emails.placeholder='e-maily';emails.value=ctx.sender_email||'';
  const city=el('input');city.placeholder='obec';
  for(const i of [ean,name,emails,city])form.appendChild(i);
  const status=el('div','slabel','');form.appendChild(status);
  const extra=el('div');form.appendChild(extra);
  const save=el('button',null,'Uložiť nového dodávateľa');
  save.style.borderColor='#1f6feb';save.style.background='#ddf4ff';save.style.color='#0969da';
  save.onclick=async()=>{
    const e=ean.value.replace(/[\s-]/g,'');
    if(!e){alert('Bez EAN kódu EDI sa dodávateľ nedá uložiť — nájdeš ho v CODEXe pri dodávateľovi.');return}
    if(!/^\d+$/.test(e)){alert('EAN kód EDI musí byť len číslice.');return}
    if(!name.value.trim()){alert('vyplň názov firmy');return}
    status.textContent='';extra.textContent='';save.disabled=true;
    try{
      await api('/api/orders/question/'+q.id+'/answer',{method:'POST',body:JSON.stringify({
        new_supplier:{ean_edi:e,name:name.value.trim(),emails:emails.value.trim(),
          city:city.value.trim()}})});
      await load()
    }catch(err){
      save.disabled=false;
      status.textContent=err.message||'chyba';
      // Deep-review finding (independent review, same PR): mirror newCustomerForm's own
      // one-click reclaim button — a 409 collision (see the httpapi.py collision check
      // this form posts to) already carries err.body.existing; render it instead of
      // leaving her to re-type the same EAN into the search box above.
      if(err.body&&err.body.existing){
        const b=el('button',null,'Použiť existujúceho '+err.body.existing.name);
        b.onclick=()=>answerGeneric(q.id,err.body.existing.ean_edi);
        extra.appendChild(b)}
    }
  };
  form.appendChild(save);
  toggle.onclick=()=>{const open=form.style.display==='none';form.style.display=open?'block':'none';form.dataset.open=open?'1':'';};
  box.appendChild(toggle);box.appendChild(form);
  return box}
function newDlProductForm(q){
  const box=el('div');box.style.marginTop='12px';
  const toggle=el('button',null,'➕ Nový produkt (najprv EAN kód na karte v Codexe)');
  toggle.style.borderColor='#d0d7de';toggle.style.background='#f6f8fa';toggle.style.color='#57606a';
  const form=el('div');form.style.display='none';
  const gtin=el('input');gtin.placeholder='GTIN (EAN kód) *';
  const name=el('input');name.placeholder='názov produktu *';name.value=q.wording||'';
  for(const i of [gtin,name])form.appendChild(i);
  const status=el('div','slabel','');form.appendChild(status);
  const save=el('button',null,'Uložiť nový produkt');
  save.style.borderColor='#1f6feb';save.style.background='#ddf4ff';save.style.color='#0969da';
  save.onclick=async()=>{
    const g=gtin.value.replace(/[\s-]/g,'');
    if(!g){alert('Bez GTIN sa karta nedá uložiť — nájdeš ho v CODEXe pri produkte.');return}
    if(!/^\d+$/.test(g)){alert('GTIN musí byť len číslice.');return}
    if(!name.value.trim()){alert('vyplň názov produktu');return}
    status.textContent='';save.disabled=true;
    try{
      await api('/api/orders/question/'+q.id+'/answer',{method:'POST',body:JSON.stringify({
        new_item:{gtin:g,name:name.value.trim()}})});
      await load()
    }catch(err){save.disabled=false;status.textContent=err.message||'chyba'}
  };
  form.appendChild(save);
  toggle.onclick=()=>{const open=form.style.display==='none';form.style.display=open?'block':'none';form.dataset.open=open?'1':'';};
  box.appendChild(toggle);box.appendChild(form);
  return box}
function dlSupplierQuestionCard(q){
  const c=el('div','q');
  c.appendChild(el('div','who',GENERIC_TITLE.dl_supplier));
  c.appendChild(el('div','w',q.wording||''));
  c.appendChild(el('div','why',q.reason||''));
  for(const opt of (q.candidates||[])){
    const b=el('button',null,opt.label||opt.value);
    b.onclick=()=>answerGeneric(q.id,opt.value);c.appendChild(b)}
  c.appendChild(el('div','slabel','alebo nájdi v celej databáze dodávateľov:'));
  c.appendChild(dlSupplierSearchBox(q));
  c.appendChild(newDlSupplierForm(q));
  const nb=el('button',null,'Neviem');
  nb.style.borderColor='#d0d7de';nb.style.background='#f6f8fa';nb.style.color='#57606a';
  nb.onclick=()=>answerGeneric(q.id,'unknown');c.appendChild(nb);
  const nw=el('button',null,'Netýka sa skladu');
  nw.style.borderColor='#eac54f';nw.style.background='#fff8c5';nw.style.color='#7d4e00';
  nw.onclick=()=>answerNotWarehouse(q.id);c.appendChild(nw);
  return c}
function dlItemQuestionCard(q){
  const c=el('div','q');
  c.appendChild(el('div','who',GENERIC_TITLE.dl_item));
  c.appendChild(el('div','w',q.wording||''));
  c.appendChild(el('div','why',q.reason||''));
  for(const opt of (q.candidates||[])){
    const b=el('button',null,opt.label||opt.value);
    b.onclick=()=>answerGeneric(q.id,opt.value);c.appendChild(b)}
  c.appendChild(el('div','slabel','alebo nájdi v celom DL katalógu:'));
  c.appendChild(dlItemSearchBox(q));
  c.appendChild(newDlProductForm(q));
  const dlhint=el('div',null,'Karta nie je v katalógu? Pridaj ju vyššie („➕ Nový produkt“) alebo v 📚 databáze znalostí — tabuľku „EAN slovnormal“ už program nečíta.');
  dlhint.style.cssText='margin-top:6px;font-size:12px;color:#6a737d';c.appendChild(dlhint);
  // #365: "nemá kartu — pošli bez tejto položky" — ships the doc WITHOUT this line
  // (confirmed, honest). Distinct from "Neviem" (defers the whole DL). Confirm first — it
  // sends an incomplete document to ORION.
  const sw=el('button',null,'Nemá kartu — pošli bez tejto položky');
  sw.style.borderColor='#e6a23c';sw.style.background='#fff3e0';sw.style.color='#8a5a00';
  sw.onclick=()=>{if(confirm('Naozaj poslať dodací list BEZ tejto položky? Doklad odíde do '
    +'ORIONu neúplný — použi len ak položka naozaj nemá skladovú kartu.'))answerGeneric(q.id,'ship_without')};
  c.appendChild(sw);
  const nb=el('button',null,'Neviem');
  nb.style.borderColor='#d0d7de';nb.style.background='#f6f8fa';nb.style.color='#57606a';
  nb.onclick=()=>answerGeneric(q.id,'unknown');c.appendChild(nb);
  const nw=el('button',null,'Netýka sa skladu');
  nw.style.borderColor='#eac54f';nw.style.background='#fff8c5';nw.style.color='#7d4e00';
  nw.onclick=()=>answerNotWarehouse(q.id);c.appendChild(nw);
  return c}
// #234: a live search over ALL current customers — not just the frozen candidates the
// question was asked with. Mirrors searchBox() above, one input, debounced.
function customerSearchBox(q){
  const wrap=el('div');
  const inp=el('input','search');inp.placeholder='hľadaj zákazníka (názov alebo EAN)…';
  const res=el('div','sres-wrap');
  wrap.appendChild(inp);wrap.appendChild(res);
  let seq=0;
  async function run(v){
    const mine=++seq;
    if(v.length<2){res.textContent='';return}
    let d;try{d=await api('/api/znalosti/clients?q='+encodeURIComponent(v))}catch(e){return}
    if(mine!==seq)return;
    res.textContent='';
    const hits=d.items.filter(it=>it.ean_edi);   // an EAN-less row cannot be picked either
    if(!hits.length){res.appendChild(el('div','sres none','žiadna zhoda'));return}
    for(const it of hits){
      const addr=[it.street,it.city].filter(Boolean).join(', ');
      const b=el('div','sres',it.name+(addr?'  ('+addr+')':'')+'  ('+it.ean_edi+')');
      b.onclick=()=>answerCustomer(q.id,it.ean_edi,it.name||'');res.appendChild(b)}
  }
  let t=null;
  inp.oninput=()=>{clearTimeout(t);t=setTimeout(()=>run(inp.value.trim()),200)};
  return wrap}
// #234: the customer genuinely does not exist anywhere yet — create it right on the card,
// prefilled from the mail. Collapsed by default so the common (candidate/search) path
// stays uncluttered.
function newCustomerForm(q){
  const ctx=q.context||{};
  const box=el('div');box.style.marginTop='12px';
  const toggle=el('button',null,'➕ Nový zákazník (najprv ho vytvor v CODEXe)');
  toggle.style.borderColor='#d0d7de';toggle.style.background='#f6f8fa';toggle.style.color='#57606a';
  const form=el('div');form.style.display='none';
  const ean=el('input');ean.placeholder='EAN kód EDI *';
  const name=el('input');name.placeholder='názov firmy *';
  name.value=ctx.company_name||ctx.sender_name||'';
  const emails=el('input');emails.placeholder='e-maily';emails.value=ctx.sender_email||'';
  const city=el('input');city.placeholder='obec';
  const street=el('input');street.placeholder='ulica';street.value=ctx.delivery_address_guess||'';
  const zip=el('input');zip.placeholder='PSČ';
  for(const i of [ean,name,emails,city,street,zip])form.appendChild(i);
  const status=el('div','slabel','');form.appendChild(status);
  const extra=el('div');form.appendChild(extra);
  const save=el('button',null,'Uložiť nového zákazníka');
  save.style.borderColor='#1f6feb';save.style.background='#ddf4ff';save.style.color='#0969da';
  save.onclick=async()=>{
    const e=ean.value.replace(/[\s-]/g,'');
    if(!e){alert('Bez EAN kódu EDI sa zákazník nedá uložiť — nájdeš ho v CODEXe pri odberateľovi.');return}
    if(!/^\d+$/.test(e)){alert('EAN kód EDI musí byť len číslice.');return}
    if(!name.value.trim()){alert('vyplň názov firmy');return}
    if(e.length!==13&&!confirm('EAN kód EDI má obvykle 13 číslic, zadal si '+e.length+'. Naozaj uložiť?'))return;
    status.textContent='';extra.textContent='';
    // #234 review finding: a fast double-click sent two overlapping POSTs — the server
    // is now race-safe (advisory lock) either way, but this closes off the easy trigger.
    save.disabled=true;
    try{
      await api('/api/orders/question/'+q.id+'/answer',{method:'POST',body:JSON.stringify({
        new_customer:{ean_edi:e,name:name.value.trim(),emails:emails.value.trim(),
          city:city.value.trim(),street:street.value.trim(),zip:zip.value.trim()}})});
      await load()
    }catch(err){
      save.disabled=false;
      status.textContent=err.message||'chyba';
      if(err.body&&err.body.existing){
        const b=el('button',null,'Doplniť e-mail k '+err.body.existing.name);
        b.onclick=()=>answerCustomer(q.id,err.body.existing.ean_edi,err.body.existing.name);
        extra.appendChild(b)}
    }
  };
  form.appendChild(save);
  toggle.onclick=()=>{const open=form.style.display==='none';form.style.display=open?'block':'none';form.dataset.open=open?'1':'';};
  box.appendChild(toggle);box.appendChild(form);
  return box}
// #159: "who is this customer?" candidates render as name + address (+ a ✓ badge when
// the ranking already found the address in the mail), plus a "neviem, kto to je" escape.
// #234: a candidate with no EAN renders disabled (would just 400), plus a live search over
// every current customer and a "add a brand-new one" form, right on the same card.
function customerQuestionCard(q){
  const ctx=q.context||{};const c=el('div','q');
  c.appendChild(el('div','who','Neznámy zákazník'+(q.delivery_date?' · na '+q.delivery_date:'')));
  c.appendChild(el('div','w',ctx.sender_email||q.wording));
  const bits=[];
  if(ctx.sender_name)bits.push('meno: '+ctx.sender_name);
  if(ctx.company_name)bits.push('firma: '+ctx.company_name);
  if(ctx.delivery_address_guess)bits.push('adresa v maile: '+ctx.delivery_address_guess);
  c.appendChild(el('div','why',(q.reason||'Kto to objednal?')+(bits.length?' — '+bits.join(' · '):'')));
  for(const cand of (q.candidates||[])){
    const addr=[cand.street,cand.city].filter(Boolean).join(', ');
    if(!cand.ean_edi){
      const b=el('button',null,(cand.name||'(bez mena)')+(addr?'  ('+addr+')':'')+
        '  — bez EAN, doplň v databáze znalostí');
      b.disabled=true;b.style.opacity='0.55';b.style.cursor='default';b.style.borderColor='#d0d7de';
      b.style.background='#f6f8fa';b.style.color='#57606a';c.appendChild(b);continue}
    const label=(cand.name||cand.ean_edi)+(addr?'  ('+addr+')':'')+(cand.address_match?'  ✓ adresa sedí':'');
    const b=el('button',null,label);
    b.onclick=()=>answerCustomer(q.id,cand.ean_edi,cand.name||'');c.appendChild(b)}
  c.appendChild(el('div','slabel','alebo nájdi v celej databáze zákazníkov:'));
  c.appendChild(customerSearchBox(q));
  c.appendChild(newCustomerForm(q));
  const nb=el('button',null,'Neviem, kto to je');
  nb.style.borderColor='#d0d7de';nb.style.background='#f6f8fa';nb.style.color='#57606a';
  nb.onclick=()=>answerCustomer(q.id,'','',true);c.appendChild(nb);
  // #369: the third escape — this is not an order at all (a supplier-eshop confirmation),
  // teach a mail_rules ignore rule so mail of this shape stops asking.
  const no=el('button',null,'Nie je to objednávka — takéto maily ignoruj');
  no.style.borderColor='#d0d7de';no.style.background='#f6f8fa';no.style.color='#57606a';
  no.onclick=()=>answerCustomerNotOrder(q.id);c.appendChild(no);
  return c}
async function answerCustomer(qid,ean_edi,name,unknown){try{await api('/api/orders/question/'+qid+'/answer',
  {method:'POST',body:JSON.stringify(unknown?{unknown:true}:{ean_edi:ean_edi,name:name})});await load()}
  catch(e){alert(e.message||'chyba')}}
async function answerCustomerNotOrder(qid){try{await api('/api/orders/question/'+qid+'/answer',
  {method:'POST',body:JSON.stringify({not_order:true})});await load()}
  catch(e){alert(e.message||'chyba')}}
async function load(){const mine=++render;let d,t;
  try{d=await api('/api/orders/questions');t=await api('/api/orders/taught')}catch(e){return}
  if(mine!==render)return;
  const W=document.getElementById('wrap');W.textContent='';
  if(!d.items.length)W.appendChild(el('div','empty','Nič nečaká. Ďakujem!'));
  for(const q of d.items){
    if(q.kind==='customer'){W.appendChild(customerQuestionCard(q));continue}
    if(q.kind==='dl_supplier'){W.appendChild(dlSupplierQuestionCard(q));continue}
    if(q.kind==='dl_item'){W.appendChild(dlItemQuestionCard(q));continue}
    if(q.kind==='mail'||q.kind==='date'||q.kind==='line'){
      W.appendChild(genericQuestionCard(q));continue}
    const c=el('div','q');
    c.appendChild(el('div','who',(q.customer_name||q.customer_ean)+(q.delivery_date?' · na '+q.delivery_date:'')));
    c.appendChild(el('div','w',q.wording+(q.quantity?'  —  '+q.quantity+' '+(q.unit||'ks'):'')));
    c.appendChild(el('div','why',q.reason||'Ktorý výrobok to je?'));
    c.appendChild(lineFields(q));
    for(const cand of (q.candidates||[])){const b=el('button',null,cand.name||cand.gtin);
      b.onclick=()=>teach(q.id,cand.gtin,cand.name||'');c.appendChild(b)}
    c.appendChild(el('div','slabel','alebo vyhľadaj v celom katalógu:'));
    c.appendChild(searchBox(q));
    const kb=document.createElement('a');kb.className='kb';kb.textContent='📚 databáza znalostí';
    kb.href='/znalosti/'+encodeURIComponent(q.customer_ean)+'?wording='+encodeURIComponent(q.wording);
    c.appendChild(kb);
    const hint=el('div',null,'Karta nie je v katalógu? Pridaj ju v 📚 databáze znalostí — tabuľku „EAN slovnormal“ už program nečíta.');
    hint.style.cssText='margin-top:6px;font-size:12px;color:#6a737d';c.appendChild(hint);
    const mb=el('button',null,'Vyriešené ručne — zadané do CODEXu, nič neposielať');
    mb.style.borderColor='#8250df';mb.style.background='#f3eefe';mb.style.color='#5a32a3';mb.style.marginTop='8px';
    mb.onclick=()=>{if(confirm('Naozaj vyriešené ručne? Objednávku si zadala do CODEXu — nič sa NEpošle do '
      +'ORIONu a zatvoria sa všetky dni dodania z tohto mailu, ktoré čakali na túto otázku.'))manualResolve(q.id)};
    c.appendChild(mb);
    W.appendChild(c)}
  if(t.items.length){W.appendChild(el('h2',null,'Naposledy naučené'));
    for(const x of t.items){const r=el('div','t');
      r.appendChild(el('span',null,x.wording+' → '+(x.answer_card==='not_order'?'nie je objednávka':(x.answer_card||x.answer_gtin))));
      const b=el('button',null,'vrátiť');b.onclick=()=>undo(x.id);r.appendChild(b);W.appendChild(r)}}}
async function teach(qid,gtin,card){try{
  const body={gtin:gtin,card:card};
  const qi=document.getElementById('oqty_'+qid),pi=document.getElementById('oprice_'+qid);
  if(qi)body.quantity=qi.value;if(pi)body.unit_price=pi.value;   // #360: confirmed qty+price
  await api('/api/orders/question/'+qid+'/answer',
    {method:'POST',body:JSON.stringify(body)});delete searchState[qid];await load()}
  catch(e){alert(e.message||'chyba')}}
// #384: „Vyriešené ručne" — the sklad entered this order into CODEX by hand; release every
// held order of this mail WITHOUT any ORION upload. Posts {manual:true}; the server refuses
// (409) if the question blocks orders from several mails, alert() surfaces it.
async function manualResolve(qid){try{
  await api('/api/orders/question/'+qid+'/answer',
    {method:'POST',body:JSON.stringify({manual:true})});delete searchState[qid];await load()}
  catch(e){alert(e.message||'chyba')}}
async function undo(qid){try{await api('/api/orders/question/'+qid+'/undo',{method:'POST'});
  await load()}catch(e){alert(e.message||'chyba')}}
__STATS_SCRIPT__
// #306: the periodic refresh must NEVER wipe an in-progress entry. load() rebuilds the
// whole board (wrap.textContent=''), so while the skladníčka is typing into a search box
// or has a "➕ Nový…" form open, a 5s rebuild would destroy her half-filled form mid-entry
// ("stále ma to vyhodí"). maybeRefresh() skips the rebuild whenever she is mid-interaction
// (a focused input/textarea, or an open collapsible form marked data-open="1"); an
// EXPLICIT load() after a real answer still rebuilds so the answered card vanishes.
function boardBusy(){
  const w=document.getElementById('wrap');if(!w)return false;
  const a=document.activeElement;
  if(a&&w.contains(a)&&(a.tagName==='INPUT'||a.tagName==='TEXTAREA'))return true;
  if(w.querySelector('[data-open="1"]'))return true;
  return false}
function maybeRefresh(){if(!boardBusy())load()}
load();setInterval(maybeRefresh,5000);
</script></body></html>"""

ASK_HTML = (_ASK_HTML_TEMPLATE
           .replace("__TITLE__", "Otázky skladu")
           .replace("__HEADING__", "&#128230; Otázky skladu")
           .replace("__STATS_HEADER__", "")
           .replace("__ALERT_BANNER__", "")
           .replace("__STATS_SCRIPT__", ""))

# #231: the DL nástenka additionally shows today/yesterday's DL run "stavy" (states) —
# a small text strip in the header, fed by `/api/orders/dl/stats` (role-scoped, see
# httpapi.py's `api_orders_dl_stats`). The orders board has no equivalent (out of scope
# for this ticket) — the placeholders above are replaced with "" for it, so nothing is
# fetched or rendered there.
#
# #239 reopened, finding 5: the three current-state gauges used to be three words
# silently appended to this SAME small header strip, shown only when non-zero — visually
# indistinguishable from ordinary text, easy to miss entirely (verified live: on a quiet
# day nothing at all was rendered, so the warehouse never learned the feature existed).
# A separate, visually prominent banner (`__ALERT_BANNER__`, hidden when quiet) now
# carries them instead, each on its own line with plain wording explaining what happened
# and what to do — the header strip itself stays the plain today/yesterday summary only.
ASK_DL_HTML = (_ASK_HTML_TEMPLATE
              .replace("__TITLE__", "Dodacie listy — sklad")
              .replace("__HEADING__", "&#128666; Dodacie listy")
              .replace("__STATS_HEADER__", '<span class="ver" id="dlStats"></span>')
              .replace("__ALERT_BANNER__",
                      '<div id="dlAlertBanner" class="dl-alert-banner" '
                      'style="display:none"></div>')
              .replace("__STATS_SCRIPT__", r"""
async function loadStats(){try{const d=await api('/api/orders/dl/stats');
  const t=d.today||{},y=d.yesterday||{};
  let s='dnes: '+(t.runs||0)+' spracovaných, '+(t.duplicates||0)+' duplicít, '+
    (t.announced_mismatch||0)+' nezhôd · včera: '+(y.runs||0)+' spracovaných';
  document.getElementById('dlStats').textContent=s;
  // #239 reopened, finding 5: each nonzero class gets its OWN plain-Slovak line in a
  // prominent banner, explaining what happened and what to do — never just a number
  // silently glued onto the header strip above.
  const banner=document.getElementById('dlAlertBanner');
  const lines=[];
  if(t.quarantined) lines.push('&#128683; '+t.quarantined+' dodací(ch) list(ov) sa po '+
    (t.quarantine_threshold||5)+' pokusoch vzdalo spracovania &mdash; skontroluj v '+
    'dashboarde.');
  if(t.pending_alerts) lines.push('&#128276; '+t.pending_alerts+
    ' upozornenie/upozornení stále čaká na odoslanie do Odoo.');
  if(t.open_import_incidents) lines.push('&#128230; '+t.open_import_incidents+
    ' otvorený problém s importom do ORIONu.');
  if(lines.length){
    banner.innerHTML=lines.map(l=>'<div>'+l+'</div>').join('');
    banner.style.display='block';
  }else{
    banner.style.display='none';
    banner.innerHTML='';
  }}
  catch(e){}}
loadStats();setInterval(loadStats,30000);"""))


# #104: direct curation of wording->card knowledge (no order_questions row required).
# Same page for /znalosti (global only, + a customer search to jump to one) and
# /znalosti/<ean> (that customer's own aliases + the global section underneath) — the JS
# below reads the ean out of location.pathname, exactly like ASK_HTML reads none at all.
ZNALOSTI_HTML = r"""<!doctype html><html lang="sk"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Databáza znalostí</title>
<style>
 *{box-sizing:border-box}
 body{font:15px/1.5 -apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif;margin:0;
      background:#f6f8fa;color:#1f2328}
 header{background:#161b22;color:#e6edf3;padding:12px 16px;display:flex;justify-content:space-between;
        align-items:center;position:sticky;top:0}
 h1{font-size:16px;margin:0}
 .ver{font-size:12px;color:#8b949e}
 main{padding:14px 12px;max-width:760px;margin:0 auto}
 h2{font-size:14px;color:#57606a;margin:22px 0 8px}
 .box{background:#fff;border:1px solid #d0d7de;border-radius:12px;padding:14px;margin-bottom:14px}
 .row{background:#fff;border:1px solid #d0d7de;border-radius:10px;padding:10px 12px;margin-bottom:8px;
      display:flex;justify-content:space-between;align-items:center;gap:10px;font-size:14px}
 .row .meta{font-size:12px;color:#57606a}
 .row button{width:auto;margin:0;border-color:#d0d7de;background:#f6f8fa;color:#57606a;padding:7px 12px;
             border-radius:8px;font:inherit;cursor:pointer}
 input{width:100%;padding:9px 10px;margin-top:6px;border:1px solid #d0d7de;border-radius:8px;font:inherit}
 .cands{margin-top:4px}
 .cand{padding:8px 10px;border:1px solid #d0d7de;border-radius:8px;margin-top:4px;cursor:pointer;font-size:14px}
 .cand:hover{background:#ddf4ff}
 .picked{font-size:13px;color:#1a7f37;margin-top:6px}
 button.add{display:block;width:100%;text-align:center;padding:11px;margin-top:10px;font:inherit;
        border:1px solid #1f6feb;border-radius:10px;background:#ddf4ff;color:#0969da;cursor:pointer}
 .box>button:not(.add){display:block;width:100%;text-align:center;padding:9px;margin-top:6px;
        font:inherit;border:1px solid #d0d7de;border-radius:10px;background:#f6f8fa;
        color:#57606a;cursor:pointer}
 .empty{color:#57606a;padding:6px 2px}
 .who{font-size:13px;color:#57606a;margin-bottom:6px}
</style></head><body>
<header><h1>&#128218; Databáza znalostí</h1><span class="ver" data-testid="version">v__VERSION__</span></header>
<main id="wrap"><div class="empty">Nahrávam&hellip;</div></main>
<script>
async function api(u,o){const r=await fetch(u,Object.assign({headers:{'Content-Type':'application/json'}},o||{}));
  if(!r.ok){throw new Error((await r.json().catch(()=>({}))).error||('HTTP '+r.status))}return r.json()}
function el(t,cls,txt){const e=document.createElement(t);if(cls)e.className=cls;
  if(txt!==undefined)e.textContent=txt;return e}
const parts=location.pathname.split('/').filter(Boolean);
const EAN=parts.length>1?decodeURIComponent(parts[1]):'';
const params=new URLSearchParams(location.search);
const PREFILL=params.get('wording')||'';
let picked=null;

function pickerBox(inputId,candId,onPick){
  const wrap=el('div');
  const inp=el('input');inp.id=inputId;inp.placeholder='hľadaj kartu (názov alebo GTIN)…';
  const cands=el('div','cands');cands.id=candId;
  wrap.appendChild(inp);wrap.appendChild(cands);
  let t=null;
  inp.oninput=()=>{clearTimeout(t);t=setTimeout(async()=>{
    const q=inp.value.trim();cands.textContent='';
    if(q.length<2)return;
    const d=await api('/api/znalosti/catalog?q='+encodeURIComponent(q));
    for(const it of d.items){const c=el('div','cand',it.name+'  ('+it.gtin+')');
      c.onclick=()=>{onPick(it);inp.value=it.name;cands.textContent=''};cands.appendChild(c)}
  },200)};
  return wrap;
}

function addForm(onSubmit,wordingPrefill){
  const box=el('div','box');
  box.appendChild(el('h2',null,'Pridať priradenie'));
  const w=el('input');w.placeholder='znenie (ako to zákazník píše)';w.value=wordingPrefill||'';
  box.appendChild(w);
  let chosen=null;
  const status=el('div','picked','');
  box.appendChild(pickerBox('','',(it)=>{chosen=it;status.textContent='vybraná karta: '+it.name}));
  box.appendChild(status);
  const b=el('button','add','Uložiť');
  b.onclick=async()=>{
    if(!w.value.trim()||!chosen){alert('vyplň znenie a vyber kartu zo zoznamu');return}
    try{await onSubmit(w.value.trim(),chosen.gtin,chosen.name);w.value='';chosen=null;
      status.textContent='';await load()}catch(e){alert(e.message||'chyba')}
  };
  box.appendChild(b);
  return box;
}

function aliasRow(item,onDelete){
  const r=el('div','row');
  const left=el('div');
  left.appendChild(el('div',null,item.item_raw+' → '+(item.card||item.gtin)));
  left.appendChild(el('div','meta',(item.source||item.taught_by||'')+' · '+
    String(item.created_at||'').slice(0,10)));
  r.appendChild(left);
  const curated=(item.source===undefined)||item.source==='human'||item.source==='sheet-import';
  if(curated){const b=el('button',null,'zmazať');b.onclick=onDelete;r.appendChild(b)}
  return r;
}

// #127: direct add/edit/retire of product cards, keyed by GTIN — one form doubles as
// add (unknown GTIN) and edit (known GTIN); a click on a search result loads it in.
function productsBox(){
  const box=el('div','box');
  box.appendChild(el('h2',null,'Karty výrobkov'));
  // #383: the Google Sheet „EAN slovnormal" is retired — this page is the ONLY source of cards.
  const notice=el('div',null,'⚠️ Karty pridávaj a upravuj LEN tu. Tabuľku „EAN slovnormal“ '
    +'už program NEČÍTA — čokoľvek doplníš do tabuľky, program neuvidí.');
  notice.style.cssText='margin:4px 0 10px;padding:8px 10px;border-radius:6px;background:#fff8c5;'
    +'border:1px solid #eac54f;color:#7d4e00;font-size:13px';
  box.appendChild(notice);
  const gtin=el('input');gtin.placeholder='GTIN';
  const name=el('input');name.placeholder='názov karty';
  const alias=el('input');alias.placeholder='doplnok / alias (napr. „rožok 70g")';
  let aliasTouched=false;alias.oninput=()=>{aliasTouched=true};gtin.oninput=()=>{aliasTouched=false};
  box.appendChild(gtin);box.appendChild(name);box.appendChild(alias);
  const status=el('div','picked','');box.appendChild(status);
  const list=el('div');
  async function refresh(q){
    list.textContent='';
    const d=await api('/api/znalosti/products'+(q?('?q='+encodeURIComponent(q)):''));
    if(!d.items.length){list.appendChild(el('div','empty','Zatiaľ nič.'));return}
    for(const it of d.items){
      const r=el('div','row');
      r.appendChild(el('div',null,it.name+'  ('+it.gtin+')'
        +(it.alias?' · doplnok: '+it.alias:'')+(it.overridden?' · upravené':'')));
      const b=el('button',null,'upraviť');
      b.onclick=()=>{gtin.value=it.gtin;name.value=it.name;alias.value=it.alias||'';aliasTouched=true;status.textContent=''};
      r.appendChild(b);list.appendChild(r)
    }
  }
  const save=el('button','add','Uložiť (nový GTIN = pridá, existujúci = upraví)');
  save.onclick=async()=>{
    if(!gtin.value.trim()||!name.value.trim()){alert('vyplň GTIN aj názov');return}
    // #383: always send `doplnok` (the field is prefilled with the card's current alias),
    // so an edit here sets it explicitly ("" clears it). The sheet is dead, so this page owns it.
    // #383: send `doplnok` ONLY when the alias field was actually engaged (typed into, or
    // prefilled by „upraviť") — a name-only save on a hand-typed GTIN must NOT silently clear a
    // card's existing alias (a real match.py signal). An empty field that WAS engaged clears it.
    const _pb={gtin:gtin.value.trim(),name:name.value.trim()};
    if(aliasTouched)_pb.doplnok=alias.value.trim();
    try{await api('/api/znalosti/products',{method:'POST',body:JSON.stringify(_pb)});
      status.textContent='uložené';await refresh(search.value.trim())}
    catch(e){alert(e.message||'chyba')}
  };
  box.appendChild(save);
  const retire=el('button',null,'Vyradiť kartu s GTIN vyššie');
  retire.onclick=async()=>{
    const g=gtin.value.trim();if(!g)return;
    if(!confirm('Vyradiť kartu '+g+'?'))return;
    try{await api('/api/znalosti/products/'+encodeURIComponent(g),{method:'DELETE'});
      gtin.value='';name.value='';alias.value='';status.textContent='vyradené';await refresh(search.value.trim())}
    catch(e){alert(e.message||'chyba')}
  };
  box.appendChild(retire);
  var search=el('input');search.placeholder='hľadaj kartu (názov alebo GTIN)…';
  box.appendChild(search);box.appendChild(list);
  let t=null;search.oninput=()=>{clearTimeout(t);t=setTimeout(()=>refresh(search.value.trim()),200)};
  refresh('');
  return box;
}

// #128: direct add/edit/retire of customers. `editing` tracks the identity the SAVE
// button targets (null = a brand-new customer); picking a search result fills the form
// AND the identity, exactly like productsBox does with a bare gtin.
function clientsBox(){
  const box=el('div','box');
  box.appendChild(el('h2',null,'Odberatelia'));
  const ean=el('input');ean.placeholder='EAN kód EDI *';
  const name=el('input');name.placeholder='názov firmy';
  const emails=el('input');emails.placeholder='e-maily (čiarkou oddelené)';
  const city=el('input');city.placeholder='obec';
  const street=el('input');street.placeholder='ulica';
  const zip=el('input');zip.placeholder='PSČ';
  for(const i of [ean,name,emails,city,street,zip])box.appendChild(i);
  const status=el('div','picked','');box.appendChild(status);
  let editing=null;
  function clearForm(){ean.value=name.value=emails.value=city.value=street.value=zip.value='';editing=null}
  const list=el('div');
  async function refresh(q){
    list.textContent='';
    const d=await api('/api/znalosti/clients'+(q?('?q='+encodeURIComponent(q)):''));
    if(!d.items.length){list.appendChild(el('div','empty','Zatiaľ nič.'));return}
    for(const it of d.items){
      const r=el('div','row');
      // #234: a legacy blank-EAN row must be VISIBLE as needing attention, not silent.
      r.appendChild(el('div',null,it.name+'  ('+(it.ean_edi||'bez EAN — doplň')+')'+
        (it.street?(' · '+it.street):'')));
      const b=el('button',null,'upraviť');
      b.onclick=()=>{
        ean.value=it.ean_edi||'';name.value=it.name||'';emails.value=(it.emails||[]).join(', ');
        city.value=it.city||'';street.value=it.street||'';zip.value=it.zip||'';
        editing={override_id:it.override_id,orig_ean_edi:it.orig_ean_edi,orig_street:it.orig_street};
        status.textContent=''
      };
      r.appendChild(b);list.appendChild(r)
    }
  }
  const save=el('button','add','Uložiť');
  save.onclick=async()=>{
    if(!name.value.trim()){alert('vyplň názov');return}
    const cleaned=ean.value.trim().replace(/[\s-]/g,'');
    if(!cleaned){alert('Bez EAN kódu EDI sa zákazník nedá uložiť — nájdeš ho v CODEXe pri odberateľovi.');return}
    if(!/^\d+$/.test(cleaned)){alert('EAN kód EDI musí byť len číslice.');return}
    const body={ean_edi:cleaned,name:name.value.trim(),emails:emails.value.trim(),
      city:city.value.trim(),street:street.value.trim(),zip:zip.value.trim()};
    if(editing)Object.assign(body,editing);
    try{await api('/api/znalosti/clients',{method:'POST',body:JSON.stringify(body)});
      status.textContent='uložené';await refresh(search.value.trim())}
    catch(e){alert(e.message||'chyba')}
  };
  box.appendChild(save);
  const retire=el('button',null,'Vyradiť tohto odberateľa');
  retire.onclick=async()=>{
    if(!editing){alert('najprv vyber existujúceho odberateľa zo zoznamu');return}
    if(!confirm('Vyradiť '+(name.value||'tohto odberateľa')+'?'))return;
    try{await api('/api/znalosti/clients',{method:'DELETE',body:JSON.stringify(editing)});
      clearForm();status.textContent='vyradené';await refresh(search.value.trim())}
    catch(e){alert(e.message||'chyba')}
  };
  box.appendChild(retire);
  var search=el('input');search.placeholder='hľadaj odberateľa (názov alebo EAN)…';
  box.appendChild(search);box.appendChild(list);
  let t=null;search.oninput=()=>{clearTimeout(t);t=setTimeout(()=>refresh(search.value.trim()),200)};
  refresh('');
  return box;
}

// #221: direct add/edit/retire of DL catalog cards (mirror of #127's productsBox, with the
// DL-specific fields doplnok/mass/sklad/cena added — see dl_snapshot.py).
function dlProductsBox(){
  const box=el('div','box');
  box.appendChild(el('h2',null,'DL katalóg (dodacie listy)'));
  const gtin=el('input');gtin.placeholder='GTIN';
  const name=el('input');name.placeholder='názov karty';
  const doplnok=el('input');doplnok.placeholder='doplnok';
  const mass=el('input');mass.placeholder='hmotnosť (kg)';
  const sklad=el('input');sklad.placeholder='sklad';
  const cena=el('input');cena.placeholder='cena';
  for(const i of [gtin,name,doplnok,mass,sklad,cena])box.appendChild(i);
  const status=el('div','picked','');box.appendChild(status);
  const list=el('div');
  async function refresh(q){
    list.textContent='';
    const d=await api('/api/znalosti/dl-products'+(q?('?q='+encodeURIComponent(q)):''));
    if(!d.items.length){list.appendChild(el('div','empty','Zatiaľ nič.'));return}
    for(const it of d.items){
      const r=el('div','row');
      r.appendChild(el('div',null,it.name+'  ('+it.gtin+')'+(it.overridden?' · upravené':'')));
      const b=el('button',null,'upraviť');
      b.onclick=()=>{gtin.value=it.gtin;name.value=it.name;doplnok.value=it.doplnok||'';
        mass.value=it.mass==null?'':it.mass;sklad.value=it.sklad||'';
        cena.value=it.cena==null?'':it.cena;status.textContent=''};
      r.appendChild(b);list.appendChild(r)
    }
  }
  const save=el('button','add','Uložiť (nový GTIN = pridá, existujúci = upraví)');
  save.onclick=async()=>{
    if(!gtin.value.trim()||!name.value.trim()){alert('vyplň GTIN aj názov');return}
    try{await api('/api/znalosti/dl-products',{method:'POST',
      body:JSON.stringify({gtin:gtin.value.trim(),name:name.value.trim(),
        doplnok:doplnok.value.trim(),mass:mass.value.trim(),sklad:sklad.value.trim(),
        cena:cena.value.trim()})});
      status.textContent='uložené';await refresh(search.value.trim())}
    catch(e){alert(e.message||'chyba')}
  };
  box.appendChild(save);
  const retire=el('button',null,'Vyradiť kartu s GTIN vyššie');
  retire.onclick=async()=>{
    const g=gtin.value.trim();if(!g)return;
    if(!confirm('Vyradiť kartu '+g+'?'))return;
    try{await api('/api/znalosti/dl-products/'+encodeURIComponent(g),{method:'DELETE'});
      gtin.value='';name.value='';doplnok.value='';mass.value='';sklad.value='';cena.value='';
      status.textContent='vyradené';await refresh(search.value.trim())}
    catch(e){alert(e.message||'chyba')}
  };
  box.appendChild(retire);
  var search=el('input');search.placeholder='hľadaj kartu (názov alebo GTIN)…';
  box.appendChild(search);box.appendChild(list);
  let t=null;search.oninput=()=>{clearTimeout(t);t=setTimeout(()=>refresh(search.value.trim()),200)};
  refresh('');
  return box;
}

// #221: direct add/edit/retire of DL suppliers (mirror of #128's clientsBox). Identity is
// city-only (no street/zip) — dl_supplier_snapshot never persists those, see dl_snapshot.py.
function dlSuppliersBox(){
  const box=el('div','box');
  box.appendChild(el('h2',null,'Dodávatelia (dodacie listy)'));
  const ean=el('input');ean.placeholder='EAN kód EDI';
  const name=el('input');name.placeholder='názov firmy';
  const emails=el('input');emails.placeholder='e-maily (čiarkou oddelené)';
  const city=el('input');city.placeholder='obec';
  for(const i of [ean,name,emails,city])box.appendChild(i);
  const status=el('div','picked','');box.appendChild(status);
  let editing=null;
  function clearForm(){ean.value=name.value=emails.value=city.value='';editing=null}
  const list=el('div');
  async function refresh(q){
    list.textContent='';
    const d=await api('/api/znalosti/dl-suppliers'+(q?('?q='+encodeURIComponent(q)):''));
    if(!d.items.length){list.appendChild(el('div','empty','Zatiaľ nič.'));return}
    for(const it of d.items){
      const r=el('div','row');
      r.appendChild(el('div',null,it.name+'  ('+(it.ean_edi||'bez EAN')+')'+
        (it.city?(' · '+it.city):'')));
      const b=el('button',null,'upraviť');
      b.onclick=()=>{
        ean.value=it.ean_edi||'';name.value=it.name||'';emails.value=(it.emails||[]).join(', ');
        city.value=it.city||'';
        editing={override_id:it.override_id,orig_ean_edi:it.orig_ean_edi,orig_city:it.orig_city};
        status.textContent=''
      };
      r.appendChild(b);list.appendChild(r)
    }
  }
  const save=el('button','add','Uložiť');
  save.onclick=async()=>{
    if(!name.value.trim()){alert('vyplň názov');return}
    const body={ean_edi:ean.value.trim(),name:name.value.trim(),emails:emails.value.trim(),
      city:city.value.trim()};
    if(editing)Object.assign(body,editing);
    try{await api('/api/znalosti/dl-suppliers',{method:'POST',body:JSON.stringify(body)});
      status.textContent='uložené';await refresh(search.value.trim())}
    catch(e){alert(e.message||'chyba')}
  };
  box.appendChild(save);
  const retire=el('button',null,'Vyradiť tohto dodávateľa');
  retire.onclick=async()=>{
    if(!editing){alert('najprv vyber existujúceho dodávateľa zo zoznamu');return}
    if(!confirm('Vyradiť '+(name.value||'tohto dodávateľa')+'?'))return;
    try{await api('/api/znalosti/dl-suppliers',{method:'DELETE',body:JSON.stringify(editing)});
      clearForm();status.textContent='vyradené';await refresh(search.value.trim())}
    catch(e){alert(e.message||'chyba')}
  };
  box.appendChild(retire);
  var search=el('input');search.placeholder='hľadaj dodávateľa (názov alebo EAN)…';
  box.appendChild(search);box.appendChild(list);
  let t=null;search.oninput=()=>{clearTimeout(t);t=setTimeout(()=>refresh(search.value.trim()),200)};
  refresh('');
  return box;
}

// #128: on the /znalosti/<ean> page, edit THIS customer directly (no search needed —
// the page already fixes which one). `record` is null only if the ean matches nobody.
function customerEditBox(record, fallbackName){
  const box=el('div','box');
  box.appendChild(el('h2',null,'Upraviť údaje zákazníka'));
  const ean=el('input');ean.placeholder='EAN kód EDI';ean.value=(record&&record.ean_edi)||EAN;
  const name=el('input');name.placeholder='názov firmy';
  name.value=(record&&record.name)||fallbackName||'';
  const emails=el('input');emails.placeholder='e-maily (čiarkou oddelené)';
  emails.value=record?(record.emails||[]).join(', '):'';
  const city=el('input');city.placeholder='obec';city.value=(record&&record.city)||'';
  const street=el('input');street.placeholder='ulica';street.value=(record&&record.street)||'';
  const zip=el('input');zip.placeholder='PSČ';zip.value=(record&&record.zip)||'';
  for(const i of [ean,name,emails,city,street,zip])box.appendChild(i);
  const b=el('button','add','Uložiť zmeny');
  b.onclick=async()=>{
    if(!name.value.trim()){alert('vyplň názov');return}
    const body={ean_edi:ean.value.trim(),name:name.value.trim(),emails:emails.value.trim(),
      city:city.value.trim(),street:street.value.trim(),zip:zip.value.trim()};
    if(record){body.override_id=record.override_id;body.orig_ean_edi=record.orig_ean_edi;
      body.orig_street=record.orig_street}
    try{await api('/api/znalosti/clients',{method:'POST',body:JSON.stringify(body)});
      alert('uložené');location.reload()}
    catch(e){alert(e.message||'chyba')}
  };
  box.appendChild(b);
  return box;
}

async function load(){
  const W=document.getElementById('wrap');W.textContent='';
  if(EAN){
    const d=await api('/api/znalosti/customer/'+encodeURIComponent(EAN));
    W.appendChild(el('div','who',(d.customer_name||EAN)+'  ('+EAN+')'));
    W.appendChild(customerEditBox(d.record,d.customer_name));
    W.appendChild(addForm((wording,gtin,card)=>
      api('/api/znalosti/customer/'+encodeURIComponent(EAN),
         {method:'POST',body:JSON.stringify({wording:wording,gtin:gtin,card:card})}),
      PREFILL));
    W.appendChild(el('h2',null,'Priradenia tohto zákazníka'));
    if(!d.items.length)W.appendChild(el('div','empty','Zatiaľ nič.'));
    for(const it of d.items){W.appendChild(aliasRow(it,async()=>{
      try{await api('/api/znalosti/customer/'+encodeURIComponent(EAN)+'/'+it.id,{method:'DELETE'});
        await load()}catch(e){alert(e.message||'chyba')}}))}
  } else {
    const box=el('div','box');
    box.appendChild(el('h2',null,'Nájsť zákazníka'));
    const inp=el('input');inp.placeholder='hľadaj zákazníka (názov alebo EAN)…';
    const cands=el('div','cands');
    let t=null;
    inp.oninput=()=>{clearTimeout(t);t=setTimeout(async()=>{
      const q=inp.value.trim();cands.textContent='';if(q.length<2)return;
      const d=await api('/api/znalosti/customers?q='+encodeURIComponent(q));
      for(const c of d.items){const e=el('div','cand',c.name+'  ('+c.ean_edi+')');
        e.onclick=()=>{location.href='/znalosti/'+encodeURIComponent(c.ean_edi)};cands.appendChild(e)}
    },200)};
    box.appendChild(inp);box.appendChild(cands);
    W.appendChild(box);
    W.appendChild(productsBox());
    W.appendChild(clientsBox());
__DL_BOXES__  }
  W.appendChild(el('h2',null,'Globálne priradenia (platia pre každého zákazníka)'));
  W.appendChild(addForm((wording,gtin,card)=>
    api('/api/znalosti/global',{method:'POST',body:JSON.stringify({wording:wording,gtin:gtin,card:card})})));
  const g=await api('/api/znalosti/global');
  if(!g.items.length)W.appendChild(el('div','empty','Zatiaľ nič.'));
  for(const it of g.items){W.appendChild(aliasRow(it,async()=>{
    try{await api('/api/znalosti/global/'+it.id,{method:'DELETE'});await load()}
    catch(e){alert(e.message||'chyba')}}))}
}
load();
</script></body></html>"""
