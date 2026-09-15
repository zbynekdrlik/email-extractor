// História objednávok + História dodacích listov tab (#448 lane 7). ONE module for both
// history tabs — the scope (orders|dl) from #board-main[data-scope] picks the documents and
// labels. Uses the ONE copy of api.js/ui.js helpers; no HTML strings (every node via el()).
// The list is READ-only over order_runs/order_items/email_events; the detail exposes the three
// sanctioned actions (spustiť znova / zadané ručne / doučiť) which the server re-guards. The
// card picker reuses the lane-4 /api/board/products catalog search. Refresh-safety: the periodic
// list refresh is skipped while the detail drawer is open or the search box is focused.
import { apiGet, apiPost, clear, debounce, el, toast } from "./ui.js";

const main = document.getElementById("board-main");
const SCOPE = (main && main.dataset.scope) || "orders";
const $ = (id) => document.getElementById(id);
const enc = (s) => encodeURIComponent(String(s));

const listEl = $("h-list"), emptyEl = $("h-empty"), chipsEl = $("h-status-chips"),
  searchEl = $("h-search"), fromEl = $("h-from"), toEl = $("h-to"),
  drawerEl = $("h-drawer"), detailEl = $("h-detail");
const state = { q: "", status: "", from: "", to: "", page: 0, total: 0, pageSize: 25,
  labels: {}, openMid: null };

function drawerOpen() { return !drawerEl.hidden; }
function busy() {
  return drawerOpen() || (searchEl && document.activeElement === searchEl);
}

// ---- status chips ----------------------------------------------------------------------
function renderChips(chips) {
  if (chipsEl.childElementCount) return;   // build once
  for (const ch of chips || []) {
    const b = el("button", { class: "h-chip", type: "button",
      onclick: () => { state.status = ch.value; state.page = 0; syncChips(); load(); } });
    b.dataset.value = ch.value;
    b.textContent = ch.label;
    chipsEl.appendChild(b);
  }
  syncChips();
}
function syncChips() {
  for (const b of chipsEl.children)
    b.classList.toggle("is-active", b.dataset.value === state.status);
}

// ---- list ------------------------------------------------------------------------------
function row(it) {
  const cells = [
    el("span", { class: "h-c h-c-date" }, (it.date || "").slice(0, 10)),
    el("span", { class: "h-c h-c-partner" }, it.partner || "—"),
    el("span", { class: "h-c h-c-doc" }, it.doc_number || "—"),
    el("span", { class: `h-c h-c-status h-st-${it.proc_status || "na"}` }, it.status_label),
    el("span", { class: "h-c h-c-subject" }, it.subject || ""),
  ];
  return el("button", { class: "h-row", type: "button",
    onclick: () => openDetail(it.message_id) }, cells);
}

async function load() {
  const p = new URLSearchParams({ scope: SCOPE, page: String(state.page) });
  if (state.q) p.set("q", state.q);
  if (state.status) p.set("status", state.status);
  if (state.from) p.set("from", state.from);
  if (state.to) p.set("to", state.to);
  let res;
  try {
    res = await apiGet(`/history?${p.toString()}`);
  } catch (e) { toast(e.message || "Načítanie zlyhalo.", { error: true }); return; }
  const meta = res.meta || {};
  state.total = meta.total || 0;
  state.pageSize = meta.page_size || 25;
  state.labels = meta.labels || {};
  renderChips(meta.statuses);
  clear(listEl);
  const items = res.items || [];
  emptyEl.hidden = items.length > 0;
  emptyEl.textContent = state.labels.empty || "Nič sa nenašlo.";
  for (const it of items) listEl.appendChild(row(it));
  const pages = Math.max(1, Math.ceil(state.total / state.pageSize));
  $("h-page-info").textContent = state.total
    ? `Strana ${state.page + 1} z ${pages} · ${state.total} dokladov` : "";
  $("h-prev").disabled = state.page <= 0;
  $("h-next").disabled = state.page >= pages - 1;
}

