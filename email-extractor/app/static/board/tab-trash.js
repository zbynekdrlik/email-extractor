// Kôš / História zmien tab (#444 lane 3). Lists /api/board/audit, drives „Vrátiť". Vanilla ES
// module, no build, no CDN; all shared helpers come from ui.js (ONE copy each). No HTML strings
// — every node is built with el() (spec §3).
import { apiGet, apiPost, clear, debounce, el, toast } from "./ui.js";

const PAGE_SIZE = 50;

// Human labels — presentation only, so they live here (JS), never in Python (spec §3).
const TABLE_LABELS = {
  catalog_overrides: "Karta (sklad)",
  dl_catalog_overrides: "Karta (objednávky)",
  customer_overrides: "Zákazník",
  dl_supplier_overrides: "Dodávateľ",
  mail_rules: "Ignorovaný mail",
  item_memory: "Alias položky",
  global_item_memory: "Globálny alias",
  dl_item_memory: "Alias položky (DL)",
  dl_supplier_memory: "Pamäť dodávateľa",
  order_questions: "Otázka",
};
const ACTION_LABELS = {
  create: "Pridané", update: "Zmenené", delete: "Zmazané",
  restore: "Vrátené", answer: "Zodpovedané", undo: "Vrátená odpoveď",
  reopen: "Znovu otvorené", teach: "Doučené",
};

const state = { action: "", q: "", page: 0, total: 0 };

const $ = (id) => document.getElementById(id);

function fmtTs(iso) {
  if (!iso) return "";
  const d = new Date(iso);
  if (isNaN(d)) return iso;
  const p = (n) => String(n).padStart(2, "0");
  return `${p(d.getDate())}.${p(d.getMonth() + 1)}. ${p(d.getHours())}:${p(d.getMinutes())}`;
}

function summarize(row) {
  const oneline = (o) => {
    if (o == null) return "";
    if (typeof o !== "object") return String(o);
    return Object.entries(o).map(([k, v]) => `${k}: ${v}`).join(", ");
  };
  const b = oneline(row.before), a = oneline(row.after);
  if (b && a) return `${b} → ${a}`;
  return b || a || "";
}

function refLabel(row) {
  if (row.question_id != null) return `otázka #${row.question_id}`;
  if (row.message_id) return `doklad ${row.message_id}`;
  if (row.row_id) return `#${row.row_id}`;
  return "";
}

async function doRestore(row) {
  if (!window.confirm(`Vrátiť túto zmenu (${ACTION_LABELS[row.action] || row.action})?`)) return;
  try {
    await apiPost(`/audit/${row.id}/restore`);
    toast("Vrátené.");
    load();
  } catch (e) {
    toast(e.message || "Vrátenie zlyhalo.", { error: true });
  }
}

function renderRow(row) {
  const canRestore = row.action !== "restore";
  const btn = canRestore
    ? el("button", { class: "trash-restore", type: "button",
                     onclick: () => doRestore(row), text: "Vrátiť" })
    : "";
  return el("tr", {}, [
    el("td", { text: fmtTs(row.ts) }),
    el("td", { text: row.actor || "" }),
    el("td", { text: TABLE_LABELS[row.table_name] || row.table_name || "" }),
    el("td", { text: ACTION_LABELS[row.action] || row.action || "" }),
    el("td", { class: "trash-summary", text: summarize(row) }),
    el("td", { class: "trash-ref", text: refLabel(row) }),
    el("td", {}, [btn]),
  ]);
}

async function load() {
  const params = new URLSearchParams();
  if (state.action) params.set("action", state.action);
  if (state.q) params.set("q", state.q);
  params.set("page", String(state.page));
  let res;
  try {
    res = await apiGet(`/audit?${params.toString()}`);
  } catch (e) {
    toast(e.message || "Načítanie zlyhalo.", { error: true });
    return;
  }
  state.total = res.total || 0;
  const tbody = $("trash-rows");
  clear(tbody);
  for (const row of res.items || []) tbody.appendChild(renderRow(row));
  $("trash-empty").hidden = (res.items || []).length > 0;

  const pages = Math.max(1, Math.ceil(state.total / PAGE_SIZE));
  $("trash-page-info").textContent = state.total
    ? `Strana ${state.page + 1} z ${pages} · ${state.total} záznamov` : "";
  $("trash-prev").disabled = state.page <= 0;
  $("trash-next").disabled = state.page >= pages - 1;
}

function wire() {
  for (const chip of document.querySelectorAll(".trash-chip")) {
    chip.addEventListener("click", () => {
      for (const c of document.querySelectorAll(".trash-chip")) c.classList.remove("is-active");
      chip.classList.add("is-active");
      state.action = chip.getAttribute("data-action") || "";
      state.page = 0;
      load();
    });
  }
  const search = $("trash-search");
  if (search) {
    search.addEventListener("input", debounce(() => {
      state.q = search.value.trim();
      state.page = 0;
      load();
    }, 300));
  }
  $("trash-prev").addEventListener("click", () => {
    if (state.page > 0) { state.page -= 1; load(); }
  });
  $("trash-next").addEventListener("click", () => { state.page += 1; load(); });
}

wire();
load();
