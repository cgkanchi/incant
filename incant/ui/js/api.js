/* api — the mgmt/serving fetch wrapper and GET/POST/PUT/PATCH shorthands */
"use strict";

// ── api ──────────────────────────────────────────────────────────────
async function api(method, path, body) {
  const res = await fetch(path, {
    method,
    cache: "no-store",
    headers: { Authorization: "Bearer " + State.token, "Content-Type": "application/json" },
    body: body !== undefined ? JSON.stringify(body) : undefined,
  });
  const text = await res.text();
  let data = null;
  try { data = text ? JSON.parse(text) : null; } catch { data = text; }
  if (!res.ok) throw { status: res.status, data };
  return data;
}
const GET = (p) => api("GET", p);
const POST = (p, b) => api("POST", p, b);
const PUT = (p, b) => api("PUT", p, b);
const PATCH = (p, b) => api("PATCH", p, b);
