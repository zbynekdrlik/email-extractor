// Board UI helpers + module entry point (#442 lane 1). Loaded by every nástenka page
// (layout.html's <script type="module">). ONE copy of each helper; later tab modules import
// from here. Lane 1 wires nothing (the tabs are placeholders) — this module only has to LOAD
// cleanly, with zero console errors/warnings, and expose the shared helpers.

import { apiDelete, apiGet, apiPost, ping } from "./api.js";

// Re-export the API wrapper so a tab module can `import { apiGet, toast } from "./ui.js"`.
export { apiGet, apiPost, apiDelete, ping };

const TOAST_MS = 3500;
let _toastTimer = null;

// Show a transient message. `error: true` styles it red. Safe if the toast node is absent.
export function toast(message, { error = false } = {}) {
  const node = document.getElementById("board-toast");
  if (!node) return;
  node.textContent = String(message == null ? "" : message);
  node.classList.toggle("is-error", !!error);
  node.hidden = false;
  if (_toastTimer) clearTimeout(_toastTimer);
  _toastTimer = setTimeout(() => { node.hidden = true; }, TOAST_MS);
}

// Trailing-edge debounce — for a search box that must not fire per keystroke.
export function debounce(fn, ms = 300) {
  let t = null;
  return (...args) => {
    if (t) clearTimeout(t);
    t = setTimeout(() => fn(...args), ms);
  };
}

// Refresh-safety: mark a form as "being edited" so an auto-refresh cycle knows not to wipe it,
// and restore focus afterwards. Lane-1 primitive the later interactive tabs build on.
export function markOpen(form) {
  if (form) form.setAttribute("data-open", "1");
}
export function isOpen(form) {
  return !!(form && form.getAttribute("data-open") === "1");
}
export function markClosed(form) {
  if (form) form.removeAttribute("data-open");
}

// Tiny DOM builders so a tab module never concatenates HTML strings (spec §3: no HTML in JS
// strings either — build nodes).
export function el(tag, attrs = {}, children = []) {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (k === "class") node.className = v;
    else if (k === "text") node.textContent = v;
    else if (k.startsWith("on") && typeof v === "function") node.addEventListener(k.slice(2), v);
    else if (v != null) node.setAttribute(k, v);
  }
  for (const c of [].concat(children)) {
    if (c == null) continue;
    node.appendChild(typeof c === "string" ? document.createTextNode(c) : c);
  }
  return node;
}

export function clear(node) {
  if (node) node.replaceChildren();
}
