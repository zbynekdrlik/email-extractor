// Produkty sklad + Produkty objednávky tab (#445). Uses the ONE copy of api.js/ui.js
// helpers. Renders one row per catalog card with search (debounce) + paging; an inline
// editor (číslo položky readonly after create, názov, doplnok, + mass/sklad/cena for DL)
// with soft delete (→ toast „Vrátiť v Koši") and a per-card alias manager (add/remove).
// create/update/delete delegate to /api/board/products* (which delegate to the real
// snapshot/dl_snapshot + memory machinery). No HTML strings — all nodes via ui.js `el()`.
// Refresh-safety: an OPEN editor (or the focused search box) is never wiped by the refresh.
import { apiDelete, apiGet, apiPost, clear, debounce, el, toast } from "./ui.js";

const main = document.getElementById("board-main");
const SCOPE = (main && main.dataset.scope) || "orders";
const listEl = document.getElementById("p-list");
const emptyEl = document.getElementById("p-empty");
const searchEl = document.getElementById("p-search");
const newBtn = document.getElementById("p-new");
const prevBtn = document.getElementById("p-prev");
const nextBtn = document.getElementById("p-next");
const pageInfo = document.getElementById("p-pageinfo");

const state = { q: "", page: 0, meta: {} };

function editingOpen() {
  if (searchEl && document.activeElement === searchEl) return true;
  if (!listEl) return false;
  if (listEl.querySelector(".p-editor")) return true;
  for (const inp of listEl.querySelectorAll("input")) {
    if (document.activeElement === inp) return true;
  }
  return false;
}

function upsert(body) {
  return apiPost(`/products?scope=${SCOPE}`, body)
    .then(() => { toast("Uložené"); load(); })
    .catch((e) => toast(e.message, { error: true }));
}

// build the scope's field inputs (name + doplnok [+ mass/sklad/cena for DL]) from meta.fields.
function fieldInputs(card = {}) {
  const inputs = {};
  const rows = (state.meta.fields || []).map((f) => {
    const val = card[f.key] != null ? String(card[f.key]) : "";
    const inp = el("input", { class: `p-f-${f.key}${f.key === "name" ? " p-name" : ""}`,
      type: "text", value: val, autocomplete: "off" });
    inputs[f.key] = inp;
    return el("label", { class: "p-field" }, [f.label + " ", inp]);
  });
  return { inputs, rows };
}

function collect(gtin, inputs) {
  const body = { gtin };
  for (const [k, inp] of Object.entries(inputs)) body[k] = inp.value.trim();
  return body;
}

// ---- one row + its inline editor --------------------------------------------------
function row(card) {
  const extra = SCOPE === "dl" ? (card.doplnok || "") : (card.alias || "");
  const box = el("section", { class: "p-row", "data-gtin": card.gtin });
  box.appendChild(el("div", { class: "p-row-head" }, [
    el("span", { class: "p-gtin-label" }, card.gtin),
    el("span", { class: "p-name-label" }, card.name || ""),
    el("span", { class: "p-extra" }, extra ? `doplnok: ${extra}` : "—"),
    el("button", { class: "p-btn p-edit", type: "button",
      onclick: () => toggleEditor(box, card) }, "Upraviť"),
  ]));
  return box;
}

function toggleEditor(box, card) {
  const open = box.querySelector(".p-editor");
  if (open) { open.remove(); return; }
  // Append the editor SYNCHRONOUSLY — `.p-editor` (with its save/delete buttons) is in the DOM
  // immediately, so `editingOpen()` sees it on the very next refresh tick and never rebuilds
  // the list out from under it. Aliases (a network round-trip) fill in afterwards. Appending
  // AFTER the await (the old shape) let a 15s refresh detach `box` mid-fetch → the editor was
  // appended to a detached node and never became visible (a real race, caught by CI #445).
  box.appendChild(buildEditor(card));
}

function buildEditor(card) {
  const { inputs, rows } = fieldInputs(card);
  const ed = el("div", { class: "p-editor" }, rows);
  ed.appendChild(el("div", { class: "p-editor-actions" }, [
    el("button", { class: "p-btn p-btn--primary p-save", type: "button",
      onclick: () => upsert(collect(card.gtin, inputs)) }, "Uložiť"),
    el("button", { class: "p-btn p-del", type: "button",
      onclick: () => confirmDelete(ed, card.gtin) }, "Zmazať"),
  ]));
  // A placeholder alias block appended NOW keeps the editor a stable node; the real alias
  // section swaps in once its fetch resolves.
  const placeholder = el("div", { class: "p-alias-block" },
    [el("div", { class: "p-alias-loading" }, "Načítavam aliasy…")]);
  ed.appendChild(placeholder);
  apiGet(`/products/${encodeURIComponent(card.gtin)}?scope=${SCOPE}`)
    .then((d) => placeholder.replaceWith(aliasSection(card.gtin, d.aliases || [])))
    .catch((e) => {
      placeholder.replaceWith(aliasSection(card.gtin, []));
      toast(e.message, { error: true });
    });
  return ed;
}

