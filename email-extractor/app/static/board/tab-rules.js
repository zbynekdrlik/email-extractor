// Naučené sklad + Naučené objednávky tab (#447). Uses the ONE copy of api.js/ui.js helpers.
// One row per learned rule (ignored mail / item alias / global alias / DL alias / supplier
// memory), grouped by KIND chips, with search (debounce) + paging. Each row shows the origin
// (the question/doc it came from — a link to the Otázky tab filtered to that question, plus an
// inline „Originál" preview) and an inline editor (fields from the list response meta.edit).
// Update + delete delegate to /api/board/rules/<kind>/<id> (which delegate to the engine write
// paths); a delete is soft (→ toast „Vrátiť v Koši"). No HTML strings — all nodes via ui.js el().
import { apiDelete, apiGet, apiPost, clear, debounce, el, toast } from "./ui.js";

const main = document.getElementById("board-main");
const SCOPE = (main && main.dataset.scope) || "orders";
const DEFAULT_KIND = SCOPE === "dl" ? "dl_alias" : "mail";
// The real question-tab slugs (board __init__.TABS) — NOT `otazky-<scope>`, which 404s.
const OTAZKY_SLUG = SCOPE === "dl" ? "otazky-sklad" : "otazky-objednavky";

const chipsEl = document.getElementById("r-chips");
const listEl = document.getElementById("r-list");
const emptyEl = document.getElementById("r-empty");
const searchEl = document.getElementById("r-search");
const prevBtn = document.getElementById("r-prev");
const nextBtn = document.getElementById("r-next");
const pageInfo = document.getElementById("r-pageinfo");

const state = { kind: DEFAULT_KIND, q: "", page: 0, meta: {} };

function editingOpen() {
  if (searchEl && document.activeElement === searchEl) return true;
  if (!listEl) return false;
  if (listEl.querySelector(".r-editor")) return true;
  for (const inp of listEl.querySelectorAll("input, select")) {
    if (document.activeElement === inp) return true;
  }
  return false;
}

// ---- origin (where the rule came from) --------------------------------------------
function originBlock(row) {
  const o = row.origin || {};
  const parts = [];
  if (o.question_id) {
    const href = `/nastenka/${OTAZKY_SLUG}?q=${encodeURIComponent(o.message_id || "")}`
      + "&status=answered";
    parts.push(el("a", { class: "r-origin-link", href }, `otázka #${o.question_id}`));
    parts.push(el("button", { class: "r-btn r-origin-pv", type: "button",
      onclick: (e) => togglePreview(e.target.closest(".r-row"), o.question_id) },
      "Originál"));
  }
  const meta = [o.by ? `kto: ${o.by}` : null, o.source ? `zdroj: ${o.source}` : null,
    o.created_at ? o.created_at.slice(0, 10) : null].filter(Boolean).join(" · ");
  if (meta) parts.push(el("span", { class: "r-origin-meta" }, meta));
  if (!parts.length) parts.push(el("span", { class: "r-origin-meta" }, "—"));
  return el("div", { class: "r-origin" }, parts);
}

async function togglePreview(rowEl, qid) {
  if (!rowEl) return;
  const existing = rowEl.querySelector(".r-preview");
  if (existing) { existing.remove(); return; }
  try {
    const pv = await apiGet(`/questions/${qid}/preview`);
    const atts = (pv.attachments || []).map((a) =>
      el("a", { class: "r-att", href: a.url, target: "_blank", rel: "noopener" },
        a.filename || `príloha ${a.idx}`));
    if (pv.eml_url) atts.push(el("a", { class: "r-att", href: pv.eml_url, target: "_blank",
      rel: "noopener" }, "raw .eml"));
    rowEl.appendChild(el("div", { class: "r-preview" }, [
      el("div", { class: "r-pv-line" }, `Predmet: ${pv.subject || "—"}`),
      el("div", { class: "r-pv-line" }, `Od: ${pv.from_name || ""} <${pv.from_addr || ""}>`),
      el("div", { class: "r-pv-line" }, `Dátum: ${pv.sent_at || pv.created_at || "—"}`),
      atts.length ? el("div", { class: "r-atts" }, atts)
        : el("div", { class: "r-pv-line" }, "Bez príloh"),
    ]));
  } catch (e) { toast(e.message, { error: true }); }
}

// ---- one row + its inline editor --------------------------------------------------
function summary(row) {
  const k = row.key || {};
  const bits = [k.sender, k.email, k.subject_key, k.wording, k.ean && `(${k.ean})`]
    .filter(Boolean).join(" · ");
  const tgt = row.target ? ` → ${row.target}` : "";
  return (bits || "—") + tgt;
}

