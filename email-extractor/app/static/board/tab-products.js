// Produkty sklad + Produkty objednávky tab (#445). Uses the ONE copy of api.js/ui.js
// helpers. Renders one row per catalog card with search (debounce) + paging; an inline
// editor (číslo položky readonly after create, názov, doplnok, + mass/sklad/cena for DL)
// with soft delete (→ toast „Vrátiť v Koši") and a per-card alias manager (add/remove).
// create/update/delete delegate to /api/board/products* (which delegate to the real
// snapshot/dl_snapshot + memory machinery). No HTML strings — all nodes via ui.js `el()`.
// Refresh-safety: an OPEN editor (or the focused search box) is never wiped by the refresh.
// #467 (Produkty sklad): a card number CODEX has no stock card for is refused by the server —
// the editor then lists CODEX cards with a similar name + code; drifted names get a badge.
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
// #467: Produkty sklad only — the CODEX stock-card list status + the "only CODEX mismatches"
// filter (drifted name / number CODEX has no stock card for).
const codexFilterEl = document.querySelector(".p-codex-filter");
const codexIssuesEl = document.getElementById("p-codex-issues");
const codexStatusEl = document.getElementById("p-codex-status");

const state = { q: "", page: 0, meta: {}, codexIssues: false };

function editingOpen() {
  if (searchEl && document.activeElement === searchEl) return true;
  if (!listEl) return false;
  if (listEl.querySelector(".p-editor")) return true;
  for (const inp of listEl.querySelectorAll("input")) {
    if (document.activeElement === inp) return true;
  }
  return false;
}

// Resolves true when saved. A refusal that carries structured help (#467: `codex.similar` — the
// number is not a CODEX stock card; `existing` — the number already has a card) is rendered
// into the editor that sent it, so the editor (and what was typed) stays open.
async function upsert(body, ed = null, gtinIn = null) {
  try {
    await apiPost(`/products?scope=${SCOPE}`, body);
    toast("Uložené");
    load();
    return true;
  } catch (e) {
    if (ed && e.data && (e.data.codex || e.data.existing)) codexHint(ed, e.data, gtinIn);
    toast(e.message, { error: true });
    return false;
  }
}

// ---- #467: CODEX help inside an editor --------------------------------------------
function findCard(ed, code) {
  if (ed.classList.contains("p-new-editor")) ed.remove();
  if (searchEl) searchEl.value = code;
  state.q = code;
  state.page = 0;
  load();
}

function codexHint(ed, data, gtinIn) {
  const old = ed.querySelector(".p-codex-hint");
  if (old) old.remove();
  const rows = [];
  if (data.existing) {
    rows.push(el("div", { class: "p-codex-row" }, [
      el("span", {}, `${data.existing.gtin} — ${data.existing.name}`),
      el("button", { class: "p-btn p-codex-find", type: "button", "data-code": data.existing.gtin,
        onclick: () => findCard(ed, data.existing.gtin) }, "Nájsť kartu v zozname"),
    ]));
  }
  const similar = (data.codex && data.codex.similar) || [];
  for (const s of similar) {
    let btn = null;
    if (s.in_catalog) {
      btn = el("button", { class: "p-btn p-codex-find", type: "button", "data-code": s.code,
        onclick: () => findCard(ed, s.code) }, "Nájsť kartu v zozname");
    } else if (gtinIn) {
      btn = el("button", { class: "p-btn p-codex-use", type: "button", "data-code": s.code,
        onclick: () => {
          gtinIn.value = s.code;
          const nameIn = ed.querySelector(".p-name");
          if (nameIn && !nameIn.value.trim()) nameIn.value = s.name;
        } }, "Použiť kód");
    }
    const ours = s.in_catalog ? ` (u nás: ${s.catalog_name})` : "";
    rows.push(el("div", { class: "p-codex-row" }, [
      el("span", {}, `${s.code} — ${s.name}${ours}`), btn]));
  }
  if (data.codex && !similar.length) {
    rows.push(el("div", { class: "p-codex-row" },
      "V CODEXe sme nenašli kartu s podobným názvom — skontroluj EAN kód karty v CODEXe."));
  }
  ed.appendChild(el("div", { class: "p-codex-hint" }, [
    el("div", { class: "p-codex-msg" }, data.error || ""), ...rows]));
}

function codexBadge(card) {
  const cx = card.codex || {};
  if (cx.status === "drift") {
    return el("span", { class: "p-codex p-codex--drift",
      title: "Názov karty v CODEXe je iný — skontroluj, či je to tá istá karta" },
    `CODEX: ${cx.name}`);
  }
  if (cx.status === "missing") {
    return el("span", { class: "p-codex p-codex--missing",
      title: "CODEX nemá skladovú kartu s týmto EAN kódom — dodací list s ňou odmietne" },
    "⚠ kód v CODEXe neexistuje");
  }
  return null;
}

function renderCodexStatus() {
  if (SCOPE !== "dl") return;
  const m = state.meta.codex || {};
  if (codexFilterEl) codexFilterEl.hidden = false;
  if (!codexStatusEl) return;
  codexStatusEl.hidden = false;
  codexStatusEl.classList.toggle("is-warn", !m.active);
  if (m.never) {
    codexStatusEl.textContent = "Zoznam kariet z CODEXu ešte neprišiel — kontrola kódov je vypnutá";
  } else if (m.stale) {
    codexStatusEl.textContent =
      `⚠ Zoznam kariet z CODEXu je zastaraný (stav k ${m.as_of_local}) — kontrola kódov je vypnutá`;
  } else {
    codexStatusEl.textContent = `Karty z CODEXu: stav k ${m.as_of_local} (${m.codes} kódov)`;
  }
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
    codexBadge(card),
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
  const cx = card.codex || {};
  ed.appendChild(el("div", { class: "p-editor-actions" }, [
    el("button", { class: "p-btn p-btn--primary p-save", type: "button",
      onclick: () => upsert(collect(card.gtin, inputs), ed) }, "Uložiť"),
    // #467: one click takes CODEX's name for a card whose name drifted (then „Uložiť").
    cx.status === "drift" && inputs.name
      ? el("button", { class: "p-btn p-codex-take", type: "button",
        onclick: () => { inputs.name.value = cx.name; } }, "Prevziať názov z CODEXu")
      : null,
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
      el("button", { class: "p-btn p-btn--primary p-save", type: "button", onclick: async () => {
        const gtin = gtinIn.value.trim();
        if (!gtin) { toast("Zadaj číslo položky", { error: true }); return; }
        // `new: true` — a number that already has a card is refused (never overwritten); the
        // editor stays open on any refusal so the hint + what was typed are not lost (#467).
        if (await upsert({ ...collect(gtin, inputs), new: true }, ed, gtinIn)) ed.remove();
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
    if (SCOPE === "dl" && state.codexIssues) params.set("codex", "issues");
    const data = await apiGet(`/products?${params.toString()}`);
    state.meta = data.meta || {};
    renderCodexStatus();
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
if (codexIssuesEl) {
  codexIssuesEl.addEventListener("change", () => {
    state.codexIssues = codexIssuesEl.checked; state.page = 0; load();
  });
}
if (prevBtn) prevBtn.addEventListener("click", () => {
  if (state.page > 0) { state.page -= 1; load(); }
});
if (nextBtn) nextBtn.addEventListener("click", () => {
  if (state.meta.has_more) { state.page += 1; load(); }
});

setInterval(() => { if (!editingOpen()) load(); }, 15000);
load();
