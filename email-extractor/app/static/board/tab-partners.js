// Zákazníci + Dodávatelia tab (#446 lane 5). ONE module for both partner tabs — the scope
// (customers|suppliers) from #board-main[data-scope] picks the board API + field set. Uses
// the ONE copy of api.js/ui.js helpers; no HTML strings (every node built via el(), spec §3).
// Customers render grouped into multi-site families (#435); create/update/delete delegate to
// /api/board/<scope> (which delegate to the real snapshot/dl_snapshot engines). Refresh-safety:
// the periodic refresh is skipped while the editor form is open or the search box is focused.
import { apiDelete, apiGet, apiPost, clear, debounce, el, toast } from "./ui.js";

const main = document.getElementById("board-main");
const SCOPE = (main && main.dataset.scope) || "customers";

const CFG = {
  customers: {
    api: "/customers", grouped: true,
    idKeys: ["override_id", "orig_ean_edi", "orig_street"],
    fields: [["ean_edi", "EAN EDI"], ["name", "Názov"], ["city", "Mesto"],
             ["street", "Ulica"], ["zip", "PSČ"], ["emails", "E-maily (čiarkou)"]],
    count: (r) => `${r.orders_shipped || 0}× objednávka`,
  },
  suppliers: {
    api: "/suppliers", grouped: false, invoiceFlag: true,
    idKeys: ["override_id", "orig_ean_edi", "orig_city"],
    fields: [["ean_edi", "EAN EDI"], ["name", "Názov"], ["city", "Mesto"],
             ["emails", "E-maily (čiarkou)"]],
    count: (r) => `${r.dls_shipped || 0}× dodací list`,
  },
}[SCOPE];

const $ = (id) => document.getElementById(id);
const listEl = $("p-list"), emptyEl = $("p-empty"), editorEl = $("p-editor"),
  searchEl = $("p-search");
const state = { q: "", page: 0, total: 0, sort: "name", desc: false, size: 100 };

function editorOpen() {
  return !editorEl.hidden || (searchEl && document.activeElement === searchEl);
}

// ---- editor form (add / edit) -----------------------------------------------------------
function showEditor(row) {
  editorEl.hidden = false;
  clear(editorEl);
  const inputs = {};
  const grid = el("div", { class: "p-form-grid" });
  for (const [name, label] of CFG.fields) {
    const val = row ? (name === "emails" ? (row.emails || []).join(", ") : (row[name] ?? "")) : "";
    const inp = el("input", { class: "p-in", type: "text", value: String(val),
      autocomplete: "off" });
    inputs[name] = inp;
    grid.appendChild(el("label", { class: "p-field" }, [label, inp]));
  }
  let invoiceInp = null;
  if (CFG.invoiceFlag) {
    invoiceInp = el("input", { type: "checkbox" });
    if (row && row.invoice_is_delivery_note) invoiceInp.checked = true;
    grid.appendChild(el("label", { class: "p-field p-field--check" },
      [invoiceInp, "Faktúra = dodací list"]));
  }
  editorEl.appendChild(el("h2", { class: "p-editor-title" },
    row ? "Upraviť kartu" : "Nová karta"));
  editorEl.appendChild(grid);
  editorEl.appendChild(el("div", { class: "p-form-actions" }, [
    el("button", { class: "p-btn p-btn--primary", type: "button",
      onclick: () => saveForm(row, inputs, invoiceInp) }, "Uložiť"),
    el("button", { class: "p-btn", type: "button", onclick: hideEditor }, "Zrušiť"),
  ]));
  (inputs.name || inputs.ean_edi).focus();
}

function hideEditor() { editorEl.hidden = true; clear(editorEl); }

async function saveForm(row, inputs, invoiceInp) {
  const body = {};
  for (const [name] of CFG.fields) body[name] = inputs[name].value.trim();
  if (invoiceInp) body.invoice_is_delivery_note = invoiceInp.checked;
  if (row) for (const k of CFG.idKeys) if (row[k] != null) body[k] = row[k];
  try {
    await apiPost(CFG.api, body);
    toast("Uložené.");
    hideEditor();
    load();
  } catch (e) { toast(e.message || "Uloženie zlyhalo.", { error: true }); }
}

async function doDelete(row) {
  if (!window.confirm(`Zmazať „${row.name}"? Vrátiť sa dá v Koši.`)) return;
  const body = {};
  for (const k of CFG.idKeys) if (row[k] != null) body[k] = row[k];
  try {
    await apiDelete(CFG.api, body);
    toast("Zmazané. Vrátiť v Koši.");
    load();
  } catch (e) { toast(e.message || "Mazanie zlyhalo.", { error: true }); }
}

// ---- table (#504): one compact row per card, sortable column headers ------------------
// Columns: [sort key or null, header]. The sort runs on the SERVER (`?sort=&dir=`) so the
// paging matches the order across every page, never just the rows already on screen.
const COLS = [["ean", "EAN EDI"], ["name", "Názov"],
  ["city", SCOPE === "customers" ? "Adresa" : "Mesto"], [null, "E-maily"],
  ["count", SCOPE === "customers" ? "Objednávky" : "Dodacie listy"], [null, ""]];

function headerRow() {
  return el("tr", {}, COLS.map(([key, label]) => {
    if (!key) return el("th", { class: "p-th" }, label);
    const active = state.sort === key;
    const arrow = active ? (state.desc ? " ▼" : " ▲") : "";
    return el("th", { class: "p-th p-th--sort" + (active ? " is-active" : ""),
      "data-sort": key, "aria-sort": active ? (state.desc ? "descending" : "ascending") : "none" },
    [el("button", { class: "p-sort", type: "button", onclick: () => sortBy(key) },
      label + arrow)]);
  }));
}

