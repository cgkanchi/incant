/* api — the mgmt/serving fetch wrapper and GET/POST/PUT/PATCH shorthands */
"use strict";

// ── api ──────────────────────────────────────────────────────────────
// Auth is cookie-first: a valid session rides in an HttpOnly cookie the browser attaches to
// same-origin requests automatically, so no Authorization header is sent; non-GET requests
// then carry the CSRF header. An in-memory bearer (State.token) — the harness/debug escape
// hatch — takes over when set and needs no CSRF. A 403 csrf_required is retried once after
// refreshing the CSRF token from the session endpoint.
async function api(method, path, body, _retried) {
  const headers = { "Content-Type": "application/json" };
  if (State.token) headers.Authorization = "Bearer " + State.token;
  else if (method !== "GET" && State.csrf) headers["X-Incant-CSRF"] = State.csrf;
  const res = await fetch(path, {
    method,
    cache: "no-store",
    headers,
    body: body !== undefined ? JSON.stringify(body) : undefined,
  });
  const text = await res.text();
  let data = null;
  try { data = text ? JSON.parse(text) : null; } catch { data = text; }
  if (!res.ok) {
    // Cookie mode: a missing/stale CSRF token yields 403 csrf_required. Refresh it from the
    // session endpoint and retry the request exactly once before giving up.
    if (res.status === 403 && !State.token && !_retried && isCsrfRequired(data)) {
      const s = await fetchSession();
      if (s) applySession(s);
      return api(method, path, body, true);
    }
    throw { status: res.status, data };
  }
  return data;
}
// True when a 403 body signals the CSRF header was missing/mismatched.
function isCsrfRequired(data) {
  if (data === "csrf_required") return true;
  const d = data && data.detail;
  return d === "csrf_required" || !!(d && typeof d === "object" && d.detail === "csrf_required");
}
// Cookie-session identity fetch — a plain GET (no bearer, no CSRF). Returns the payload
// {principal_id, name, roles, csrf} when the cookie is valid, else null.
async function fetchSession() {
  try {
    const res = await fetch("/auth/session", { method: "GET", cache: "no-store", headers: { "Content-Type": "application/json" } });
    if (!res.ok) return null;
    const text = await res.text();
    return text ? JSON.parse(text) : null;
  } catch (_) { return null; }
}
const GET = (p) => api("GET", p);
const POST = (p, b) => api("POST", p, b);
const PUT = (p, b) => api("PUT", p, b);
const PATCH = (p, b) => api("PATCH", p, b);
