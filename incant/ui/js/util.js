/* util — app state, DOM/format helpers, toasts, modal machinery, status pills, diff renderers */
"use strict";

const State = {
  token: readStoredToken(),   // sessionStorage → localStorage → "" (no baked default)
  env: localStorage.getItem("incant_env") || "prod",
  theme: localStorage.getItem("incant_theme") || "light",
  tech: localStorage.getItem("incant_tech") === "1",   // reveal commit SHAs / rules_version
  envs: [],
  me: null,               // cached GET /mgmt/whoami — cleared when the key changes
  _meFailed: false,       // whoami rejected (bad/absent key) → account chip shows "not signed in"
  _mePromise: null,       // in-flight whoami fetch, so it runs once per session
  tweakOpen: false,
  navOpen: false,        // off-canvas nav drawer (mobile) open state
  route: { name: "prompts", pid: null, q: {} },
};

// ── credentials / storage ────────────────────────────────────────────
// The API key is read from sessionStorage first, then localStorage. A "remember on this
// device" key persists in localStorage; an un-remembered key lives only for the session.
// There is no baked default — an empty token surfaces the 401 sign-in card.
function readStoredToken() {
  try { const s = sessionStorage.getItem("incant_token"); if (s) return s; } catch (_) {}
  try { const l = localStorage.getItem("incant_token"); if (l) return l; } catch (_) {}
  return "";
}
// remember=true → persist in localStorage (survives the browser); false → sessionStorage
// only. Writing one store always clears the other so the two never disagree.
function saveToken(val, remember) {
  State.token = val;
  try {
    if (remember) { localStorage.setItem("incant_token", val); sessionStorage.removeItem("incant_token"); }
    else { sessionStorage.setItem("incant_token", val); localStorage.removeItem("incant_token"); }
  } catch (_) {}
}
// "Forget this key" — clears both stores and the token (the 401 card takes over).
function forgetToken() {
  State.token = "";
  try { localStorage.removeItem("incant_token"); sessionStorage.removeItem("incant_token"); } catch (_) {}
}

// ── role hierarchy (chrome gating) ───────────────────────────────────
// renderer < viewer < editor < operator < releaser < admin. Chrome is gated by the BEST
// role held in ANY scope — the server's per-project 403s remain the real enforcement, so
// we never attempt per-project chrome math here.
const ROLE_RANK = { renderer: 0, viewer: 1, editor: 2, operator: 3, releaser: 4, admin: 5 };
function roleRank(role) { return Object.prototype.hasOwnProperty.call(ROLE_RANK, role) ? ROLE_RANK[role] : -1; }
// The highest role a principal holds across every scope (or null).
function bestRole(me) {
  me = (me === undefined) ? State.me : me;
  let best = null, bi = -1;
  for (const r of (me && me.roles) || []) { const i = roleRank(r.role); if (i > bi) { bi = i; best = r.role; } }
  return best;
}
// Does the current principal's best role meet `min`? Gates show/hide of mutating chrome.
function canRole(min) { return roleRank(bestRole()) >= roleRank(min); }