function sortBy(key) {
  if (state.sort === key) state.desc = !state.desc;
  else { state.sort = key; state.desc = key === "count"; }   // counts: the busiest first
  state.page = 0;
  remember();
  load();
}

function siteRow(row, inFamily) {
  const nameCell = [el("div", { class: "p-name" }, row.name || "—")];
  if (row.site_total > 1)
    nameCell.push(el("span", { class: "p-site" },
      `prevádzka ${row.site_index} z ${row.site_total}`));
  if (row.invoice_is_delivery_note)
    nameCell.push(el("span", { class: "p-tag" }, "Faktúra = dodací list"));
  const addr = SCOPE === "customers"
    ? [row.street, row.zip, row.city].filter(Boolean).join(", ") : (row.city || "");
  return el("tr", { class: "p-row" + (inFamily ? " p-row--site" : "") }, [
    el("td", { class: "p-ean" }, row.ean_edi || "—"),
    el("td", { class: "p-cell-name" }, nameCell),
    el("td", { class: "p-meta" }, addr || "—"),
    el("td", { class: "p-meta p-emails" }, (row.emails || []).join(", ")),
    el("td", { class: "p-count" }, CFG.count(row)),
    el("td", { class: "p-cardactions" }, [
      el("button", { class: "p-btn", type: "button", onclick: () => showEditor(row) }, "Upraviť"),
      el("button", { class: "p-btn p-btn--del", type: "button",
        onclick: () => doDelete(row) }, "Zmazať"),
    ]),
  ]);
}

function renderTable(items) {
  const body = el("tbody");
  for (const it of items) {
    if (CFG.grouped && it.size > 1) {
      body.appendChild(el("tr", { class: "p-family-row" }, [
        el("td", { class: "p-family", colspan: String(COLS.length) },
          `${it.label} — ${it.size} prevádzok`)]));
      for (const s of it.sites) body.appendChild(siteRow(s, true));
    } else {
      body.appendChild(siteRow(CFG.grouped ? it.sites[0] : it, false));
    }
  }
  listEl.appendChild(el("table", { class: "p-table" }, [el("thead", {}, headerRow()), body]));
}

// ---- remembered view (per browser; storage may be blocked — never required) ---------
const VIEW_KEY = `board-partners-view-${SCOPE}`;
function remember() {
  try {
    localStorage.setItem(VIEW_KEY, JSON.stringify(
      { sort: state.sort, desc: state.desc, size: state.size }));
  } catch (e) { /* private window / blocked storage: the view just is not remembered */ }
}
function recall() {
  try {
    const v = JSON.parse(localStorage.getItem(VIEW_KEY) || "null");
    if (v && typeof v === "object") {
      if (typeof v.sort === "string") state.sort = v.sort;
      state.desc = !!v.desc;
      if (Number.isInteger(v.size)) state.size = v.size;
    }
  } catch (e) { /* see remember() */ }
}

// ---- load + render ----------------------------------------------------------------------
async function load() {
  const params = new URLSearchParams();
  if (state.q) params.set("q", state.q);
  params.set("page", String(state.page));
  params.set("sort", state.sort);
  params.set("dir", state.desc ? "desc" : "asc");
  params.set("size", String(state.size));
  let res;
  try {
    res = await apiGet(`${CFG.api}?${params.toString()}`);
  } catch (e) { toast(e.message || "Načítanie zlyhalo.", { error: true }); return; }
  state.total = res.total || 0;
  state.sort = res.sort || state.sort;
  state.desc = !!res.desc;
  state.size = res.page_size ?? state.size;
  state.page = res.page || 0;
  if (sizeEl) sizeEl.value = String(state.size);
  clear(listEl);
  const items = CFG.grouped ? (res.families || []) : (res.suppliers || []);
  emptyEl.hidden = items.length > 0;
  if (items.length) renderTable(items);
  const pages = state.size ? Math.max(1, Math.ceil(state.total / state.size)) : 1;
  const unit = CFG.grouped && state.sort === "name" ? "rodín" : "záznamov";
  const rows = CFG.grouped && res.rows != null && unit === "rodín" ? ` (${res.rows} prevádzok)` : "";
  $("p-page-info").textContent = state.total
    ? `Strana ${state.page + 1} z ${pages} · ${state.total} ${unit}${rows}` : "";
  $("p-prev").disabled = state.page <= 0;
  $("p-next").disabled = state.page >= pages - 1;
}

// ---- wiring -----------------------------------------------------------------------------
const sizeEl = $("p-size");
$("p-add").addEventListener("click", () => showEditor(null));
searchEl.addEventListener("input", debounce(() => {
  state.q = searchEl.value.trim(); state.page = 0; load();
}, 300));
if (sizeEl) sizeEl.addEventListener("change", () => {
  state.size = parseInt(sizeEl.value, 10) || 0; state.page = 0; remember(); load();
});
$("p-prev").addEventListener("click", () => { if (state.page > 0) { state.page -= 1; load(); } });
$("p-next").addEventListener("click", () => { state.page += 1; load(); });
setInterval(() => { if (!editorOpen()) load(); }, 15000);
recall();

// #449 lane 8: honour a `?q=<term>` deep link (the retired /znalosti/<ean> page now
// redirects to /nastenka/zakaznici?q=<ean>) — seed the search box + filter so the tab
// opens already narrowed to that customer. Additive: no param = the unchanged default.
const _initQ = new URLSearchParams(location.search).get("q");
if (_initQ) { state.q = _initQ.trim(); if (searchEl) searchEl.value = state.q; }
load();