// ---- detail ----------------------------------------------------------------------------
async function openDetail(mid) {
  state.openMid = mid;
  let d;
  try {
    d = await apiGet(`/history/${enc(mid)}?scope=${SCOPE}`);
  } catch (e) { toast(e.message || "Detail sa nenačítal.", { error: true }); return; }
  renderDetail(d);
  drawerEl.hidden = false;
  drawerEl.scrollIntoView({ block: "start" });
}

function closeDetail() { drawerEl.hidden = true; state.openMid = null; clear(detailEl); load(); }

function metaLine(label, value) {
  return el("div", { class: "h-meta" }, [
    el("span", { class: "h-meta-k" }, label + ": "),
    el("span", { class: "h-meta-v" }, value || "—")]);
}

function actionButton(kind, spec, mid) {
  const btn = el("button", { class: `h-btn h-btn--${kind}`, type: "button",
    title: spec.reason || "" }, kind === "rerun" ? "↻ Spustiť znova" : "✎ Zadané ručne");
  if (!spec.allowed) { btn.disabled = true; }
  else btn.addEventListener("click", () => runAction(kind, mid));
  return el("div", { class: "h-action" }, [btn,
    el("span", { class: "h-action-hint" }, spec.reason || "")]);
}

async function runAction(kind, mid) {
  const confirmMsg = kind === "rerun"
    ? "Spustiť tento doklad znova? (spraví sa len ak sa doklad nikdy nenahral do ORIONu)"
    : "Označiť ako zadané ručne? Objednávka sa uvoľní bez odoslania do ORIONu.";
  if (!window.confirm(confirmMsg)) return;
  try {
    await apiPost(`/history/${enc(mid)}/${kind}?scope=${SCOPE}`, {});
    toast(kind === "rerun" ? "Spustené znova." : "Označené ako zadané ručne.");
    openDetail(mid);
  } catch (e) { toast(e.message || "Akcia zlyhala.", { error: true }); }
}

function itemRow(it, mid) {
  const trace = [
    it.card ? `karta: ${it.card}` : "bez karty",
    it.rule ? `pravidlo: ${it.rule}` : null,
    (it.confidence != null) ? `istota: ${Math.round(it.confidence * 100)}%` : null,
  ].filter(Boolean).join(" · ");
  const teachWrap = el("div", { class: "h-teach", hidden: true });
  const teachBtn = el("button", { class: "h-btn h-btn--ghost h-teach-btn", type: "button",
    onclick: () => { teachWrap.hidden = !teachWrap.hidden;
      if (!teachWrap.hidden) buildPicker(teachWrap, it, mid); } }, "✏️ Doučiť");
  return el("div", { class: "h-item" }, [
    el("div", { class: "h-item-main" }, [
      el("span", { class: "h-item-name" }, it.name || "—"),
      el("span", { class: "h-item-qty" },
        `${it.quantity != null ? it.quantity : ""} ${it.unit || ""}`.trim()),
      teachBtn]),
    el("div", { class: "h-item-trace" }, trace),
    teachWrap]);
}

function buildPicker(wrap, item, mid) {
  clear(wrap);
  const search = el("input", { class: "h-pick-search", type: "search", autocomplete: "off",
    placeholder: "Hľadať kartu (názov / číslo položky)…" });
  const results = el("div", { class: "h-pick-results" });
  wrap.appendChild(el("div", { class: "h-pick-title" },
    `Doučiť „${item.name || ""}“ na správnu kartu:`));
  wrap.appendChild(search);
  wrap.appendChild(results);
  const run = debounce(async () => {
    const q = search.value.trim();
    if (!q) { clear(results); return; }
    let res;
    try { res = await apiGet(`/products?scope=${SCOPE}&q=${enc(q)}`); }
    catch (e) { toast(e.message || "Hľadanie zlyhalo.", { error: true }); return; }
    clear(results);
    for (const card of (res.items || []).slice(0, 12)) {
      results.appendChild(el("button", { class: "h-pick-opt", type: "button",
        onclick: () => doTeach(item, mid, card) },
        `${card.name || "—"} — ${card.gtin}`));
    }
    if (!(res.items || []).length)
      results.appendChild(el("div", { class: "h-pick-none" }, "Nič sa nenašlo."));
  }, 300);
  search.addEventListener("input", run);
  search.focus();
}