// ── util ─────────────────────────────────────────────────────────────
const esc = (s) => String(s == null ? "" : s).replace(/[&<>"']/g, (c) =>
  ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const enc = encodeURIComponent;
const el = (id) => document.getElementById(id);
function ago(iso) {
  if (!iso) return "";
  const d = new Date(iso), s = (Date.now() - d.getTime()) / 1000;
  if (s < 90) return "just now";
  if (s < 5400) return Math.round(s / 60) + "m";
  if (s < 129600) return Math.round(s / 3600) + "h";
  return Math.round(s / 86400) + "d";
}
let toastTimer;
function toast(msg, err) {
  let t = el("toast");
  if (!t) {
    t = document.createElement("div"); t.id = "toast";
    t.setAttribute("role", "status"); t.setAttribute("aria-live", "polite");
    document.body.appendChild(t);
  }
  t.className = "toast show" + (err ? " err" : "");
  t.textContent = msg;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => (t.className = "toast"), 2600);
}
function errText(e) {
  if (e && e.data && e.data.detail) {
    const d = e.data.detail;
    return typeof d === "string" ? d : (d.detail || JSON.stringify(d));
  }
  return (e && e.status ? "HTTP " + e.status : "error");
}
function go(hash) { location.hash = hash; }

// ── modal ────────────────────────────────────────────────────────────
let _modalPrevFocus = null;   // element focused before the modal opened — restored on close
// The keyboard focusables inside a container, in DOM order (used by the focus trap).
function modalFocusables(container) {
  if (!container || !container.querySelectorAll) return [];
  return Array.from(container.querySelectorAll(
    'a[href],button:not([disabled]),input:not([disabled]),textarea:not([disabled]),select:not([disabled]),[tabindex]:not([tabindex="-1"])'));
}
function openModal(html, cls) {
  // Remember what had focus so we can restore it when the dialog closes.
  const prev = (typeof document !== "undefined" && document.activeElement) ? document.activeElement : null;
  closeModal();
  _modalPrevFocus = prev;
  const o = document.createElement("div");
  o.id = "modal";
  o.className = "modal-overlay";
  o.innerHTML = `<div class="modal ${cls || ""}" role="dialog" aria-modal="true">${html}</div>`;
  document.body.appendChild(o);
  const dialog = o.querySelector && o.querySelector(".modal");
  if (dialog) {
    const h = dialog.querySelector && dialog.querySelector("h3");
    if (h && h.textContent) dialog.setAttribute("aria-label", h.textContent);
    // Focus the first field, else the first focusable control (keeps prior behavior).
    const field = dialog.querySelector("input, textarea, select");
    const target = field || modalFocusables(dialog)[0];
    if (target && target.focus) target.focus();
    // Focus trap: keep Tab / Shift-Tab cycling within the dialog.
    if (o.addEventListener) o.addEventListener("keydown", (ev) => {
      if (ev.key !== "Tab") return;
      const f = modalFocusables(dialog);
      if (!f.length) return;
      const first = f[0], last = f[f.length - 1];
      if (ev.shiftKey && document.activeElement === first) { ev.preventDefault(); last.focus(); }
      else if (!ev.shiftKey && document.activeElement === last) { ev.preventDefault(); first.focus(); }
    });
  }
}
function closeModal() {
  const m = el("modal");
  if (m) m.remove();
  const prev = _modalPrevFocus;
  _modalPrevFocus = null;
  if (prev && prev.focus) { try { prev.focus(); } catch (_) { /* element gone */ } }
}
function isLocked() {
  return !!(State.envs.find((e) => e.id === State.env) || {}).protected;
}
// LaunchDarkly-style "type the name to confirm" modal for locked (protected) envs.
// `body` is trusted HTML — callers must esc() any interpolated values.
function typeToConfirm({ title, body, token, confirmLabel, act, data }) {
  const attrs = Object.entries(data || {}).map(([k, v]) => `data-${esc(k)}="${esc(v)}"`).join(" ");
  return `
    <h3>${esc(title)}</h3>
    <p class="hint">${body}</p>
    <div style="margin:6px 0 2px;font-size:11px;color:var(--faint)">Type <span class="mono" style="color:var(--mut)">${esc(token)}</span> to confirm:</div>
    <input id="confirmInput" data-act="confirmInput" data-token="${esc(token)}" data-btn="confirmBtn"
      spellcheck="false" autocomplete="off" placeholder="${esc(token)}"
      style="width:100%;font-family:'IBM Plex Mono',monospace">
    <div class="modal-actions">
      <button class="btn" data-act="closeModal">Cancel</button>
      <button id="confirmBtn" class="btn primary" disabled data-act="${esc(act)}" ${attrs}>${esc(confirmLabel)}</button>
    </div>`;
}


// ── status vocabulary (shared) ───────────────────────────────────────
// green = "Live for everyone", amber = "Testing with a group",
// indigo = "unpublished draft/edits". These helpers keep the dots + pills
// consistent across the prompts list, the prompt page, and (next agent) targeting.
const KIND_CLS = { live: "live", testing: "testing", draft: "draft" };
function statusLine(kind, text, sub) {   // dot + bold sentence (+ optional muted sub)
  const k = KIND_CLS[kind] || "draft";
  return `<span class="statusline ${k}"><span class="sdot ${k}"></span><span>${text}</span></span>` +
    (sub ? `<span class="faint" style="font-size:11.5px">${sub}</span>` : "");
}
// A pill badge. kind: warn (amber testing) · acc (indigo edits) · neutral (grey draft) · live.
function pill(kind, text) { return `<span class="pill ${kind}">${text}</span>`; }
const plural = (n, one, many) => (n === 1 ? one : (many || one + "s"));

// The on-demand "technical details" disclosure. `inner` is trusted HTML (mono content) —
// callers esc() any interpolated values. Toggled by State.tech (persisted).
function techDetails(inner, hint) {
  if (State.tech) {
    return `<div class="techdet">
      <button type="button" class="techtoggle btn-bare" data-act="toggleTech" aria-expanded="true">Hide technical details ▴</button>
      <div class="techbody">${inner}</div></div>`;
  }
  return `<div class="techdet">
    <button type="button" class="techtoggle btn-bare" data-act="toggleTech" aria-expanded="false">Show technical details ▾${
      hint ? `<span class="hintmono">${esc(hint)}</span>` : ""}</button></div>`;
}

// ── diff helpers (shared by Compare + the draft diff tab) ─────────────
function renderUnifiedDiff(diffText) {
  const lines = (diffText || "").split("\n");
  if (lines.length === 1 && lines[0] === "") return "";   // empty diff = no changes
  return lines.map((ln) => {
    let cls = "";
    if (ln.startsWith("+") && !ln.startsWith("+++")) cls = "add";
    else if (ln.startsWith("-") && !ln.startsWith("---")) cls = "del";
    if (ln.startsWith("@@") || ln.startsWith("+++") || ln.startsWith("---"))
      return `<div class="diffline"><span class="gut"></span><span class="txt faint">${esc(ln)}</span></div>`;
    return `<div class="diffline ${cls}"><span class="gut">${cls === "add" ? "+" : cls === "del" ? "−" : ""}</span><span class="txt">${esc(ln.replace(/^[+-]/, ""))}</span></div>`;
  }).join("");
}
// LCS line alignment: unchanged lines sit across from each other, inserted/removed
// lines get their own colored row — feeds the side-by-side rendered diff.
function alignLines(a, b) {
  const n = a.length, m = b.length;
  const dp = Array.from({ length: n + 1 }, () => new Int32Array(m + 1));
  for (let i = n - 1; i >= 0; i--)
    for (let j = m - 1; j >= 0; j--)
      dp[i][j] = a[i] === b[j] ? dp[i + 1][j + 1] + 1 : Math.max(dp[i + 1][j], dp[i][j + 1]);
  const rows = []; let i = 0, j = 0;
  while (i < n && j < m) {
    if (a[i] === b[j]) { rows.push({ l: a[i], r: b[j], t: "same" }); i++; j++; }
    else if (dp[i + 1][j] >= dp[i][j + 1]) { rows.push({ l: a[i], r: null, t: "del" }); i++; }
    else { rows.push({ l: null, r: b[j], t: "add" }); j++; }
  }
  while (i < n) rows.push({ l: a[i++], r: null, t: "del" });
  while (j < m) rows.push({ l: null, r: b[j++], t: "add" });
  return rows;
}
function renderSideBySide(left, right) {
  return alignLines((left || "").split("\n"), (right || "").split("\n")).map((row) =>
    `<div class="sxs-row"><div class="sxs-cell${row.t === "del" ? " del" : ""}">${row.l == null ? "" : esc(row.l)}</div>` +
    `<div class="sxs-cell${row.t === "add" ? " add" : ""}">${row.r == null ? "" : esc(row.r)}</div></div>`).join("");
}
// "nothing servable in the pointer history of X@vN" means an included prompt was
// never published in this environment — no variables or test context can fix it.
function friendlyRenderError(msg) {
  const s = String(msg || "");
  const m = /nothing servable in the pointer history of ([\w\/.-]+)@v(\d+)/.exec(s);
  if (m) {
    const ipid = m[1], ver = m[2];
    const hash = `#/p/${enc(ipid)}/pointers?v=${enc(ver)}`;
    return `⚠ This prompt includes <b>${esc(ipid)}</b>, which isn't published in <b>${esc(State.env)}</b> yet — there's nothing live to expand. ` +
      `<a href="${hash}" data-act="go" data-hash="${hash}">Publish ${esc(ipid)} →</a>`;
  }
  // Jinja names the container type, not the variable, when a value has the wrong
  // shape (e.g. a list variable set to plain text) — translate that for humans.
  if (/'(str|dict|list|int|float|bool) object'/.test(s))
    return `⚠ ${esc(s)}<br><span style="color:var(--mut)">A value probably has the wrong shape — a list variable needs <span class="mono">[]</span>, an object needs <span class="mono">{}</span>.</span>`;
  return null;
}
// A pane head for the review/publish side-by-side rendered panes.
function sxsPaneHead(label) {
  return `<div style="padding:7px 14px;font-size:11px;font-weight:600;color:var(--mut);border-bottom:1px solid var(--line2);background:var(--panel2)">${esc(label)}</div>`;
}