function confirmDelete(ed, gtin) {
  if (ed.querySelector(".p-confirm")) return;
  const cf = el("div", { class: "p-confirm" }, [
    el("span", {}, "Naozaj zmazať kartu? Nájdeš ju v Koši."),
    el("button", { class: "p-btn p-btn--danger p-del-yes", type: "button", onclick: () => {
      apiDelete(`/products/${encodeURIComponent(gtin)}?scope=${SCOPE}`)
        .then(() => { toast("Zmazané — Vrátiť v Koši"); load(); })
        .catch((e) => toast(e.message, { error: true }));
    } }, "Áno, zmazať"),
    el("button", { class: "p-btn", type: "button", onclick: () => cf.remove() }, "Zrušiť"),
  ]);
  ed.appendChild(cf);
}

// ---- alias manager ----------------------------------------------------------------
function aliasSection(gtin, aliases) {
  const listWrap = el("div", { class: "p-aliases" },
    aliases.length ? aliases.map((a) => aliasRow(gtin, a))
      : [el("div", { class: "p-alias-none" }, "Zatiaľ žiadne aliasy.")]);
  const cfg = state.meta.alias || {};
  const wIn = el("input", { class: "p-alias-w", type: "text", autocomplete: "off",
    placeholder: "Nové znenie (alias)" });
  const eIn = cfg.per_customer
    ? el("input", { class: "p-alias-ean", type: "text", autocomplete: "off",
        placeholder: cfg.ean_label || "EAN" })
    : null;
  const addBtn = el("button", { class: "p-btn p-alias-add", type: "button", onclick: () => {
    const wording = wIn.value.trim();
    if (!wording) { toast("Zadaj znenie aliasu", { error: true }); return; }
    const body = { wording };
    if (eIn) body.ean = eIn.value.trim();
    apiPost(`/products/${encodeURIComponent(gtin)}/aliases?scope=${SCOPE}`, body)
      .then(() => { toast("Alias pridaný"); load(); })
      .catch((e) => toast(e.message, { error: true }));
  } }, "Pridať alias");
  return el("div", { class: "p-alias-block" }, [
    el("div", { class: "p-alias-title" }, "Aliasy položky"),
    listWrap,
    el("div", { class: "p-alias-form" }, [wIn, eIn, addBtn].filter(Boolean)),
  ]);
}

function aliasRow(gtin, a) {
  const meta = [a.wording, a.ean ? `(${a.ean})` : null, a.source ? `· ${a.source}` : null]
    .filter(Boolean).join(" ");
  return el("div", { class: "p-alias-row" }, [
    el("span", { class: "p-alias-text" }, meta),
    el("button", { class: "p-btn p-alias-del", type: "button", onclick: () => {
      apiDelete(`/products/${encodeURIComponent(gtin)}/aliases?scope=${SCOPE}`,
        { alias_scope: a.scope, id: a.id, ean: a.ean || "" })
        .then(() => { toast("Alias zmazaný"); load(); })
        .catch((e) => toast(e.message, { error: true }));
    } }, "Zmazať"),
  ]);
}

// ---- new card ---------------------------------------------------------------------
function newCard() {
  if (listEl.querySelector(".p-new-editor")) return;
  const gtinIn = el("input", { class: "p-gtin", type: "text", autocomplete: "off",
    placeholder: "Číslo položky (GTIN)" });
  const { inputs, rows } = fieldInputs();
  const ed = el("div", { class: "p-editor p-new-editor" }, [
    el("label", { class: "p-field" }, ["Číslo položky ", gtinIn]),
    ...rows,
    el("div", { class: "p-editor-actions" }, [
      el("button", { class: "p-btn p-btn--primary p-save", type: "button", onclick: () => {
        const gtin = gtinIn.value.trim();
        if (!gtin) { toast("Zadaj číslo položky", { error: true }); return; }
        upsert(collect(gtin, inputs)).then(() => ed.remove());
      } }, "Uložiť"),
      el("button", { class: "p-btn", type: "button", onclick: () => ed.remove() }, "Zrušiť"),
    ]),
  ]);
  listEl.prepend(ed);
  gtinIn.focus();
}

// ---- load + render ----------------------------------------------------------------
async function load() {
  try {
    const params = new URLSearchParams({ scope: SCOPE, page: String(state.page) });
    if (state.q) params.set("q", state.q);
    const data = await apiGet(`/products?${params.toString()}`);
    state.meta = data.meta || {};
    clear(listEl);
    const items = data.items || [];
    emptyEl.hidden = items.length > 0;
    for (const card of items) listEl.appendChild(row(card));
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
if (newBtn) newBtn.addEventListener("click", newCard);
if (prevBtn) prevBtn.addEventListener("click", () => {
  if (state.page > 0) { state.page -= 1; load(); }
});
if (nextBtn) nextBtn.addEventListener("click", () => {
  if (state.meta.has_more) { state.page += 1; load(); }
});

setInterval(() => { if (!editingOpen()) load(); }, 15000);
load();