async function doTeach(item, mid, card) {
  try {
    await apiPost(`/history/${enc(mid)}/teach?scope=${SCOPE}`,
      { name: item.name, gtin: card.gtin, card: card.name || "" });
    toast(`Doučené: „${item.name}“ → ${card.name || card.gtin}.`);
    openDetail(mid);
  } catch (e) { toast(e.message || "Doučenie zlyhalo.", { error: true }); }
}

function timeline(events) {
  const wrap = el("div", { class: "h-timeline" });
  for (const e of events || []) {
    wrap.appendChild(el("div", { class: "h-tl" }, [
      el("span", { class: "h-tl-ts" }, (e.ts || "").slice(0, 19).replace("T", " ")),
      el("span", { class: "h-tl-stage" }, `${e.stage || ""} / ${e.status || ""}`),
      el("span", { class: "h-tl-out" }, e.outcome || "")]));
  }
  return wrap;
}

function renderDetail(d) {
  clear(detailEl);
  const lbl = d.labels || {};
  detailEl.appendChild(el("h2", { class: "h-detail-title" }, d.subject || "(bez predmetu)"));
  detailEl.appendChild(el("div", { class: "h-detail-meta" }, [
    metaLine("Stav", d.status_label),
    metaLine(lbl.partner || "Partner",
      d.partner ? `${d.partner.name}${d.partner.ean ? " (" + d.partner.ean + ")" : ""}` : "—"),
    metaLine(lbl.doc || "Doklad", (d.doc_numbers || []).join(", ")),
    metaLine("Dátum", (d.date || "").slice(0, 19).replace("T", " ")),
    d.outcome ? metaLine("Výsledok", d.outcome) : null,
  ].filter(Boolean)));

  // actions
  detailEl.appendChild(el("div", { class: "h-actions" }, [
    actionButton("rerun", d.rerun || {}, d.message_id),
    actionButton("manual", d.manual || {}, d.message_id)]));

  // items
  detailEl.appendChild(el("h3", { class: "h-sec" }, "Položky a párovanie"));
  const items = d.items || [];
  if (items.length)
    detailEl.appendChild(el("div", { class: "h-items" }, items.map((it) => itemRow(it, d.message_id))));
  else
    detailEl.appendChild(el("p", { class: "h-empty2" }, "Žiadne položky (doklad nemá záznam behu)."));

  // original
  if ((d.attachments || []).length || d.eml_url) {
    detailEl.appendChild(el("h3", { class: "h-sec" }, "Originál"));
    const orig = el("div", { class: "h-orig" });
    for (const a of d.attachments || [])
      orig.appendChild(el("a", { class: "h-orig-link", href: a.url, target: "_blank",
        rel: "noopener" }, a.filename || `príloha ${a.idx}`));
    if (d.eml_url)
      orig.appendChild(el("a", { class: "h-orig-link", href: d.eml_url, target: "_blank",
        rel: "noopener" }, "e-mail (.eml)"));
    detailEl.appendChild(orig);
  }

  // timeline
  detailEl.appendChild(el("h3", { class: "h-sec" }, "Priebeh"));
  detailEl.appendChild(timeline(d.events));
}

// ---- wiring ----------------------------------------------------------------------------
searchEl.addEventListener("input", debounce(() => {
  state.q = searchEl.value.trim(); state.page = 0; load();
}, 300));
fromEl.addEventListener("change", () => { state.from = fromEl.value; state.page = 0; load(); });
toEl.addEventListener("change", () => { state.to = toEl.value; state.page = 0; load(); });
$("h-prev").addEventListener("click", () => { if (state.page > 0) { state.page -= 1; load(); } });
$("h-next").addEventListener("click", () => { state.page += 1; load(); });
$("h-close").addEventListener("click", closeDetail);
setInterval(() => { if (!busy()) load(); }, 20000);
load();
