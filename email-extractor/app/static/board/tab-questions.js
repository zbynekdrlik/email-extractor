// Otázky sklad + Otázky objednávky tab (#443). Uses the ONE copy of api.js/ui.js helpers.
// Renders one card per question with the SAME choices the old boards offer; answers/undo
// delegate to /api/board/questions/<id>/answer|undo (which delegate to the real teach/hold
// machinery). Expired → „Znovu otvoriť"; answered → „Vrátiť odpoveď". No HTML strings — all
// nodes are built via ui.js `el()`. Refresh-safety: a card being edited (data-open) is never
// wiped by the periodic refresh.
import { apiGet, apiPost, clear, debounce, el, toast } from "./ui.js";

const main = document.getElementById("board-main");
const SCOPE = (main && main.dataset.scope) || "orders";
const listEl = document.getElementById("q-list");
const emptyEl = document.getElementById("q-empty");
const searchEl = document.getElementById("q-search");

const state = { status: "open", q: "", cardActions: {} };

function editingOpen() {
  // Skip the periodic refresh only while something is ACTIVELY being edited — never freeze
  // the whole list on a stale marker. An open inline form, the search box focused, or any
  // qty/price/free input that is focused OR already holds text all count; an abandoned
  // (empty, blurred) focus does not, so the list resumes refreshing on its own.
  if (searchEl && document.activeElement === searchEl) return true;
  if (!listEl) return false;
  if (listEl.querySelector(".q-inline-form")) return true;
  for (const inp of listEl.querySelectorAll(".q-qty, .q-price, .q-freein, .q-in")) {
    if (document.activeElement === inp) return true;
    if (inp.value && inp.value.trim()) return true;
  }
  return false;
}

// ---- answer-body shapers per kind (candidate click) -------------------------------
function candidateButton(q, cand) {
  const kind = q.kind;
  let label, body;
  if (kind === "item") {
    label = cand.name || cand.gtin;
    body = () => ({ gtin: cand.gtin, card: cand.name || "", ...lineEdits(q) });
  } else if (kind === "customer") {
    label = cand.name || cand.ean_edi;
    body = () => ({ ean_edi: cand.ean_edi, name: cand.name || "" });
  } else {
    label = cand.label || cand.value;
    body = () => ({ choice: cand.value });
  }
  return el("button", { class: "q-btn q-btn--cand", type: "button",
    onclick: () => submit(q.id, body()) }, label);
}

function lineEdits(q) {
  const card = document.getElementById(`q-card-${q.id}`);
  if (!card) return {};
  const out = {};
  const qty = card.querySelector(".q-qty");
  const price = card.querySelector(".q-price");
  if (qty && qty.value.trim()) out.quantity = qty.value.trim();
  if (price && price.value.trim()) out.unit_price = price.value.trim();
  return out;
}

// ---- action buttons (op -> body / inline form) ------------------------------------
const SIMPLE_OPS = {
  manual: () => ({ manual: true }),
  unknown_customer: () => ({ unknown: true }),
  not_order: () => ({ not_order: true }),
  mail_not_order: () => ({ choice: "not_order" }),
  mail_manual: () => ({ choice: "manual" }),
  line_keep: () => ({ choice: "keep" }),
  line_drop: () => ({ choice: "drop" }),
  ship_without: () => ({ choice: "ship_without" }),
  not_warehouse: () => ({ not_warehouse: true }),
  dl_unknown: () => ({ choice: "unknown" }),
};

const FORM_OPS = {
  new_product: { key: "new_product", fields: [["gtin", "Číslo položky"], ["name", "Názov karty"]] },
  new_item: { key: "new_item", fields: [["gtin", "Číslo položky"], ["name", "Názov karty"]] },
  new_customer: { key: "new_customer", fields: [["ean_edi", "EAN zákazníka"], ["name", "Názov"]] },
  new_supplier: { key: "new_supplier", fields: [["ean_edi", "EAN dodávateľa"], ["name", "Názov"]] },
};

function actionButton(q, act) {
  if (SIMPLE_OPS[act.op]) {
    return el("button", { class: "q-btn q-btn--act", type: "button",
      onclick: () => submit(q.id, { ...SIMPLE_OPS[act.op](), ...(q.kind === "item" ? lineEdits(q) : {}) }) },
      act.label);
  }
  if (FORM_OPS[act.op]) {
    return el("button", { class: "q-btn q-btn--act", type: "button",
      onclick: (e) => toggleForm(q, FORM_OPS[act.op], e.target) }, act.label);
  }
  return null;
}