function row(rule) {
  const box = el("section", { class: "r-row", "data-id": rule.id, "data-kind": rule.kind });
  const head = el("div", { class: "r-row-head" }, [
    el("span", { class: "r-label" }, rule.label || rule.kind),
    el("span", { class: "r-summary" }, summary(rule)),
  ]);
  if (rule.sample_had_attachments === false) {
    head.appendChild(el("span", { class: "r-badge", title: "Vzorka bola bez príloh" },
      "0 príloh vo vzorke"));
  }
  head.appendChild(el("button", { class: "r-btn r-edit", type: "button",
    onclick: () => toggleEditor(box, rule) }, "Upraviť"));
  box.appendChild(head);
  box.appendChild(originBlock(rule));
  return box;
}

function toggleEditor(box, rule) {
  const open = box.querySelector(".r-editor");
  if (open) { open.remove(); return; }
  box.appendChild(buildEditor(rule));
}

function fieldInput(field, values) {
  const val = values[field.key] != null ? String(values[field.key]) : "";
  if (field.options) {
    const sel = el("select", { class: `r-f-${field.key}` },
      field.options.map((o) => el("option",
        { value: o.value, ...(o.value === val ? { selected: "selected" } : {}) }, o.label)));
    return { input: sel, label: el("label", { class: "r-field" }, [field.label + " ", sel]) };
  }
  const inp = el("input", { class: `r-f-${field.key}`, type: "text", value: val,
    autocomplete: "off" });
  return { input: inp, label: el("label", { class: "r-field" }, [field.label + " ", inp]) };
}

function buildEditor(rule) {
  const fields = state.meta.edit || [];
  const inputs = {};
  const rows = fields.map((f) => {
    const { input, label } = fieldInput(f, rule.values || {});
    inputs[f.key] = input;
    return label;
  });
  const ed = el("div", { class: "r-editor" }, rows);
  ed.appendChild(el("div", { class: "r-editor-actions" }, [
    el("button", { class: "r-btn r-btn--primary r-save", type: "button", onclick: () => {
      const body = {};
      for (const [k, inp] of Object.entries(inputs)) body[k] = inp.value.trim();
      apiPost(`/rules/${rule.kind}/${rule.id}`, body)
        .then(() => { toast("Uložené"); load(); })
        .catch((e) => toast(e.message, { error: true }));
    } }, "Uložiť"),
    el("button", { class: "r-btn r-del", type: "button",
      onclick: () => confirmDelete(ed, rule) }, "Zmazať"),
  ]));
  return ed;
}

function confirmDelete(ed, rule) {
  if (ed.querySelector(".r-confirm")) return;
  const cf = el("div", { class: "r-confirm" }, [
    el("span", {}, "Naozaj zmazať pravidlo? Nájdeš ho v Koši."),
    el("button", { class: "r-btn r-btn--danger r-del-yes", type: "button", onclick: () => {
      apiDelete(`/rules/${rule.kind}/${rule.id}`)
        .then(() => { toast("Zmazané — Vrátiť v Koši"); load(); })
        .catch((e) => toast(e.message, { error: true }));
    } }, "Áno, zmazať"),
    el("button", { class: "r-btn", type: "button", onclick: () => cf.remove() }, "Zrušiť"),
  ]);
  ed.appendChild(cf);
}

// ---- kind chips -------------------------------------------------------------------
function renderChips() {
  if (!chipsEl) return;
  clear(chipsEl);
  for (const k of state.meta.kinds || []) {
    chipsEl.appendChild(el("button", {
      class: `r-chip${k.slug === state.kind ? " is-active" : ""}`, type: "button",
      role: "tab", "aria-selected": String(k.slug === state.kind),
      onclick: () => {
        if (k.slug === state.kind) return;
        state.kind = k.slug; state.page = 0; load();
      },
    }, k.label));
  }
}

// ---- load + render ----------------------------------------------------------------
async function load() {
  try {
    const params = new URLSearchParams({ scope: SCOPE, kind: state.kind,
      page: String(state.page) });
    if (state.q) params.set("q", state.q);
    const data = await apiGet(`/rules?${params.toString()}`);
    state.meta = data.meta || {};
    renderChips();
    clear(listEl);
    const items = data.items || [];
    emptyEl.hidden = items.length > 0;
    for (const rule of items) listEl.appendChild(row(rule));
    renderPager();
  } catch (e) { toast(e.message, { error: true }); }
}

function renderPager() {
  const m = state.meta;
  const total = m.total || 0;
  const size = m.page_size || 50;
  const from = total ? state.page * size + 1 : 0;
  const to = Math.min(total, (state.page + 1) * size);
  pageInfo.textContent = total ? `${from}–${to} z ${total}` : "";
  prevBtn.hidden = state.page <= 0;
  nextBtn.hidden = !m.has_more;
}

// ---- wiring -----------------------------------------------------------------------
if (searchEl) {
  searchEl.addEventListener("input", debounce(() => {
    state.q = searchEl.value.trim(); state.page = 0; load();
  }, 300));
}
if (prevBtn) prevBtn.addEventListener("click", () => {
  if (state.page > 0) { state.page -= 1; load(); }
});
if (nextBtn) nextBtn.addEventListener("click", () => {
  if (state.meta.has_more) { state.page += 1; load(); }
});

setInterval(() => { if (!editingOpen()) load(); }, 15000);
load();
