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
const state = { q: "", page: 0, total: 0, pageSize: 50 };

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

// ---- one card ---------------------------------------------------------------------------
function card(row) {
  const lines = [
    el("div", { class: "p-name" }, row.name || "—"),
    el("div", { class: "p-meta" }, `EAN ${row.ean_edi || "—"}`),
  ];
  const addr = [row.street, row.zip, row.city].filter(Boolean).join(", ");
  if (addr) lines.push(el("div", { class: "p-meta" }, addr));
  else if (row.city) lines.push(el("div", { class: "p-meta" }, row.city));
  if ((row.emails || []).length)
    lines.push(el("div", { class: "p-meta" }, (row.emails || []).join(", ")));
  if (row.invoice_is_delivery_note)
    lines.push(el("div", { class: "p-tag" }, "Faktúra = dodací list"));
  const foot = [el("span", { class: "p-count" }, CFG.count(row))];
  if (row.site_total > 1)
    foot.unshift(el("span", { class: "p-site" }, `prevádzka ${row.site_index} z ${row.site_total}`));
  lines.push(el("div", { class: "p-cardfoot" }, foot));
  lines.push(el("div", { class: "p-cardactions" }, [
    el("button", { class: "p-btn", type: "button", onclick: () => showEditor(row) }, "Upraviť"),
    el("button", { class: "p-btn p-btn--del", type: "button",
      onclick: () => doDelete(row) }, "Zmazať"),
  ]));
  return el("article", { class: "p-card" }, lines);
}

function renderFamilies(families) {
  for (const fam of families) {
    if (fam.size > 1) {
      listEl.appendChild(el("h2", { class: "p-family" },
        `${fam.label} — ${fam.size} prevádzok`));
      const wrap = el("div", { class: "p-family-sites" }, fam.sites.map(card));
      listEl.appendChild(wrap);
    } else {
      listEl.appendChild(card(fam.sites[0]));
    }
  }
}

// ---- load + render ----------------------------------------------------------------------
async function load() {
  const params = new URLSearchParams();
  if (state.q) params.set("q", state.q);
  params.set("page", String(state.page));
  let res;
  try {
    res = await apiGet(`${CFG.api}?${params.toString()}`);
  } catch (e) { toast(e.message || "Načítanie zlyhalo.", { error: true }); return; }
  state.total = res.total || 0;
  state.pageSize = res.page_size || 50;
  clear(listEl);
  const items = CFG.grouped ? (res.families || []) : (res.suppliers || []);
  emptyEl.hidden = items.length > 0;
  if (CFG.grouped) renderFamilies(items);
  else for (const r of items) listEl.appendChild(card(r));
  const pages = Math.max(1, Math.ceil(state.total / state.pageSize));
  $("p-page-info").textContent = state.total
    ? `Strana ${state.page + 1} z ${pages} · ${state.total} ${CFG.grouped ? "rodín" : "dodávateľov"}` : "";
  $("p-prev").disabled = state.page <= 0;
  $("p-next").disabled = state.page >= pages - 1;
}

// ---- wiring -----------------------------------------------------------------------------
$("p-add").addEventListener("click", () => showEditor(null));
searchEl.addEventListener("input", debounce(() => {
  state.q = searchEl.value.trim(); state.page = 0; load();
}, 300));
$("p-prev").addEventListener("click", () => { if (state.page > 0) { state.page -= 1; load(); } });
$("p-next").addEventListener("click", () => { state.page += 1; load(); });
setInterval(() => { if (!editorOpen()) load(); }, 15000);
load();