function toggleForm(q, spec, btn) {
  const card = document.getElementById(`q-card-${q.id}`);
  if (!card) return;
  let form = card.querySelector(".q-inline-form");
  if (form) { form.remove(); card.removeAttribute("data-open"); return; }
  card.setAttribute("data-open", "1");
  const inputs = spec.fields.map(([name, ph]) =>
    el("input", { class: `q-in q-in-${name}`, type: "text", placeholder: ph, autocomplete: "off" }));
  form = el("div", { class: "q-inline-form" }, [
    ...inputs,
    el("button", { class: "q-btn q-btn--primary", type: "button", onclick: () => {
      const payload = {};
      spec.fields.forEach(([name], i) => { payload[name] = inputs[i].value.trim(); });
      const body = { [spec.key]: payload };
      if (q.kind === "item") Object.assign(body, lineEdits(q));
      submit(q.id, body);
    } }, "Uložiť"),
    el("button", { class: "q-btn", type: "button",
      onclick: () => { form.remove(); card.removeAttribute("data-open"); } }, "Zrušiť"),
  ]);
  card.appendChild(form);
  if (inputs[0]) inputs[0].focus();
}

// ---- one card ---------------------------------------------------------------------
function card(q) {
  const box = el("section", { class: "q-card", id: `q-card-${q.id}`,
    "data-kind": q.kind, "data-qid": q.id });
  const title = q.wording || q.customer_name || `#${q.id}`;
  box.appendChild(el("div", { class: "q-head" }, [
    el("span", { class: "q-kind" }, q.kind),
    el("span", { class: "q-title" }, title),
    q.customer_name ? el("span", { class: "q-cust" }, q.customer_name) : null,
  ]));

  if (state.status === "answered") {
    box.appendChild(el("div", { class: "q-answer" },
      `Odpovedané: ${q.answer_card || q.answer_gtin || (q.answer && q.answer.choice) || "—"}`));
    box.appendChild(el("div", { class: "q-actions" }, [
      el("button", { class: "q-btn q-btn--undo", type: "button",
        onclick: () => act(q.id, "undo") }, "Vrátiť odpoveď"),
      previewButton(q),
    ]));
    return box;
  }

  // open / expired: candidates + line edits + actions
  const cands = (q.candidates || []).map((c) => candidateButton(q, c));
  if (cands.length) box.appendChild(el("div", { class: "q-cands" }, cands));

  if (q.kind === "item") {
    box.appendChild(el("div", { class: "q-lineedit" }, [
      el("label", {}, ["Množstvo ", el("input", { class: "q-qty", type: "text",
        value: q.quantity != null ? String(q.quantity) : "" })]),
      el("label", {}, ["Cena/ks ", el("input", { class: "q-price", type: "text",
        value: q.unit_price != null ? String(q.unit_price) : "" })]),
    ]));
  }
  if (q.kind === "item" || q.kind === "dl_item") {
    box.appendChild(el("div", { class: "q-freecard" }, [
      el("input", { class: "q-freein", type: "text", placeholder: "Iné číslo položky (GTIN)",
        autocomplete: "off" }),
      el("button", { class: "q-btn", type: "button", onclick: () => {
        const g = box.querySelector(".q-freein").value.trim();
        if (!g) { toast("Zadaj číslo položky", { error: true }); return; }
        submit(q.id, q.kind === "item"
          ? { gtin: g, card: "", ...lineEdits(q) } : { choice: g });
      } }, "Priradiť"),
    ]));
  }

  const acts = (state.cardActions[q.kind] || []).map((a) => actionButton(q, a)).filter(Boolean);
  const actionsRow = el("div", { class: "q-actions" }, acts);
  if (state.status === "expired") {
    actionsRow.appendChild(el("button", { class: "q-btn q-btn--reopen", type: "button",
      onclick: () => act(q.id, "reopen") }, "Znovu otvoriť"));
  }
  actionsRow.appendChild(previewButton(q));
  box.appendChild(actionsRow);
  return box;
}

function previewButton(q) {
  return el("button", { class: "q-btn q-btn--preview", type: "button",
    onclick: () => togglePreview(q) }, "Náhľad originálu");
}

