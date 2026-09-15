// Board API fetch wrapper (#442 lane 1). ONE copy of every fetch helper; no CDN, no build.
// Every call is same-origin, session-cookie authenticated (credentials: 'same-origin' is the
// browser default for same-origin, set explicitly for clarity). A non-2xx JSON response is
// turned into a thrown Error carrying the server's own `error` message, so callers can toast it.

const BASE = "/api/board";

async function request(path, { method = "GET", body = null } = {}) {
  const opts = { method, credentials: "same-origin", headers: {} };
  if (body !== null) {
    opts.headers["Content-Type"] = "application/json";
    opts.body = JSON.stringify(body);
  }
  const resp = await fetch(BASE + path, opts);
  let data = null;
  try {
    data = await resp.json();
  } catch (e) {
    data = null; // a body-less response (e.g. 204) is fine
  }
  if (!resp.ok) {
    const msg = (data && data.error) || `Chyba servera (${resp.status})`;
    const err = new Error(msg);
    err.status = resp.status;
    throw err;
  }
  return data;
}

export function apiGet(path) {
  return request(path, { method: "GET" });
}

export function apiPost(path, body) {
  return request(path, { method: "POST", body });
}

export function apiDelete(path, body = null) {
  return request(path, { method: "DELETE", body });
}

export function ping() {
  return apiGet("/ping");
}