async function togglePreview(q) {
  const card = document.getElementById(`q-card-${q.id}`);
  if (!card) return;
  const existing = card.querySelector(".q-preview");
  if (existing) { existing.remove(); return; }
  try {
    const pv = await apiGet(`/questions/${q.id}/preview`);
    const atts = (pv.attachments || []).map((a) =>
      el("a", { class: "q-att", href: a.url, target: "_blank", rel: "noopener" },
        a.filename || `príloha ${a.idx}`));
    if (pv.eml_url) atts.push(el("a", { class: "q-att", href: pv.eml_url, target: "_blank",
      rel: "noopener" }, "raw .eml"));
    card.appendChild(el("div", { class: "q-preview" }, [
      el("div", { class: "q-pv-line" }, `Predmet: ${pv.subject || "—"}`),
      el("div", { class: "q-pv-line" }, `Od: ${pv.from_name || ""} <${pv.from_addr || ""}>`),
      el("div", { class: "q-pv-line" }, `Dátum: ${pv.sent_at || pv.created_at || "—"}`),
      atts.length ? el("div", { class: "q-atts" }, atts) : el("div", { class: "q-pv-line" }, "Bez príloh"),
    ]));
  } catch (e) { toast(e.message, { error: true }); }
}

// ---- network actions --------------------------------------------------------------
async function submit(qid, body) {
  try {
    await apiPost(`/questions/${qid}/answer`, body);
    toast("Uložené");
    await load();
  } catch (e) { toast(e.message, { error: true }); }
}

async function act(qid, op) {
  try {
    await apiPost(`/questions/${qid}/${op}`, {});
    toast(op === "undo" ? "Odpoveď vrátená" : "Znovu otvorené");
    await load();
  } catch (e) { toast(e.message, { error: true }); }
}

// ---- load + render ----------------------------------------------------------------
async function load() {
  try {
    const params = new URLSearchParams({ scope: SCOPE, status: state.status });
    if (state.q) params.set("q", state.q);
    const data = await apiGet(`/questions?${params.toString()}`);
    state.cardActions = (data.meta && data.meta.card_actions) || {};
    clear(listEl);
    const items = data.items || [];
    emptyEl.hidden = items.length > 0;
    for (const q of items) listEl.appendChild(card(q));
    focusQuestion(false);   // re-apply the deep-link highlight after a refresh (no re-scroll)
  } catch (e) { toast(e.message, { error: true }); }
}

// #459: a deep link from an Odoo message — `?q=<question_id>` — scrolls to + highlights that
// ONE question card. Distinct from the #447 search-seed below (a NON-numeric `q`, e.g. a
// message_id from the „Naučené" origin link, still seeds the search box). `scroll` is true
// only on the first load, so a periodic refresh keeps the highlight without re-scrolling.
function focusQuestion(scroll) {
  if (!_focusId) return;
  const el2 = document.getElementById(`q-card-${_focusId}`);
  if (!el2) return;
  el2.classList.add("q-card--focus");
  if (scroll) el2.scrollIntoView({ behavior: "smooth", block: "center" });
}

// ---- wiring -----------------------------------------------------------------------
document.querySelectorAll(".q-chip").forEach((chip) => {
  chip.addEventListener("click", () => {
    document.querySelectorAll(".q-chip").forEach((c) => c.classList.remove("is-active"));
    chip.classList.add("is-active");
    state.status = chip.dataset.status;
    load();
  });
});
if (searchEl) {
  searchEl.addEventListener("input", debounce(() => { state.q = searchEl.value.trim(); load(); }, 300));
}

// #447: honour a deep link from the „Naučené" tab origin link
// (/nastenka/otazky-<scope>?q=<message_id>&status=answered) — seed the filter + search box so
// the tab opens already filtered to that rule's originating question. Additive: no params =
// unchanged default (open questions, empty search).
const _init = new URLSearchParams(location.search);
const _initStatus = _init.get("status");
const _initQ = _init.get("q");
// #459: a purely-numeric `q` is a question-id DEEP LINK (scroll + highlight), never a search
// filter — seeding it into the search box would filter the list empty (search matches wording/
// message_id, not the DB id). A non-numeric `q` stays the #447 search-seed (a message_id is
// never a bare integer, so that path is unchanged).
const _focusId = _initQ && /^\d+$/.test(_initQ) ? _initQ : null;
if (["open", "expired", "answered"].includes(_initStatus)) state.status = _initStatus;
if (_initQ && !_focusId) state.q = _initQ;
if (searchEl && state.q) searchEl.value = state.q;
document.querySelectorAll(".q-chip").forEach((c) => {
  c.classList.toggle("is-active", c.dataset.status === state.status);
});

setInterval(() => { if (!editingOpen()) load(); }, 8000);
load().then(() => focusQuestion(true));
