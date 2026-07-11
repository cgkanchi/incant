/* Incant UI — single-page app over the mgmt + serving APIs. Vanilla JS, no build. */
"use strict";

const State = {
  token: localStorage.getItem("incant_token") || "incant_sk_dev_admin",
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
    <input id="confirmInput" data-token="${esc(token)}" spellcheck="false" autocomplete="off"
      placeholder="${esc(token)}"
      style="width:100%;font-family:'IBM Plex Mono',monospace"
      oninput="var b=document.getElementById('confirmBtn');b.disabled=(this.value.trim()!==this.dataset.token)">
    <div class="modal-actions">
      <button class="btn" data-act="closeModal">Cancel</button>
      <button id="confirmBtn" class="btn primary" disabled data-act="${esc(act)}" ${attrs}>${esc(confirmLabel)}</button>
    </div>`;
}

// ── describe rules ───────────────────────────────────────────────────
const OPSYM = { eq: "=", neq: "≠", in: "∈", not_in: "∉", contains: "⊇", starts_with: "starts", ends_with: "ends",
  matches: "~", gt: ">", gte: "≥", lt: "<", lte: "≤", semver_gt: "semver>", semver_lt: "semver<", exists: "exists" };
function describeWhen(c) {
  if (c == null) return '<span class="muted">always</span>';
  if (c.all) return c.all.map(describeWhen).join(' <span class="muted">and</span> ');
  if (c.any) return c.any.map(describeWhen).join(' <span class="muted">or</span> ');
  if (c.not) return '<span class="muted">not</span> ' + describeWhen(c.not);
  if (c.segment) return 'in segment <b>' + esc(c.segment) + "</b>";
  if (c.flag) {
    const val = c.values ? c.values.join(", ") : c.value;
    if (c.op === "exists") return '<span class="codeinline">' + esc(c.flag) + " exists</span>";
    return '<span class="codeinline">' + esc(c.flag) + " " + (OPSYM[c.op] || c.op) + " " + esc(val) + "</span>";
  }
  return esc(JSON.stringify(c));
}
function describeServe(s) {
  if (!s) return "";
  if (s.rollout) {
    const parts = (s.rollout.weights || []).map((w) =>
      (w.default ? "default" : (w.label || "v" + w.version)) + " " + w.weight + "%");
    return "rollout by <b>" + esc(s.rollout.bucket_by) + "</b> → " + parts.join(" / ");
  }
  if (s.label) return "label <b>" + esc(s.label) + "</b> @ live";
  if (s.version != null) return "<b>v" + s.version + " @ " + (s.at || "live") + "</b>";
  return esc(JSON.stringify(s));
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

// Which active rules apply to a prompt: a prompt-scoped rule matching this id, or
// any global rule (global rules apply to every prompt). Paused/archived excluded.
function activeRulesFor(rules, pid) {
  return (rules || []).filter((r) => r.status === "active" &&
    (r.scope === "global" || r.prompt_id === pid));
}
// What a rule's serve targets: {version?, label?, tip}. Rollouts report the first
// non-default weighted arm. Returns null when nothing concrete is served.
function serveTarget(serve) {
  if (!serve) return null;
  if (serve.rollout) {
    const w = (serve.rollout.weights || []).find((x) => !x.default && (x.version != null || x.label));
    return w ? { version: w.version, label: w.label, tip: false } : null;
  }
  if (serve.version != null) return { version: serve.version, tip: serve.at === "tip" };
  if (serve.label) return { label: serve.label, tip: serve.at === "tip" };
  return null;
}
// Testing descriptors for a prompt: active rules serving a non-default version, or a
// draft (@tip). `liveVersion` is the prompt's live/default version number.
function testingFor(rules, pid, liveVersion) {
  const out = [];
  for (const r of activeRulesFor(rules, pid)) {
    const t = serveTarget(r.serve);
    if (!t) continue;
    const differs = t.version != null && t.version !== liveVersion;
    if (!t.tip && !differs) continue;   // serving the live version, not "testing"
    out.push({ rule: r, version: t.version, label: t.label, tip: t.tip });
  }
  return out;
}

// ── plain-language rule helpers (for "Who sees what") ────────────────
function ordinal(n) {
  const s = ["th", "st", "nd", "rd"], v = n % 100;
  return n + (s[(v - 20) % 10] || s[v] || s[0]);
}
// A rule's serve target in a short plain phrase — returns trusted HTML (numbers are
// safe, labels are esc()'d). Used in the "rules that will be ignored" list.
function serveTargetPlain(serve) {
  const t = serveTarget(serve);
  if (!t) return "the default";
  if (t.tip) return `latest draft of Version ${t.version}`;
  if (t.version != null) return `Version ${t.version}`;
  if (t.label) return `label ${esc(t.label)}`;
  return "the default";
}
// The prose body line under an ordinal rule row: "See Version N — who it's for".
// Trusted HTML; describeWhen/serveTarget already esc() their values.
function ruleServeLine(r) {
  const t = serveTarget(r.serve);
  if (r.serve && r.serve.rollout) {
    const w = (r.serve.rollout.weights || []).find((x) => !x.default && (x.version != null || x.label));
    const bucket = esc(r.serve.rollout.bucket_by || "user");
    if (w && w.version != null)
      return `<b>${w.weight}% of users</b>, chosen by ${bucket}, see <b>Version ${w.version}</b>; the rest see the default`;
    return `A share of users see a newer version; the rest see the default`;
  }
  if (t && t.tip)
    return `See the <b>latest unpublished draft of Version ${t.version}</b> <span class="muted">— how you try changes before publishing them for everyone</span>`;
  if (t && t.version != null)
    return `See <b>Version ${t.version}</b> <span class="muted">— ${describeWhen(r.when)}</span>`;
  if (t && t.label)
    return `See <b>label ${esc(t.label)}</b> <span class="muted">— ${describeWhen(r.when)}</span>`;
  return `<span class="muted">${describeWhen(r.when)} → ${describeServe(r.serve)}</span>`;
}
// Stashed by screenRules so the "turn targeting off" confirm modal can list the rules.
let _rulesData = null;

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

// ── router ───────────────────────────────────────────────────────────
function parseRoute() {
  const h = location.hash.replace(/^#\/?/, "");
  const [pathPart, queryPart] = h.split("?");
  const q = {};
  if (queryPart) queryPart.split("&").forEach((kv) => { const [k, v] = kv.split("="); q[k] = decodeURIComponent(v || ""); });
  const parts = pathPart.split("/").filter(Boolean);
  if (parts.length === 0 || parts[0] === "prompts") return { name: "prompts", pid: null, q };
  if (parts[0] === "segments") return { name: "segments", pid: null, q };
  if (parts[0] === "play") return { name: "play", pid: null, q };
  if (parts[0] === "audit") return { name: "audit", pid: null, q };
  if (parts[0] === "access") return { name: "access", pid: null, q };
  if (parts[0] === "p") {
    const pid = decodeURIComponent(parts[1] || "");
    let screen = parts[2] || "overview";
    // Legacy route redirects — old links keep working after the draft-page reshape.
    if (screen === "editor") screen = "draft";
    else if (screen === "review") { screen = "draft"; if (!q.tab) q.tab = "review"; }
    else if (screen === "diff") screen = "compare";
    return { name: screen, pid, q };
  }
  return { name: "prompts", pid: null, q };
}

// ── shell / sidebar ──────────────────────────────────────────────────
function subnav(pid) {
  if (!pid) return "";
  const items = [
    ["overview", "Overview", "◈"], ["draft", "Edit", "✎"], ["compare", "Compare", "⇄"],
    ["rules", "Who sees what", "◐"], ["pointers", "Publish history", "▸"],
  ];
  const cur = State.route.name;
  const head = `<a class="subnav ${cur === "overview" ? "active" : ""}" href="#/p/${enc(pid)}/overview" data-act="go" data-hash="#/p/${enc(pid)}/overview">
    <span style="color:var(--acc-ink)">↳</span>${esc(pid)}</a>`;
  const rows = items.map(([id, label, gl]) =>
    `<a class="subnav ${cur === id ? "active" : ""}" href="#/p/${enc(pid)}/${id}" data-act="go" data-hash="#/p/${enc(pid)}/${id}">
      <span class="gl">${gl}</span><span>${label}</span></a>`).join("");
  return head + rows;
}

// ── identity / account chip ──────────────────────────────────────────
// Roles hierarchy (least → most privileged). The chip shows the highest held role.
const ROLE_ORDER = ["renderer", "viewer", "editor", "operator", "releaser", "admin"];
function highestRole(roles) {
  let best = null, bi = -1;
  for (const r of roles || []) { const i = ROLE_ORDER.indexOf(r.role); if (i > bi) { bi = i; best = r.role; } }
  return best;
}
function accountChipInner(me) {
  if (me && me.name) {
    const role = highestRole(me.roles);
    const initial = String(me.name || "?").trim().charAt(0).toUpperCase() || "?";
    return `<span class="acct-av">${esc(initial)}</span>
      <span class="acct-name">${esc(me.name)}</span>
      ${role ? `<span class="pill acc acct-role">${esc(role)}</span>` : ""}`;
  }
  if (State._meFailed)
    return `<span class="acct-av err">!</span><span class="acct-name faint">not signed in</span>`;
  return `<span class="acct-av">…</span><span class="acct-name faint">signing in…</span>`;
}
function accountChip() {
  return `<button class="acct btn-bare" id="acctChip" data-act="acctMenu" title="Account" aria-label="Account menu">${accountChipInner(State.me)}</button>`;
}
function updateAcctChip() { const h = el("acctChip"); if (h) h.innerHTML = accountChipInner(State.me); }
// Lazily fetch whoami once per session; cache on State.me. Callers may await the
// returned promise (screenAccess does). setToken() resets the cache + flags.
function ensureWhoami() {
  if (State.me || State._meFailed) { updateAcctChip(); return Promise.resolve(); }
  if (State._mePromise) return State._mePromise;
  State._mePromise = (async () => {
    try { State.me = await GET("/mgmt/whoami"); }
    catch (e) { State._meFailed = true; State.me = null; }
    finally { State._mePromise = null; updateAcctChip(); }
  })();
  return State._mePromise;
}

// The centered sign-in card that replaces screen content on a 401.
function signInCard() {
  return `<div class="signin-wrap"><div class="signin-card">
    <div class="signin-mark">✦</div>
    <div class="signin-title serif">Sign in to Incant</div>
    <p class="signin-copy">Paste an API key — you got one from your admin, or use the bootstrap dev key on a fresh install.</p>
    <input id="signinKey" type="password" class="signin-input" placeholder="incant_sk_…" spellcheck="false" autocomplete="off"
      onkeydown="if(event.key==='Enter'){var b=document.getElementById('signinBtn');if(b)b.click();}">
    <button id="signinBtn" class="btn primary" data-act="setToken" style="width:100%;margin-top:10px">Sign in</button>
    <div class="signin-hint">Keys are managed in <b>Access</b> — an admin can issue you one.</div>
  </div></div>`;
}
// "Switch API key…" — a password-input modal that reuses setToken semantics.
function openSwitchKeyModal() {
  openModal(`
    <h3>Switch API key</h3>
    <p class="hint">Paste an API key to sign in as a different principal. Keys are managed in Access and never shown here.</p>
    <input id="switchKeyIn" type="password" placeholder="incant_sk_…" spellcheck="false" autocomplete="off"
      style="width:100%;font-family:'IBM Plex Mono',monospace"
      onkeydown="if(event.key==='Enter'){var b=document.getElementById('switchKeyBtn');if(b)b.click();}">
    <div class="modal-actions">
      <button class="btn" data-act="closeModal">Cancel</button>
      <button id="switchKeyBtn" class="btn primary" data-act="setToken">Set</button></div>`);
}

function sidebar() {
  const pid = State.route.pid;
  const envOpts = State.envs.map((e) =>
    `<option value="${esc(e.id)}" ${e.id === State.env ? "selected" : ""}>${esc(e.id)}</option>`).join("");
  const curEnv = State.envs.find((e) => e.id === State.env) || {};
  return `<div class="sidebar">
    <div class="brand">
      <span class="star">✦</span><span class="name">Incant</span><div class="grow"></div>
      <button class="theme-btn" aria-label="${State.theme === "light" ? "Switch to dark theme" : "Switch to light theme"}" data-act="theme">${State.theme === "light" ? "☾" : "☀"}</button>
    </div>
    <div class="sect">LIBRARY</div>
    <a class="nav ${State.route.name === "prompts" ? "active" : ""}" href="#/prompts" data-act="go" data-hash="#/prompts">
      <span class="gl">◈</span><span>Prompts</span></a>
    ${subnav(pid)}
    <a class="nav ${State.route.name === "segments" ? "active" : ""}" href="#/segments" data-act="go" data-hash="#/segments">
      <span class="gl">⬡</span><span>Segments</span></a>
    <a class="nav ${State.route.name === "play" ? "active" : ""}" href="#/play" data-act="go" data-hash="#/play">
      <span class="gl">▶</span><span>Playground</span></a>
    <a class="nav ${State.route.name === "audit" ? "active" : ""}" href="#/audit" data-act="go" data-hash="#/audit">
      <span class="gl">◷</span><span>Audit</span></a>
    <a class="nav ${State.route.name === "access" ? "active" : ""}" href="#/access" data-act="go" data-hash="#/access">
      <span class="gl">⚿</span><span>Access</span></a>
    <div class="spacer"></div>
    ${pid ? `<button class="tweak-btn" data-act="toggleTweak"><span>✦</span> How to publish</button>` : ""}
    <div class="envbar">
      <span class="pill faint" style="font-size:11px;letter-spacing:.06em">ENV</span>
      <select class="envsel" aria-label="Environment" data-act="env">${envOpts}</select>
      ${curEnv.protected ? '<span class="pill warn">PROTECTED</span>' : ""}
      ${curEnv.track_tip ? '<span class="pill live" title="valid saves publish automatically in this environment">auto-publish</span>' : ""}
    </div>
    ${accountChip()}
  </div>`;
}

// A fixed top bar (shown only under 920px via CSS) with the app mark, current env, and
// a hamburger that toggles the off-canvas nav drawer. The scrim closes the drawer.
function topbar() {
  return `<div class="topbar">
    <button class="hamburger" aria-label="Menu" aria-expanded="${State.navOpen ? "true" : "false"}" data-act="navToggle">☰</button>
    <span class="topbar-mark"><span class="star">✦</span>Incant</span>
    <span class="grow"></span>
    <span class="pill acc">${esc(State.env)}</span>
  </div>`;
}
function shell(mainHtml) {
  return `${topbar()}
    <button class="nav-scrim btn-bare" aria-label="Close menu" data-act="navClose"></button>
    <div class="shell">${sidebar()}
    <div class="main" id="main">${mainHtml}</div>
    ${State.tweakOpen && State.route.pid ? tweakPanel() : ""}
  </div>`;
}

// ── "How to publish" — a live progress panel for the route's prompt ──
// Fetches versions + drafts + rules (cached per pid+env, refetched on open) and lights
// up each step as done/next. Falls back to the static list on fetch failure.
const _tweak = { pid: null, env: null, data: null, loading: false };
const TWEAK_NEXT_LABELS = {
  edit: "Edit the prompt", save: "Save your edits",
  publish: "Publish latest edits", cleanup: "Remove the test rule",
};

// Pure state mapping: fixture data → step states + the single next action. Testable.
function computeTweakSteps(pid, d) {
  const versions = d.versions || [], drafts = d.drafts || [], rules = d.rules || [];
  const wv = versions.find((v) => v.is_default) || versions.find((v) => v.live_sha) || versions[0] || {};
  const tipAhead = wv.tip_ahead || 0;
  const versionIsLive = !!wv.live_sha;
  const hasOpenDraft = drafts.some((x) => x.status !== "committed" && x.status !== "discarded");
  // A live rule serving this prompt @tip (the "testing with a group" signal).
  const tipRuleActive = activeRulesFor(rules, pid).some((r) => (serveTarget(r.serve) || {}).tip);
  // Cleanup only surfaces once the tip is already live but a @tip test rule lingers.
  const cleanupVisible = tipRuleActive && tipAhead === 0 && versionIsLive;

  const s1done = tipAhead > 0 || hasOpenDraft;   // Edit
  const s2done = tipAhead > 0;                    // Save edits
  const s3done = tipRuleActive;                   // Test with a group
  const s5done = tipAhead === 0 && versionIsLive; // Publish (nothing waiting)

  // Exactly one step is the primary "next" action. A lingering @tip test rule after
  // publish (cleanup) wins only when nothing else is in flight.
  let nextKey;
  if (!s1done) nextKey = cleanupVisible ? "cleanup" : "edit";
  else if (!s2done) nextKey = "save";
  else if (tipAhead > 0) nextKey = "publish";
  else if (cleanupVisible) nextKey = "cleanup";
  else nextKey = "edit";

  const steps = [
    { key: "edit", n: "1", label: "Edit", sub: "Change the text — nothing goes live yet", target: "draft", done: s1done },
    { key: "save", n: "2", label: "Save edits", sub: "Validated and recorded in this version's history", target: "draft?tab=review", done: s2done },
    { key: "test", n: "3", label: "Test with a group", sub: "Show the edits to a chosen group first", target: "rules", done: s3done },
    { key: "verify", n: "4", label: "Verify", sub: "Check the rendered diff against what's live", target: "draft?tab=diff", done: false, neutral: true },
    { key: "publish", n: "5", label: "Publish", sub: "Make the edits live for everyone", target: "pointers", done: s5done },
  ];
  if (cleanupVisible)
    steps.push({ key: "cleanup", n: "6", label: "Clean up", sub: "Remove the now-redundant test rule", target: "rules", done: false });
  return { steps, nextKey };
}

async function loadTweakData(pid) {
  if (!pid) return;
  _tweak.loading = true;
  const env = State.env;
  try {
    const [dv, dl, rd] = await Promise.all([
      GET(`/mgmt/prompts/${enc(pid)}/versions?environment=${enc(env)}`),
      GET(`/mgmt/prompts/${enc(pid)}/drafts`).catch(() => ({ drafts: [] })),
      GET(`/mgmt/envs/${enc(env)}/rules`).catch(() => ({ rules: [] })),
    ]);
    _tweak.data = { versions: dv.versions || [], drafts: dl.drafts || [], rules: rd.rules || [] };
  } catch (e) {
    _tweak.data = { error: true };
  } finally {
    _tweak.pid = pid; _tweak.env = env; _tweak.loading = false;
    // Targeted refresh — don't re-render the whole screen just for the side panel.
    const host = el("tweakPanel");
    if (host && State.tweakOpen && State.route.pid === pid) host.innerHTML = tweakPanelInner(pid);
  }
}

function tweakStepRow(pid, s, isNext) {
  const numCls = s.done ? "tnum done" : isNext ? "tnum next" : "tnum";
  const glyph = s.done ? "✓" : s.n;
  return `<a class="tstep${isNext ? " next" : ""}" href="#/p/${enc(pid)}/${s.target}" data-act="go" data-hash="#/p/${enc(pid)}/${s.target}">
    <span class="${numCls}">${glyph}</span>
    <span style="flex:1"><span style="font-size:12.5px;font-weight:600;display:block">${esc(s.label)}</span>
      <span style="font-size:11.5px;color:var(--mut)">${esc(s.sub)}</span></span></a>`;
}
// The original static list — shown while loading and as the fetch-failure fallback.
function tweakStaticRows(pid) {
  const steps = [
    ["Edit", "Change the text — nothing goes live yet", "draft"],
    ["Save edits", "Validated and reviewed, ready to try", "draft?tab=review"],
    ["Test with a group", "Show the edits to a chosen group first", "rules"],
    ["Check the result", "Preview and compare against what's live", "draft?tab=diff"],
    ["Publish", "Make the edits live for everyone", "pointers"],
  ];
  return steps.map(([t, s, target], i) =>
    `<a class="tstep" href="#/p/${enc(pid)}/${target}" data-act="go" data-hash="#/p/${enc(pid)}/${target}">
      <span class="tnum">${i + 1}</span>
      <span style="flex:1"><span style="font-size:12.5px;font-weight:600;display:block">${t}</span>
      <span style="font-size:11.5px;color:var(--mut)">${s}</span></span></a>`).join("");
}
function tweakPanelInner(pid) {
  const header = `<div style="display:flex;align-items:center;gap:8px;margin-bottom:6px">
      <span style="color:var(--acc-ink)">✦</span>
      <span class="serif" style="font-style:italic;font-size:19px">How to publish a change</span>
      <button type="button" class="link btn-bare" style="margin-left:auto" aria-label="Close" data-act="toggleTweak">✕</button></div>
    <div style="font-size:11.5px;color:var(--mut);margin-bottom:14px">Improve a live prompt without starting a whole new version. Your edits change nothing until the final step — publishing is the one moment that goes live for everyone.</div>`;
  const footer = `<div style="border-top:1px solid var(--line2);margin-top:14px;padding-top:12px;font-size:11.5px;color:var(--faint)">Testing with a group first is how you try edits safely. Once you publish, you can stop testing and remove the group.</div>`;
  const ready = _tweak.pid === pid && _tweak.env === State.env && !_tweak.loading && _tweak.data && !_tweak.data.error;
  if (!ready) return header + tweakStaticRows(pid) + footer;   // loading or fetch failed
  const { steps, nextKey } = computeTweakSteps(pid, _tweak.data);
  const nextStep = steps.find((s) => s.key === nextKey) || steps[0];
  const nextBtn = `<div style="margin-bottom:12px">
    <button class="btn primary sm" style="width:100%" data-act="go" data-hash="#/p/${enc(pid)}/${nextStep.target}">${esc(TWEAK_NEXT_LABELS[nextKey] || "Continue")} →</button>
    <div style="font-size:11.5px;color:var(--faint);text-align:center;margin-top:5px">your next step</div></div>`;
  const rows = steps.map((s) => tweakStepRow(pid, s, s.key === nextKey)).join("");
  return header + nextBtn + rows + footer;
}
function tweakPanel() {
  const pid = State.route.pid;
  // Fetch (or refetch) when data is missing/stale for this pid+env and none is in flight.
  if ((!_tweak.data || _tweak.pid !== pid || _tweak.env !== State.env) && !_tweak.loading) loadTweakData(pid);
  return `<div class="tweakpanel" id="tweakPanel">${tweakPanelInner(pid)}</div>`;
}

// ── screens ──────────────────────────────────────────────────────────
// Library filters — single-select chips that combine with the search text. State is
// in-memory (persists across renders); the fetched data is cached so search/filter
// changes rebuild only the list, never re-fetch.
const PROMPT_FILTERS = [
  ["all", "All"], ["edits", "Unpublished edits"], ["testing", "Being tested"],
  ["notlive", "Not live"], ["review", "Needs review"], ["recent", "Recently published"],
];
let _promptsFilter = { key: "all", q: "" };
let _promptsCache = null;   // { env, data, rules }

function within7Days(iso) {
  if (!iso) return false;
  const t = new Date(iso).getTime();
  if (!isFinite(t)) return false;
  const diff = Date.now() - t;
  return diff >= 0 && diff < 7 * 24 * 3600 * 1000;
}
// Pure predicates (testable): does a prompt row match a filter chip / the search text?
function promptMatchesFilter(p, key, rules) {
  switch (key) {
    case "edits": return (p.tip_ahead > 0) || (p.open_drafts > 0);
    case "testing": return testingFor(rules, p.prompt_id, p.live_version).length > 0;
    case "notlive": return p.newest_version != null && !p.newest_version_live;
    case "review": return p.open_drafts > 0;
    case "recent": return within7Days(p.live_at);
    case "all": default: return true;
  }
}
function promptMatchesSearch(p, q) {
  if (!q) return true;
  const s = q.toLowerCase();
  return String(p.prompt_id || "").toLowerCase().includes(s) ||
         String(p.description || "").toLowerCase().includes(s);
}
function promptRowHtml(p, rules) {
  const bits = [];
  // green — live for everyone
  if (p.live && p.live_version != null) bits.push(statusLine("live", `Version ${p.live_version} live`));
  // amber — being tested with a group (dedupe by rendered label)
  const seen = new Set();
  for (const t of testingFor(rules, p.prompt_id, p.live_version)) {
    const lbl = t.tip ? "draft testing" : (t.version != null ? `v${t.version} testing`
              : (t.label ? `${esc(t.label)} testing` : "testing"));
    if (seen.has(lbl)) continue; seen.add(lbl);
    bits.push(pill("warn", lbl));
  }
  // indigo — unpublished edits waiting
  if (p.tip_ahead > 0) bits.push(pill("acc", `${p.tip_ahead} ${plural(p.tip_ahead, "edit")} waiting`));
  // neutral — a newer version exists but was never published here
  if (p.newest_version != null && p.newest_version_live === false &&
      (!p.live || p.newest_version !== p.live_version))
    bits.push(pill("neutral", `v${p.newest_version} draft, not live`));

  const upd = p.updated ? `${ago(p.updated.when)} · ${esc(p.updated.who)}` : "";
  const desc = String(p.description || "").trim();
  const descLine = desc
    ? `<div class="prow-desc">${esc(desc.length > 110 ? desc.slice(0, 110) + "…" : desc)}</div>` : "";
  // The whole row is the link (keyboard + middle-click); the "Details →" affordance is
  // decorative content inside it (a real control nested in <a> would be invalid markup).
  return `<a class="prow click" href="#/p/${enc(p.prompt_id)}/overview" data-pid="${esc(p.prompt_id)}" data-act="go" data-hash="#/p/${enc(p.prompt_id)}/overview">
    <div class="prow-main">
      <div class="prow-id">${esc(p.prompt_id)}</div>
      ${descLine}
      <div class="prow-status">${bits.join("") || '<span class="faint" style="font-size:12px">Not live yet</span>'}</div>
    </div>
    <span class="prow-meta">${upd}</span>
    <div class="prow-actions">
      <span class="btn primary sm" aria-hidden="true">Details →</span>
    </div></a>`;
}
function promptListHtml() {
  if (!_promptsCache) return "";
  const { data, rules } = _promptsCache;
  const { key, q } = _promptsFilter;
  let html = "", total = 0;
  for (const proj of data.projects) {
    const matched = proj.prompts.filter((p) =>
      promptMatchesFilter(p, key, rules) && promptMatchesSearch(p, q));
    if (!matched.length) continue;
    total += matched.length;
    html += `<div class="groupname">${esc(proj.project.toUpperCase())}</div>
      <div class="card" style="margin-bottom:18px">${matched.map((p) => promptRowHtml(p, rules)).join("")}</div>`;
  }
  if (!total) {
    const lbl = (PROMPT_FILTERS.find(([k]) => k === key) || [, "All"])[1];
    const fNote = key !== "all" ? ` under <b>${esc(lbl)}</b>` : "";
    const qNote = q ? ` matching “${esc(q)}”` : "";
    return `<div class="empty">No prompts${fNote}${qNote}.
      <div style="margin-top:8px;font-size:11.5px">Try a different filter or clear the search.</div></div>`;
  }
  return html;
}
function updatePromptList() { const host = el("promptList"); if (host) host.innerHTML = promptListHtml(); }

async function screenPrompts() {
  const [data, rulesData] = await Promise.all([
    GET(`/mgmt/overview?environment=${enc(State.env)}`),
    GET(`/mgmt/envs/${enc(State.env)}/rules`).catch(() => ({ rules: [] })),
  ]);
  const rules = rulesData.rules || [];
  _promptsCache = { env: State.env, data, rules };
  const nPrompts = data.projects.reduce((s, p) => s + p.prompts.length, 0);
  const allPrompts = data.projects.flatMap((p) => p.prompts);
  const counts = {};
  for (const [k] of PROMPT_FILTERS) counts[k] = allPrompts.filter((p) => promptMatchesFilter(p, k, rules)).length;
  const chips = PROMPT_FILTERS.map(([k, lbl]) =>
    `<button type="button" class="chip btn-bare ${k === _promptsFilter.key ? "active" : ""}" aria-pressed="${k === _promptsFilter.key}" data-act="promptFilter" data-key="${k}">${esc(lbl)} (${counts[k]})</button>`).join("");

  el("main").innerHTML = `<div class="screen">
    <div class="h1row">
      <div><div class="page-h1">Prompts</div>
        <div class="page-sub">${data.projects.length} ${plural(data.projects.length, "project")} · ${nPrompts} ${plural(nPrompts, "prompt")} · showing what's live in ${esc(State.env)}</div></div>
      <div class="grow"></div>
      <input class="search" id="promptSearch" placeholder="Search id or description…" data-act="search" spellcheck="false" value="${esc(_promptsFilter.q)}">
      <button class="btn primary" data-act="newPrompt">New prompt</button></div>
    <div id="promptFilters" style="display:flex;gap:8px;flex-wrap:wrap;margin-bottom:18px">${chips}</div>
    <div id="promptList">${promptListHtml()}</div>
    <div style="font-size:11.5px;color:var(--faint);margin-top:4px">Any prompt can be included by any other — shared fragments are just prompts.</div></div>`;
}

async function screenOverview() {
  const pid = State.route.pid;
  const [d, rulesData] = await Promise.all([
    GET(`/mgmt/prompts/${enc(pid)}/versions?environment=${enc(State.env)}`),
    GET(`/mgmt/envs/${enc(State.env)}/rules`).catch(() => ({ rules: [] })),
  ]);
  const rules = rulesData.rules || [];
  const liveV = d.versions.find((v) => v.is_default) || d.versions.find((v) => v.live_sha) || null;
  const liveVersion = liveV ? liveV.version : null;
  const testing = testingFor(rules, pid, liveVersion);

  // ── status hero: only the rows that apply, in fixed order ──
  const heroRows = [];
  if (liveV && liveV.live_sha) {
    const sub = `Published ${ago(liveV.live_at)}${liveV.live_by ? " by " + esc(liveV.live_by) : ""}`;
    heroRows.push(`<div class="hero-row live">
      <span class="hdot live"></span>
      <div class="hero-body"><div class="t">Version ${liveV.version} is live for everyone</div>
        <div class="s">${sub}</div></div>
      <button class="btn olive sm" data-act="go" data-hash="#/p/${enc(pid)}/compare?b=${enc(liveV.version + "@live")}">View live text</button>
    </div>`);
  }
  for (const t of testing) {
    const title = t.tip ? "The latest unpublished draft is being tried by a group"
      : t.version != null ? `Version ${t.version} is being tested with a group`
      : t.label ? `Label ${esc(t.label)} is being tested with a group`
      : "A change is being tested with a group";
    const desc = t.rule.comment ? esc(t.rule.comment) : describeWhen(t.rule.when);
    heroRows.push(`<div class="hero-row">
      <span class="hdot testing"></span>
      <div class="hero-body"><div class="t">${title}</div>
        <div class="s">${desc} · not live for everyone</div></div>
      <a class="link mut" style="font-size:12px" href="#/p/${enc(pid)}/rules" data-act="go" data-hash="#/p/${enc(pid)}/rules">Manage →</a>
    </div>`);
  }
  if (liveV && liveV.tip_ahead > 0) {
    const subjects = (liveV.history || []).slice(0, liveV.tip_ahead).map((h) => h.subject).filter(Boolean);
    let sline = subjects.join(" + ");
    if (sline.length > 80) sline = sline.slice(0, 77) + "…";
    const meta = `last edit ${ago(liveV.tip_when)}${liveV.tip_author ? " by " + esc(liveV.tip_author) : ""} · not live yet`;
    heroRows.push(`<div class="hero-row draft">
      <span class="hdot draft"></span>
      <div class="hero-body"><div class="t">${liveV.tip_ahead} unpublished ${plural(liveV.tip_ahead, "edit")} on Version ${liveV.version}</div>
        <div class="s">${sline ? esc(sline) + " · " : ""}${meta}</div></div>
      <button class="btn primary sm" data-act="go" data-hash="#/p/${enc(pid)}/pointers?v=${liveV.version}">Review &amp; publish →</button>
    </div>`);
  }
  if (!heroRows.length) heroRows.push(`<div class="hero-row">
    <span class="hdot" style="background:var(--faint)"></span>
    <div class="hero-body"><div class="t">Not published yet</div>
      <div class="s">No version of this prompt is live in ${esc(State.env)}.</div></div></div>`);

  // ── technical details (SHAs + rules_version) ──
  const techLines = d.versions.map((v) =>
    `v${v.version} · live ${esc(v.live_sha || "—")} · tip ${esc(v.tip_sha || "—")}`).join("<br>") +
    `<br>rules_version ${esc(String(rulesData.rules_version ?? "—"))}`;

  // ── all versions ──
  const vrows = d.versions.map((v) => {
    const chip = v.label ? pill("acc", esc(v.label)) : "";
    // A non-live version being served to a group reads as "Testing"; the live version
    // itself always reads "Live for everyone" (its draft-testing shows in the hero).
    const vTesting = testing.some((t) => t.version === v.version && v.version !== liveVersion);
    let status;
    if (v.status === "archived")
      status = `<span class="faint" style="font-size:12px">Archived · still serving where pinned, no new changes</span>`;
    else if (v.version === liveVersion && v.live_sha) status = statusLine("live", "Live for everyone");
    else if (vTesting) status = statusLine("testing", "Testing with a group");
    else if (v.live_sha) status = statusLine("live", "Live for everyone");
    else status = `<span class="faint" style="font-size:12px">Not live</span>`;
    const edits = v.tip_ahead > 0 ? pill("acc", `${v.tip_ahead} ${plural(v.tip_ahead, "edit")} waiting`) : "";
    const meta = v.tip_author ? `Updated ${ago(v.tip_when)} · ${esc(v.tip_author)}` : "";
    return `<div class="prow">
      <span style="font-size:14px;font-weight:700;width:34px;flex:none">v${v.version}</span>
      ${chip}${status}${edits}
      <div class="grow"></div>
      <span class="prow-meta">${meta}</span>
      <a class="link sm" style="margin-left:10px;font-size:12px" href="#/p/${enc(pid)}/draft?v=${v.version}" data-act="go" data-hash="#/p/${enc(pid)}/draft?v=${v.version}">Open</a>
    </div>`;
  }).join("");

  // ── side cards (kept) ──
  const vars = d.variables.map((vr) => {
    const cls = vr.required ? "req" : "opt";
    const over = vr.overridden ? " over" : "";
    return `<div class="kv"><span class="varname">${esc(vr.name)}</span><span class="muted">${esc(vr.type)}</span>
      <button type="button" class="reqtag btn-bare ${cls}${over}" data-act="toggleReq" data-name="${esc(vr.name)}" data-v="${d.versions.find(x=>x.is_default)?.version||""}" data-req="${vr.required}" aria-label="${esc(vr.name)} is ${vr.required ? "required" : "optional"} — toggle">${vr.required ? "required" : "optional"}${vr.overridden ? " ·" : ""}</button></div>`;
  }).join("") || '<div class="faint">No variables.</div>';

  const includes = d.includes.length
    ? d.includes.map((i) => `<div style="display:flex;gap:8px;align-items:center"><span style="color:var(--acc-ink)">↳</span><span class="mono" style="font-size:11px">${esc(i)}</span></div>`).join("")
    : '<div class="faint">No includes.</div>';

  el("main").innerHTML = `<div class="screen">
    <div class="crumb"><a href="#/prompts" data-act="go" data-hash="#/prompts">Prompts</a> / ${esc(pid.split("/")[0])} /</div>
    <div class="h1row">
      <div><div class="page-h1 mono">${esc(pid)}</div>
        <div class="page-sub">${d.versions.length} ${plural(d.versions.length, "version")} · ${esc(State.env)}</div></div>
      <div class="grow"></div>
      <button type="button" class="link mut btn-bare" data-act="projectSettings" data-project="${esc(pid.split("/")[0])}" data-prompt="${esc(pid)}" style="font-size:12px">⚙ Project settings</button>
      <button class="btn primary" data-act="go" data-hash="#/p/${enc(pid)}/draft">Edit this prompt</button></div>
    <div class="hero">${heroRows.join("")}</div>
    ${techDetails(techLines, "commit SHAs, rules version")}
    <div style="display:flex;align-items:center;margin-top:22px">
      <div class="groupname" style="margin:0">ALL VERSIONS</div>
      <div class="grow"></div>
      <button type="button" class="link mut btn-bare" data-act="newVersionExplain" style="font-size:12px">＋ New version…</button></div>
    <div class="card">${vrows}</div>
    <div class="panelrow" style="margin-top:18px">
      <div class="card pad" style="flex:1 1 300px;min-width:0">
        <div class="groupname">Effective variables</div>
        <div class="kvs">${vars}</div>
        <div style="font-size:11.5px;color:var(--faint);margin-top:12px;border-top:1px solid var(--line2);padding-top:10px">Inferred from the template — click required/optional to override. Overrides carry forward across edits.</div>
      </div>
      <div class="card pad" style="flex:1 1 260px;min-width:0"><div class="groupname">Includes</div>${includes}</div>
    </div></div>`;
}

// ── project settings (governance) ────────────────────────────────────
// Self-review policy lives on the project, not the prompt. Opening this reads the
// current value with a no-op PATCH (returns the state; admins only). A non-admin gets
// 403 → we fall back to reading the value off an open draft and show it read-only.
async function openProjectSettings(project, prompt) {
  window._projSettings = { project, prompt, selfOk: null, canEdit: false, err: "", loading: true };
  openModal(`<div id="projSettingsBody">${projectSettingsBodyHtml(window._projSettings)}</div>`);
  const ps = window._projSettings;
  try {
    const r = await PATCH(`/mgmt/projects/${enc(project)}`, {});   // no-op read
    ps.selfOk = r.allow_self_review; ps.canEdit = true;
  } catch (e) {
    if (e && e.status === 403) {
      ps.canEdit = false;
      try {
        const dl = await GET(`/mgmt/prompts/${enc(prompt)}/drafts`);
        if (dl.drafts && dl.drafts.length) {
          const d = await GET(`/mgmt/drafts/${enc(dl.drafts[0].id)}`);
          ps.selfOk = d.allow_self_review;
        }
      } catch (_) { /* leave selfOk unknown */ }
    } else {
      ps.err = errText(e);
    }
  } finally {
    ps.loading = false;
    renderProjectSettings();
  }
}
function projectSettingsBodyHtml(ps) {
  const ON = "Anyone can approve their own edits";
  const OFF = "A different person must approve";
  let control;
  if (ps.loading) {
    control = `<div class="empty">Loading…</div>`;
  } else if (ps.selfOk == null && !ps.canEdit) {
    control = `<div class="faint" style="font-size:12px">The self-review policy is managed by admins — ask one to view or change it.</div>`;
  } else {
    const selfOk = !!ps.selfOk;
    const effect = selfOk
      ? "Authors can approve and commit their own drafts — no second reviewer needed."
      : "Every draft needs an approval from someone other than its author before it can be saved.";
    const stateLine = `<div style="display:flex;align-items:center;gap:10px;margin-bottom:8px">
        <span class="pill ${selfOk ? "live" : "warn"}">${selfOk ? "self-review on" : "four-eyes"}</span>
        <span style="font-size:13px;font-weight:600">${selfOk ? ON : OFF}</span></div>
      <div style="font-size:12px;color:var(--mut)">${effect}</div>`;
    control = ps.canEdit
      ? `${stateLine}
        <div style="display:flex;gap:8px;margin-top:14px;flex-wrap:wrap">
          <button class="btn ${selfOk ? "primary" : ""}" data-act="setSelfReview" data-to="true">${ON}</button>
          <button class="btn ${!selfOk ? "primary" : ""}" data-act="setSelfReview" data-to="false">${OFF}</button></div>`
      : `${stateLine}
        <div class="faint" style="font-size:11.5px;margin-top:12px;border-top:1px solid var(--line2);padding-top:10px">Read-only — admins can change this.</div>`;
  }
  return `<h3>Project settings</h3>
    <p class="hint">Settings for project <span class="mono">${esc(ps.project)}</span>.</p>
    ${ps.err ? `<div class="banner danger" style="margin-bottom:12px"><span style="font-size:12.5px;font-weight:600">${esc(ps.err)}</span></div>` : ""}
    <div class="groupname">Who can approve edits</div>
    ${control}
    <div class="modal-actions"><button class="btn" data-act="closeModal">Close</button></div>`;
}
function renderProjectSettings() {
  const b = el("projSettingsBody");
  if (b && window._projSettings) b.innerHTML = projectSettingsBodyHtml(window._projSettings);
}

// ── draft page: autosave ─────────────────────────────────────────────
// Autosave state lives at module scope so a pending save survives the re-render
// that a tab switch or navigation triggers — a keystroke is never dropped.
const Auto = { draftId: null, timer: null, seq: 0, applied: 0, inflight: null };
let _draftNotice = null;   // one-shot notice shown atop the review tab (e.g. after a 412)

function scheduleAutosave() {
  clearTimeout(Auto.timer);
  Auto.timer = setTimeout(fireAutosave, 800);   // ~800ms debounce after the last keystroke
}
function fireAutosave() {
  clearTimeout(Auto.timer); Auto.timer = null;
  const ta = el("draftTa");
  if (!ta || !Auto.draftId) return;
  const draftId = Auto.draftId, content = ta.value, seq = ++Auto.seq;
  setAutosaveChip("saving");
  Auto.inflight = (async () => {
    try {
      const r = await PUT(`/mgmt/drafts/${draftId}/content`, { content });
      if (seq < Auto.applied) return;   // out-of-order guard: a newer save already landed
      Auto.applied = seq;
      if (window._dp && window._dp.draft && window._dp.draft.id === r.id) {
        applyDraftUpdate(r);
        setAutosaveChip("saved");
        doRenderDraft();                // refresh the test render off the saved content
      }
    } catch (e) {
      setAutosaveChip("failed");
    }
  })();
  return Auto.inflight;
}
// Fire any pending debounce immediately and await the in-flight PUT — called before
// the DOM is replaced (render) and before a commit, so no edit is lost or stale.
async function flushAutosave() {
  if (Auto.timer) fireAutosave();
  if (Auto.inflight) { try { await Auto.inflight; } catch (_) {} }
}
function setAutosaveChip(state) {
  const c = el("autoChip"); if (!c) return;
  if (state === "saving") { c.textContent = "saving…"; c.className = "autochip"; }
  else if (state === "saved") { c.textContent = "saved just now"; c.className = "autochip ok"; }
  else if (state === "failed") { c.textContent = "save failed"; c.className = "autochip err"; }
  else { c.textContent = "saved"; c.className = "autochip faint"; }
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

// ── draft page: header pieces ────────────────────────────────────────
function lintChipHtml(draft) {
  const lint = draft.lint || {};
  return lint.status === "valid"
    ? `<span class="pill live">✓ lint clean</span>`
    : `<span class="pill danger">${esc(lint.error || "invalid")}</span>`;
}
function varsLine(draft) {
  if (!draft.variables) return "";
  const req = draft.variables.required.map((n) => "<b>" + esc(n) + "</b>").join(" · ");
  const opt = draft.variables.optional.length
    ? " · " + draft.variables.optional.map((n) => esc(n) + "?").join(" · ") : "";
  return `variables: ${req}${opt}`;
}
// The single primary action — its label is always the next step in the flow.
function draftPrimary(draft) {
  const lint = draft.lint || {};
  if (lint.status !== "valid")
    return `<button class="btn primary" disabled>Fix template error</button>`;
  const need = draft.review_policy || 0, have = draft.reviewers.length;
  if (need > 0 && have < need)   // a pointer to the review tab, not a dead end
    return `<button class="btn primary" data-act="draftTab" data-tab="review">Awaiting ${need - have} approval(s)</button>`;
  return `<button class="btn primary" data-act="openCommit">Save edits…</button>`;
}
function applyDraftUpdate(r) {
  window._dp.draft = r;
  const lc = el("draftLintChip"); if (lc) lc.innerHTML = lintChipHtml(r);
  const pw = el("draftPrimaryWrap"); if (pw) pw.innerHTML = draftPrimary(r);
  const vl = el("varLine"); if (vl) vl.innerHTML = varsLine(r);
}

async function screenDraft() {
  await flushAutosave();               // never lose a pending edit when re-entering
  const pid = State.route.pid, q = State.route.q;
  const vq = q.v ? parseInt(q.v) : null;
  const tab = q.tab || "write";
  el("main").innerHTML = `<div class="empty">Opening draft…</div>`;

  if (!State.me) State.me = await GET(`/mgmt/whoami`);
  const [list, dv] = await Promise.all([
    GET(`/mgmt/prompts/${enc(pid)}/drafts`),
    GET(`/mgmt/prompts/${enc(pid)}/versions?environment=${enc(State.env)}`),
  ]);

  // Draft resolution: open ?draft; else the current user's own draft on ?v; else
  // their newest open draft; else create one. NEVER auto-open another author's draft.
  let draftId = q.draft;
  if (!draftId) {
    const mine = list.drafts.filter((d) => d.author === State.me.name);
    const chosen = (vq != null && mine.find((d) => d.version_number === vq)) || mine[0] || null;
    if (chosen) draftId = chosen.id;
  }
  if (!draftId) {
    const targetV = vq || dv.versions.find((x) => x.is_default)?.version || dv.versions[0]?.version || 1;
    const created = await POST(`/mgmt/prompts/${enc(pid)}/drafts`, {
      version_number: targetV, seed_from_version: targetV, title: "Draft v" + targetV,
    });
    draftId = created.id;
    // The listing was fetched pre-creation — add the new draft so the switcher shows it.
    list.drafts.unshift({ id: created.id, title: created.title, author: created.author,
      status: created.status, version_number: created.version_number,
      base_sha: created.base_sha, updated_at: null, approvals: [] });
  }

  const [draft, tcs] = await Promise.all([
    GET(`/mgmt/drafts/${enc(draftId)}`),
    GET(`/mgmt/prompts/${enc(pid)}/test-contexts`),
  ]);

  // Page state survives in-tab updates (test contexts, diff controls); autosave is
  // tracked separately in Auto so it isn't lost across re-renders.
  window._dp = {
    draft, drafts: list.drafts, versions: dv.versions, tcs: tcs.test_contexts,
    // No saved contexts is not a dead end — fall back to an ad-hoc JSON context.
    tcActive: tcs.test_contexts[0]?.name || "__custom",
    customVars: null, customFlags: null,
    diffAgainst: "base", diffMode: "source", diffTc: tcs.test_contexts[0]?.name || null,
    // Review tab: which context drives the rendered before/after, and whether the
    // secondary source-diff section is expanded.
    reviewTc: tcs.test_contexts[0]?.name || null, reviewSrcOpen: false,
    pendingMsg: "",
  };
  window._draft = draft;   // codebase idiom — keep the alias current
  Auto.draftId = draft.id;

  const switcherOpts = list.drafts.map((d) => {
    // "awaiting review" is only meaningful under a review policy; otherwise open
    // drafts are just open.
    const st = d.status === "approved" ? "approved"
             : draft.review_policy > 0 ? "awaiting review" : "open";
    const label = [`v${d.version_number}`, esc(d.author), ago(d.updated_at), st]
      .filter(Boolean).join(" · ");
    return `<option value="${esc(d.id)}"${d.id === draft.id ? " selected" : ""}>${label}</option>`;
  }).join("") +
    `<option value="__new">＋ New draft on v${draft.version_number}…</option>` +
    `<option value="__discard">Discard this draft…</option>`;

  const tabs = [["write", "Write"], ["diff", "What changed"], ["review", "Review"]];
  const tabsHtml = tabs.map(([id, label]) =>
    `<button type="button" role="tab" aria-selected="${tab === id}" class="tab btn-bare ${tab === id ? "active" : ""}" data-act="draftTab" data-tab="${id}">${label}</button>`).join("");

  const body = tab === "diff" ? draftDiffTabShell(window._dp)
             : tab === "review" ? draftReviewTab(window._dp)
             : draftWriteTab(window._dp);

  el("main").innerHTML = `<div class="screen">
    <div class="crumb"><a href="#/prompts" data-act="go" data-hash="#/prompts">Prompts</a> /
      <a href="#/p/${enc(pid)}/overview" data-act="go" data-hash="#/p/${enc(pid)}/overview">${esc(pid)}</a> /</div>
    <div class="h1row"><span class="h1 sm serif">Edit — <i>v${draft.version_number}</i></span>
      <span class="sub">based on <span class="mono">${esc(draft.base_sha || "—")}</span> ·
        <span class="autochip faint" id="autoChip" aria-live="polite">saved</span> ·
        <span id="draftLintChip">${lintChipHtml(draft)}</span></span>
      <div class="grow"></div>
      <select class="envsel" aria-label="Switch draft" data-act="switchDraft" style="max-width:240px">${switcherOpts}</select>
      <span id="draftPrimaryWrap">${draftPrimary(draft)}</span></div>
    <div class="tabs" role="tablist" aria-label="Draft views">${tabsHtml}</div>
    <div id="draftTabBody">${body}</div></div>`;

  if (tab === "write" && window._dp.tcActive) doRenderDraft();
  if (tab === "diff") loadDraftDiff();
  if (tab === "review") { loadReviewRendered(); loadReviewComments(); }
}

// Prefill skeleton for the ad-hoc context: every required variable, ready to fill in.
function customVarsSkeleton(draft) {
  const req = draft.variables?.required || [];
  if (!req.length) return "{}";
  return JSON.stringify(Object.fromEntries(req.map((n) => [n, ""])), null, 2);
}

function draftWriteTab(dp) {
  const draft = dp.draft;
  if (dp.customVars == null) dp.customVars = customVarsSkeleton(draft);
  if (dp.customFlags == null) dp.customFlags = "{}";
  const chips = dp.tcs.map((t) =>
    `<button type="button" class="chip btn-bare ${t.name === dp.tcActive ? "active" : ""}" aria-pressed="${t.name === dp.tcActive}" data-act="tc" data-name="${esc(t.name)}">${esc(t.name)}</button>`).join("") +
    `<button type="button" class="chip btn-bare ${dp.tcActive === "__custom" ? "active" : ""}" aria-pressed="${dp.tcActive === "__custom"}" data-act="tc" data-name="__custom">＋ custom</button>`;
  const custom = dp.tcActive === "__custom" ? `
      <div style="padding:8px 16px 0;display:flex;flex-direction:column;gap:8px">
        <div class="field" style="margin-bottom:0"><label>Variables (JSON)</label>
          <textarea id="tcVars" data-act="tcVarsInput" spellcheck="false" style="min-height:72px">${esc(dp.customVars)}</textarea></div>
        <div class="field" style="margin-bottom:0"><label>Flags (JSON)</label>
          <textarea id="tcFlags" data-act="tcFlagsInput" spellcheck="false" style="min-height:40px">${esc(dp.customFlags)}</textarea></div>
        <button type="button" class="link btn-bare" style="font-size:12px" data-act="saveTestContext">Save as a test context…</button>
      </div>` : "";
  return `<div class="editor-wrap">
    <div class="card editor">
      <div class="ed-head"><span class="mono">v${draft.version_number}.j2</span><span>·</span><span>Jinja2</span>
        <span style="margin-left:auto" class="mono">${esc(draft.id)}</span></div>
      <textarea class="ta" id="draftTa" data-act="draftInput" spellcheck="false">${esc(draft.content || "")}</textarea>
      <div class="ed-foot"><span id="varLine">${varsLine(draft)}</span>
        <span style="margin-left:auto" class="faint">autosaves as you type</span></div>
    </div>
    <div class="card testpanel">
      <div style="padding:12px 16px;border-bottom:1px solid var(--line2);display:flex;align-items:center;gap:8px">
        <span style="font-size:12px;font-weight:700">Test render</span>
        <span style="font-size:11.5px;color:var(--faint)">live · fragments expanded</span></div>
      <div style="display:flex;gap:6px;padding:12px 16px 4px;flex-wrap:wrap">${chips}</div>
      ${custom}
      <div class="render-out" id="renderOut">renders live as you type</div>
    </div></div>`;
}

// The Review tab answers "what changes for users, and under which situations?" — a
// rendered before/after is the centerpiece, the source diff a secondary disclosure,
// with verdict buttons, review-state pills and a comment thread. The rendered pane +
// comments load async after paint (loadReviewRendered / loadReviewComments).
function draftReviewTab(dp) {
  const draft = dp.draft, selfOk = draft.allow_self_review;
  const isAuthor = draft.author === (State.me && State.me.name);
  const blocked = isAuthor && !selfOk;
  // The change summary: the draft's title, else a base→draft context line.
  const listEntry = (dp.drafts || []).find((d) => d.id === draft.id);
  const title = String(draft.title || (listEntry && listEntry.title) || "").trim();
  const summary = title || `Edits to Version ${draft.version_number}`;
  const policyText = draft.review_policy > 0
    ? `${draft.review_policy} approval(s) to commit · ${selfOk
        ? "self-review counts — the author can approve their own draft"
        : "distinct reviewer required — the author's own approval doesn't count"}`
    : "no approvals required to commit";

  // Verdict pills: prefer the new reviews[] (approved / changes_requested), fall back
  // to the legacy reviewers[] (approval names only).
  const reviews = draft.reviews || [];
  let verdicts;
  if (reviews.length)
    verdicts = reviews.map((r) => r.state === "approved"
      ? `<span class="pill live">✓ ${esc(r.reviewer)}</span>`
      : `<span class="pill warn">⨯ ${esc(r.reviewer)} requested changes</span>`).join(" ");
  else if ((draft.reviewers || []).length)
    verdicts = draft.reviewers.map((r) => `<span class="pill live">✓ ${esc(r)}</span>`).join(" ");
  else verdicts = '<span class="faint">No verdicts yet.</span>';

  const verdictBtns = blocked
    ? `<button class="btn" disabled>Approve ✓</button>
       <button class="btn" disabled>Request changes</button>`
    : `<button class="btn olive" data-act="approve" data-draft="${esc(draft.id)}">Approve ✓</button>
       <button class="btn danger" data-act="requestChanges" data-draft="${esc(draft.id)}">Request changes</button>`;

  const notice = _draftNotice
    ? `<div class="banner warn"><span style="font-size:12.5px;font-weight:600">${esc(_draftNotice)}</span></div>` : "";
  _draftNotice = null;   // one-shot

  const tcChips = dp.tcs.length
    ? dp.tcs.map((t) => `<button type="button" class="chip btn-bare ${t.name === dp.reviewTc ? "active" : ""}" aria-pressed="${t.name === dp.reviewTc}" data-act="reviewTc" data-name="${esc(t.name)}">${esc(t.name)}</button>`).join("")
    : '<span class="faint" style="font-size:11.5px">No saved test contexts — add one on the Write tab to preview a rendered before/after.</span>';

  return `${notice}
    <div class="panelrow">
      <div style="flex:10 1 460px;min-width:0"><div class="card">
        <div style="padding:15px 18px;border-bottom:1px solid var(--line2)">
          <div style="display:flex;gap:12px;align-items:flex-start;flex-wrap:wrap">
            <div style="flex:1;min-width:0">
              <div style="font-size:14px;font-weight:700">${esc(summary)}</div>
              <div style="font-size:11.5px;color:var(--mut);margin-top:3px">by <b>${esc(draft.author)}</b> · v${draft.version_number} · base <span class="mono">${esc(draft.base_sha || "—")}</span></div></div>
            <div style="display:flex;gap:8px;flex-wrap:wrap">${verdictBtns}</div></div>
          <div style="font-size:11.5px;color:var(--mut);margin-top:10px">${esc(policyText)}</div>
          ${blocked ? `<div style="font-size:11.5px;color:var(--warn);font-weight:600;margin-top:4px">You authored this draft — a distinct reviewer must weigh in.</div>` : ""}
          <div style="display:flex;gap:6px;flex-wrap:wrap;align-items:center;margin-top:10px">${verdicts}</div></div>
        <div id="reviewBanner"></div>
        <div style="padding:14px 18px 4px">
          <div class="groupname">What changes for people</div>
          <div id="reviewTcChips" style="display:flex;gap:6px;margin-bottom:10px;flex-wrap:wrap">${tcChips}</div></div>
        <div id="reviewRendered" class="sxs-frame"><div class="empty">Loading rendered preview…</div></div>
        <div style="padding:0 18px 12px;font-size:11.5px;color:var(--mut)">${varsLine(draft) || "no declared variables"}</div>
        <div id="reviewSrcSection" style="padding:0 18px 16px">${reviewSrcSectionInner(dp)}</div>
        <div style="border-top:1px solid var(--line2);padding:14px 18px">
          <div class="groupname">Discussion</div>
          <div id="reviewCommentsThread"><div class="empty">Loading comments…</div></div>
          <div style="margin-top:10px">
            <textarea id="reviewCommentBody" class="cmt-input" spellcheck="false"
              placeholder="Leave a note for the author or other reviewers…"
              oninput="var b=document.getElementById('reviewCommentBtn');b.disabled=!this.value.trim()"></textarea>
            <div style="display:flex;justify-content:flex-end;margin-top:8px">
              <button id="reviewCommentBtn" class="btn primary" disabled data-act="postComment">Comment</button></div></div></div>
      </div></div>
      <div style="flex:1 1 240px;min-width:0"><div class="card pad">
        <div class="groupname">Review policy</div>
        <div style="display:flex;gap:8px;align-items:center;flex-wrap:wrap;margin-bottom:8px">
          <span class="pill ${selfOk ? "live" : "warn"}">${selfOk ? "self-review on" : "four-eyes"}</span>
          <span style="font-size:11.5px;color:var(--mut)">${selfOk ? "the author can approve their own draft" : "a distinct reviewer is required"}</span></div>
        <div style="font-size:11.5px;color:var(--faint)">Change this in <b>Project settings</b> on the prompt's overview.</div>
        <div style="font-size:11.5px;color:var(--faint);border-top:1px solid var(--line2);padding-top:10px;margin-top:8px">Review gates what enters the repo — targeting gates who sees it.</div>
      </div></div>
    </div>`;
}

// A pane head for the review/publish side-by-side rendered panes.
function sxsPaneHead(label) {
  return `<div style="padding:7px 14px;font-size:11px;font-weight:600;color:var(--mut);border-bottom:1px solid var(--line2);background:var(--panel2)">${esc(label)}</div>`;
}
// The rendered before/after body for the review tab (draft diff, mode=rendered).
function reviewRenderedBody(res) {
  if (res.error)
    return `<div class="empty">⚠ ${esc(res.error)}<div style="margin-top:8px;font-size:11.5px;color:var(--mut)">Try another test context, or add one with the variables this draft needs on the Write tab.</div></div>`;
  const left = res.left || "", right = res.right || "";
  if (left === right)
    return `<div style="padding:7px 14px;font-size:11px;color:var(--faint);border-bottom:1px solid var(--line2)">no rendered change for this context</div>
      <div class="diffbox" style="padding:12px 18px;white-space:pre-wrap">${esc(right)}</div>`;
  return `<div class="sxs-heads">${sxsPaneHead("What people get now")}${sxsPaneHead("What they'd get after this change")}</div>
    <div class="sxs">${renderSideBySide(left, right)}</div>`;
}
async function loadReviewRendered() {
  const box = el("reviewRendered"); if (!box || !window._dp) return;
  const dp = window._dp, tc = dp.reviewTc;
  box.innerHTML = '<div class="empty">Loading rendered preview…</div>';
  const banner = el("reviewBanner"); if (banner) banner.innerHTML = "";
  let q = `mode=rendered&environment=${enc(State.env)}`;
  if (tc) q += `&test_context=${enc(tc)}`;
  try {
    const res = await GET(`/mgmt/drafts/${enc(dp.draft.id)}/diff?${q}`);
    if (el("reviewRendered")) el("reviewRendered").innerHTML = reviewRenderedBody(res);
    // Cheap failure probe: only the active context. If it errors, warn atop the tab.
    if (el("reviewBanner"))
      el("reviewBanner").innerHTML = res.error
        ? `<div class="banner warn" style="margin:12px 18px 0"><span style="font-size:12.5px;font-weight:600">This draft doesn't render for ${tc ? `context “${esc(tc)}”` : "this context"} — ${esc(res.error)}</span></div>`
        : "";
  } catch (e) {
    if (el("reviewRendered")) el("reviewRendered").innerHTML = `<div class="empty">⚠ ${esc(errText(e))}</div>`;
  }
}
// Secondary "exact text changes" disclosure — source unified diff, loaded on expand.
function reviewSrcSectionInner(dp) {
  const open = dp.reviewSrcOpen;
  return `<button type="button" class="techtoggle btn-bare" data-act="reviewSrcToggle" aria-expanded="${open}">${open ? "Hide the exact text changes ▴" : "Show the exact text changes ▾"}</button>
    ${open ? `<div class="card" style="margin-top:8px"><div id="reviewSrcBox"><div class="empty">Loading diff…</div></div></div>` : ""}`;
}
async function loadReviewSrc() {
  const box = el("reviewSrcBox"); if (!box || !window._dp) return;
  try {
    const res = await GET(`/mgmt/drafts/${enc(window._dp.draft.id)}/diff?mode=source&environment=${enc(State.env)}`);
    const inner = res.error ? `<div class="empty">⚠ ${esc(res.error)}</div>`
      : (renderUnifiedDiff(res.diff) ? `<div class="diffbox">${renderUnifiedDiff(res.diff)}</div>` : '<div class="empty">No source changes vs base.</div>');
    if (el("reviewSrcBox")) el("reviewSrcBox").innerHTML = inner;
  } catch (e) {
    if (el("reviewSrcBox")) el("reviewSrcBox").innerHTML = `<div class="empty">⚠ ${esc(errText(e))}</div>`;
  }
}
// Comment thread (oldest first). Body is user content — esc() is load-bearing here.
function commentsThreadHtml(comments) {
  if (!comments.length) return '<div class="faint" style="font-size:12px">No comments yet — start the discussion.</div>';
  return comments.map((c) => `<div class="cmt">
    <div class="cmt-head"><b>${esc(c.author)}</b> <span class="faint">${esc(ago(c.created_at))}</span></div>
    <div class="cmt-body">${esc(c.body)}</div></div>`).join("");
}
async function loadReviewComments() {
  const box = el("reviewCommentsThread"); if (!box || !window._dp) return;
  try {
    const r = await GET(`/mgmt/drafts/${enc(window._dp.draft.id)}/comments`);
    if (el("reviewCommentsThread")) el("reviewCommentsThread").innerHTML = commentsThreadHtml(r.comments || []);
  } catch (e) {
    if (el("reviewCommentsThread")) el("reviewCommentsThread").innerHTML = `<div class="empty">⚠ ${esc(errText(e))}</div>`;
  }
}

async function doRenderDraft() {
  const out = el("renderOut"); if (!out || !window._dp) return;
  const dp = window._dp, tc = dp.tcActive;
  if (!tc) { out.textContent = "Pick a test context — renders live as you type."; return; }
  const body = { environment: State.env };
  if (tc === "__custom") {
    try { body.variables = (dp.customVars || "").trim() ? JSON.parse(dp.customVars) : {}; }
    catch { out.textContent = "⚠ Variables: invalid JSON"; return; }
    try { body.flags = (dp.customFlags || "").trim() ? JSON.parse(dp.customFlags) : {}; }
    catch { out.textContent = "⚠ Flags: invalid JSON"; return; }
  } else {
    body.test_context = tc;
  }
  out.textContent = "rendering…";
  try {
    const r = await POST(`/mgmt/drafts/${dp.draft.id}/render`, body);
    if (el("renderOut")) el("renderOut").textContent = r.rendered;
  } catch (e) {
    if (el("renderOut")) el("renderOut").textContent = "⚠ " + errText(e);
  }
}

// ── draft diff tab ───────────────────────────────────────────────────
// "What did I change" — the draft vs its base by default, or vs any live/tip SHA.
function draftDiffTabShell(dp) {
  const draft = dp.draft;
  // Reuse Compare's revision enumeration: each version at its live and/or tip SHA.
  const revs = [];
  for (const v of dp.versions) {
    if (v.tip_full_sha && v.tip_full_sha !== v.live_full_sha)
      revs.push({ v: v.version, sha: v.tip_full_sha, label: `v${v.version} · latest edits · ${v.tip_sha}` });
    if (v.live_full_sha)
      revs.push({ v: v.version, sha: v.live_full_sha, label: `v${v.version} · what ${State.env} serves · ${v.live_sha}` });
  }
  const sel = dp.diffAgainst || "base";
  const opts = `<option value="base"${sel === "base" ? " selected" : ""}>where you started · v${draft.version_number} · ${esc(draft.base_sha || "—")}</option>` +
    revs.map((r) => { const tok = r.v + ":" + r.sha;
      return `<option value="${esc(tok)}"${sel === tok ? " selected" : ""}>${esc(r.label)}</option>`; }).join("");
  const mode = dp.diffMode || "source";
  const tcRow = mode === "rendered"
    ? `<div style="display:flex;gap:6px;margin-bottom:10px;flex-wrap:wrap">${
        dp.tcs.map((t) => `<button type="button" class="chip btn-bare ${t.name === dp.diffTc ? "active" : ""}" aria-pressed="${t.name === dp.diffTc}" data-act="diffTc" data-name="${esc(t.name)}">${esc(t.name)}</button>`).join("")
        || '<span class="faint">No test contexts</span>'}</div>` : "";
  return `<div style="display:flex;gap:9px;align-items:center;margin-bottom:12px;flex-wrap:wrap">
      <span class="faint" style="font-size:12px">against</span>
      <select class="envsel" aria-label="Compare draft against" data-act="diffAgainst" style="min-width:240px">${opts}</select></div>
    <div class="tabs" role="tablist" aria-label="Diff mode">
      <button type="button" role="tab" aria-selected="${mode === "source"}" class="tab btn-bare ${mode === "source" ? "active" : ""}" data-act="diffMode" data-mode="source">Source</button>
      <button type="button" role="tab" aria-selected="${mode === "rendered"}" class="tab btn-bare ${mode === "rendered" ? "active" : ""}" data-act="diffMode" data-mode="rendered">Rendered</button></div>
    ${tcRow}
    <div class="card"><div id="draftDiffBox"><div class="empty">Loading diff…</div></div></div>`;
}
function draftDiffBody(res) {
  if (res.error) return `<div class="empty">⚠ ${esc(res.error)}</div>`;
  if (res.mode === "rendered") {
    if ((res.left || "") === (res.right || "")) return '<div class="empty">No differences — identical rendered output.</div>';
    return `<div class="sxs">${renderSideBySide(res.left, res.right)}</div>`;
  }
  const html = renderUnifiedDiff(res.diff);
  return html ? `<div class="diffbox">${html}</div>`
              : '<div class="empty">No differences — nothing changed vs this revision.</div>';
}
function fetchDraftDiff() {
  const dp = window._dp;
  let q = `mode=${dp.diffMode}&environment=${enc(State.env)}`;
  if (dp.diffAgainst && dp.diffAgainst !== "base") {   // omitting against_* defaults to base
    const [ver, sha] = dp.diffAgainst.split(":");
    q += `&against_version=${enc(ver)}&against_sha=${enc(sha)}`;
  }
  if (dp.diffMode === "rendered" && dp.diffTc) q += `&test_context=${enc(dp.diffTc)}`;
  return GET(`/mgmt/drafts/${enc(dp.draft.id)}/diff?${q}`);
}
async function loadDraftDiff() {
  const box = el("draftDiffBox"); if (!box) return;
  box.innerHTML = '<div class="empty">Loading diff…</div>';
  try {
    const res = await fetchDraftDiff();
    if (el("draftDiffBox")) el("draftDiffBox").innerHTML = draftDiffBody(res);
  } catch (e) {
    if (el("draftDiffBox")) el("draftDiffBox").innerHTML = `<div class="empty">⚠ ${esc(errText(e))}</div>`;
  }
}
function renderDraftDiffTab() {   // rebuild the diff-tab controls in place (mode toggle) + reload
  const host = el("draftTabBody"); if (!host) return;
  host.innerHTML = draftDiffTabShell(window._dp);
  loadDraftDiff();
}

// ── draft page: commit + conflict ────────────────────────────────────
function commitModalHtml(draft) {
  return `
    <h3>Save edits to v${draft.version_number}</h3>
    <p class="hint">Your edits land in Version ${draft.version_number}'s history — validated and recorded. Nothing changes for anyone until you publish.</p>
    <div class="field"><label>What changed?</label>
      <input id="commitMsg" placeholder="a short note on what you changed" spellcheck="false"></div>
    <div class="groupname" style="margin:2px 0 6px">What you changed vs where you started</div>
    <div class="card"><div class="diffbox modal-diff" id="commitDiffBox"><div class="empty">Loading diff…</div></div></div>
    <div class="modal-actions">
      <button class="btn" data-act="closeModal">Cancel</button>
      <button class="btn primary" data-act="commitDraft" data-id="${esc(draft.id)}">Save edits</button></div>`;
}
// 409 → the version moved since this draft's base. Show what landed in between and
// let the author force their edit on top. Replaces the old always-on force checkbox.
function openConflictModal(draftId, c) {
  const diffHtml = c.diff ? renderUnifiedDiff(c.diff) : "";
  openModal(`
    <h3>v${window._dp.draft.version_number} changed while you were editing</h3>
    <p class="hint">Someone else's edits landed after you started. Review what changed in between, then save anyway to put your version on top.</p>
    <div class="groupname" style="margin:2px 0 6px">What changed in between <span class="mono faint" style="font-size:11.5px;font-weight:400">${esc(c.base_sha || "")} → ${esc(c.current_sha || "")}</span></div>
    <div class="card"><div class="diffbox modal-diff">${diffHtml || '<div class="empty">No intervening changes to show.</div>'}</div></div>
    <div class="modal-actions">
      <button class="btn" data-act="closeModal">Cancel</button>
      <button class="btn danger" data-act="commitForce" data-id="${esc(draftId)}">Save anyway</button></div>`, "wide");
}
// 412 → review required. Don't strand the user with a toast: land them on the review tab.
function goReviewNotice() {
  _draftNotice = "Review required before saving — approve below (or adjust the policy).";
  closeModal();
  go(`#/p/${enc(State.route.pid)}/draft?draft=${enc(window._dp.draft.id)}&tab=review`);
}

// Compare — the history tool: any two committed states, A → B (renamed from the old
// Diff screen). Human labels; rendered mode stays unified (this endpoint has no left/right).
async function screenCompare() {
  const pid = State.route.pid;
  const d = await GET(`/mgmt/prompts/${enc(pid)}/versions?environment=${enc(State.env)}`);
  const mode = State.route.q.mode || "source";

  // Selectable revisions: each version at its live and/or tip SHA (newest first,
  // tip before live within a version).
  const revs = [];
  for (const v of d.versions) {
    // Tip is listed whenever it differs from live — tip_ahead is 0 for a version
    // that was committed but never made live, and those must still be comparable.
    if (v.tip_full_sha && v.tip_full_sha !== v.live_full_sha)
      revs.push({ token: `${v.version}@tip`, version: v.version, sha: v.tip_full_sha,
                  short: v.tip_sha, label: `v${v.version} · latest edits (unpublished) · ${v.tip_sha}` });
    if (v.live_full_sha)
      revs.push({ token: `${v.version}@live`, version: v.version, sha: v.live_full_sha,
                  short: v.live_sha, label: `v${v.version} · what ${State.env} serves now · ${v.live_sha}` });
  }
  if (revs.length < 2) {
    el("main").innerHTML = `<div class="screen"><div class="h1row"><span class="h1 sm serif">Compare</span></div><div class="empty">Nothing to compare yet — need two committed states.</div></div>`;
    return;
  }

  // Default: the two newest versions at their live SHA (base = older, target = newer).
  // Single version with a tip ahead falls back to live→tip.
  const liveByVer = [];
  const seenVer = new Set();
  for (const r of revs) if (r.token.endsWith("@live") && !seenVer.has(r.version)) {
    seenVer.add(r.version); liveByVer.push(r);
  }
  const [defA, defB] = liveByVer.length >= 2
    ? [liveByVer[1].token, liveByVer[0].token]
    : [revs[revs.length - 1].token, revs[0].token];
  const has = (tok) => revs.some((r) => r.token === tok);
  const aTok = has(State.route.q.a) ? State.route.q.a : defA;
  const bTok = has(State.route.q.b) ? State.route.q.b : defB;
  const a = revs.find((r) => r.token === aTok);
  const b = revs.find((r) => r.token === bTok);
  const opts = (sel) => revs.map((r) =>
    `<option value="${esc(r.token)}"${r.token === sel ? " selected" : ""}>${esc(r.label)}</option>`).join("");

  const query = `a_version=${a.version}&a_sha=${a.sha}&b_version=${b.version}&b_sha=${b.sha}&mode=${mode}&environment=${enc(State.env)}`;
  const res = await GET(`/mgmt/prompts/${enc(pid)}/diff?${query}`);
  // Two-pane view: the full text of both revisions is always on screen, changed
  // lines highlighted. Identical revisions show the text once — never a dead end.
  const identical = !res.error && (res.left || "") === (res.right || "");
  const paneHead = (label, note) => `<div style="padding:8px 14px;font-size:11px;font-weight:600;color:var(--mut);
    border-bottom:1px solid var(--line2);background:var(--panel2)">${esc(label)}${note ? ` <span class="faint" style="font-weight:400">· ${esc(note)}</span>` : ""}</div>`;
  let body;
  if (res.error) {
    body = `<div class="empty">⚠ ${esc(res.error)}</div>`;
  } else if (identical) {
    body = paneHead(b.label, aTok === bTok ? "" : "same text on both sides") +
      `<div class="diffbox" style="padding:12px 18px;white-space:pre-wrap">${esc(res.right || "")}</div>`;
  } else {
    body = `<div class="sxs-heads">${paneHead(a.label)}${paneHead(b.label)}</div>
      <div class="sxs">${renderSideBySide(res.left, res.right)}</div>`;
  }

  const qs = `a=${enc(aTok)}&b=${enc(bTok)}`;
  el("main").innerHTML = `<div class="screen">
    <div class="h1row"><span class="h1 sm serif">Compare — <i>${esc(pid)}</i></span>
      ${res.context ? `<span class="pill acc">${esc(res.context)}</span>` : ""}
      ${identical ? '<span class="pill neutral">these two are identical — showing the text</span>' : ""}</div>
    <div style="display:flex;gap:9px;align-items:center;margin-bottom:12px;flex-wrap:wrap">
      <select class="envsel" aria-label="Compare — left revision" data-act="diffPick" data-side="a" style="min-width:230px">${opts(aTok)}</select>
      <span class="faint" style="font-size:13px">→</span>
      <select class="envsel" aria-label="Compare — right revision" data-act="diffPick" data-side="b" style="min-width:230px">${opts(bTok)}</select>
      ${State.tech ? `<span class="mono muted" style="font-size:11px;margin-left:4px">${esc(a.short)} → ${esc(b.short)}</span>` : ""}</div>
    <div class="tabs" role="tablist" aria-label="Diff mode">
      <a role="tab" aria-selected="${mode === "source"}" class="tab ${mode === "source" ? "active" : ""}" href="#/p/${enc(pid)}/compare?mode=source&${qs}" data-act="go" data-hash="#/p/${enc(pid)}/compare?mode=source&${qs}">Source</a>
      <a role="tab" aria-selected="${mode === "rendered"}" class="tab ${mode === "rendered" ? "active" : ""}" href="#/p/${enc(pid)}/compare?mode=rendered&${qs}" data-act="go" data-hash="#/p/${enc(pid)}/compare?mode=rendered&${qs}">Rendered</a></div>
    <div class="card"><div style="display:flex;gap:14px;padding:9px 18px;border-bottom:1px solid var(--line2);font-size:11px;color:var(--mut)">
      <span class="mono">${esc(pid)}</span><span style="margin-left:auto">${mode === "rendered" ? "rendered · fragments expanded" : "source, side by side"}</span></div>
      ${body}</div></div>`;
}

async function screenRules() {
  const env = State.env;
  const [d, rv] = await Promise.all([
    GET(`/mgmt/envs/${enc(env)}/rules`),
    GET(`/mgmt/envs/${enc(env)}/revisions?limit=25`),
  ]);
  _rulesData = d;   // stashed for the "turn targeting off" confirm modal
  const pid = State.route.pid;
  // Kill semantics are per-prompt; the header toggle governs the route prompt or,
  // on the env-wide screen, the first prompt-scoped rule's prompt.
  const defaultPid = pid || (d.rules.find((r) => r.prompt_id)?.prompt_id) || null;
  const defV = defaultPid ? d.defaults[defaultPid] : null;
  const killEngaged = !!(defaultPid && d.kills[defaultPid]);

  // ── header: "Up to date" status + the Targeting toggle (kill reframed) ──
  const rightControls = `${statusLine("live", "Up to date")}${defaultPid
    ? `<span class="faint" style="margin:0 4px">·</span>
       <span style="display:inline-flex;align-items:center;gap:8px">
         <span class="muted" style="font-size:12px;font-weight:600">Targeting</span>
         <button type="button" role="switch" aria-checked="${!killEngaged}" aria-label="Targeting for ${esc(defaultPid)}" class="toggle ${killEngaged ? "" : "on"}" data-act="targetingToggle" data-pid="${esc(defaultPid)}" data-engaged="${killEngaged}"><span class="knob"></span></button>
       </span>` : ""}`;

  const killBanner = killEngaged ? `<div class="banner danger">
      <span style="font-size:15px">⏻</span>
      <span style="font-size:12.5px;font-weight:700">Targeting is off for ${esc(defaultPid)} — everyone gets the default. Rules below are being ignored.</span>
      <button type="button" class="link btn-bare" style="margin-left:auto;color:var(--danger)" data-act="kill" data-pid="${esc(defaultPid)}" data-engage="false">Turn targeting back on</button></div>` : "";

  // ── ordinal rule rows (ordered by priority — the evaluation order) ──
  const sorted = d.rules.slice().sort((a, b) => a.priority - b.priority);
  const ordRows = sorted.map((r, i) => {
    const t = serveTarget(r.serve);
    const isTip = !!(t && t.tip);
    const statusCol = r.status === "active"
      ? `<span style="font-size:11.5px;color:var(--live);font-weight:600">● On</span>`
      : `<span class="faint" style="font-size:11px">${esc(r.status)}</span>`;
    const statusToggle = r.status === "active"
      ? `<button type="button" class="link mut btn-bare" data-act="ruleStatus" data-id="${esc(r.id)}" data-status="archived">Archive</button>`
      : `<button type="button" class="link btn-bare" data-act="ruleStatus" data-id="${esc(r.id)}" data-status="active">Activate</button>`;
    const scopeChip = r.scope === "global"
      ? pill("acc", "all prompts")
      : (r.prompt_id ? `<span class="mono faint" style="font-size:11.5px">${esc(r.prompt_id)}</span>` : "");
    const up = i > 0 ? `<button type="button" class="ord-move btn-bare" data-act="ruleMove" data-id="${esc(r.id)}" data-dir="up" title="move earlier" aria-label="Move rule earlier">↑</button>` : "";
    const down = i < sorted.length - 1 ? `<button type="button" class="ord-move btn-bare" data-act="ruleMove" data-id="${esc(r.id)}" data-dir="down" title="move later" aria-label="Move rule later">↓</button>` : "";
    const stopTest = (isTip && r.prompt_id && r.status === "active")
      ? `<button type="button" class="link btn-bare" data-act="stopTestPublish" data-id="${esc(r.id)}">Stop test &amp; publish</button>` : "";
    return `<div class="ord" data-ruleid="${esc(r.id)}"${killEngaged ? ' style="opacity:.45"' : ""}>
      <div class="ord-head">
        <span class="ord-n">${ordinal(i + 1)}</span>
        <span class="ord-title">${esc(r.comment || r.id)}</span>
        ${scopeChip}${isTip ? pill("warn", "testing") : ""}
        <div class="grow"></div>${statusCol}</div>
      <div class="ord-body">${ruleServeLine(r)}</div>
      <div class="ord-actions">${up}${down}
        <button type="button" class="link btn-bare" data-act="ruleEdit" data-id="${esc(r.id)}">Edit</button>
        <button type="button" class="link btn-bare" data-act="ruleDup" data-id="${esc(r.id)}">Duplicate</button>
        ${stopTest}${statusToggle}</div></div>`;
  }).join("") || '<div class="empty">No rules yet — everyone gets the default below.</div>';

  const fallbackRow = `<div style="display:flex;align-items:center;gap:12px;padding:15px 20px;background:var(--panel2);flex-wrap:wrap">
      <span class="muted" style="font-size:12px">Everyone else →</span>
      <span style="font-size:13px;font-weight:700">${defV != null
        ? `Version ${defV} <span style="font-weight:500;color:var(--live)">(live)</span>`
        : "the environment default"}</span>
      <div class="grow"></div>
      <span class="faint" style="font-size:11.5px">The fallback when no rule matches.</span></div>`;

  // ── change history ──
  const revRows = rv.revisions.map((r, i) => `
    <tr class="grow-row">
      <td class="mono muted">rv${r.rules_version}</td>
      <td><span class="tag ${r.kind === "rollback" ? "acc" : "mut"}">${esc(r.kind)}</span>
        ${r.rule_id ? `<span class="mono" style="font-size:11px">${esc(r.rule_id)}</span>` : ""}</td>
      <td><b>${esc(r.actor || "—")}</b></td>
      <td class="muted" style="font-size:11px">${esc(r.comment || "")}</td>
      <td class="mono muted" style="font-size:11px">${new Date(r.at).toLocaleString()}</td>
      <td style="text-align:right;white-space:nowrap">${i === 0
        ? '<span class="faint" style="font-size:11.5px">current</span>'
        : `<button class="btn" data-act="rollback" data-rv="${r.rules_version}">▸ Go back to here</button>`}</td>
    </tr>`).join("") || '<tr><td colspan="6" class="empty">No targeting changes yet.</td></tr>';

  // Audience tester renders through the real serving path for one prompt; reset its
  // cached result when the prompt in focus changes.
  const testerPid = pid || defaultPid;
  if (window._audience && window._audience.pid !== testerPid) window._audience = null;

  el("main").innerHTML = `<div class="screen">
    <div style="display:flex;align-items:center;gap:12px;margin-bottom:16px;flex-wrap:wrap">
      <span class="page-h1">Who sees what</span>
      ${d.protected ? pill("warn", `${esc(env)} · protected`) : ""}
      <div class="grow"></div>
      <button class="btn primary sm" data-act="ruleNew">＋ New rule</button>
      <span class="faint" style="font-size:11px">·</span>${rightControls}</div>
    ${killBanner}
    <div class="card">
      <div style="padding:11px 20px;font-size:12px;color:var(--mut);background:var(--panel2);border-bottom:1px solid var(--line2)">Rules are checked top to bottom. The first one that matches a request decides which version that person sees.</div>
      ${ordRows}${fallbackRow}</div>
    <div id="audienceCard">${audienceCardHtml(testerPid)}</div>
    ${techDetails(`rules_version ${esc(String(d.rules_version))} · synced &lt;2s`, "rules version, sync")}
    <div class="h1row" style="margin-top:26px"><span class="h1 sm serif">Change history</span>
      <span class="sub">every targeting change — who, what, when</span></div>
    <div class="card" style="overflow-x:auto"><table class="grid">
      <thead class="ghead"><tr><th>Version</th><th>Change</th><th>Who</th><th>Comment</th><th>When</th><th></th></tr></thead>
      <tbody>${revRows}</tbody></table></div>
    <div style="font-size:11px;color:var(--faint);margin-top:12px">Going back restores the rules to an earlier point — rules created afterward stop serving. It's itself a change, so history is never rewritten.</div></div>`;
}

// The reframed kill switch. Turning targeting OFF is destructive (falls everyone
// through to the default, ignores every rule and every deliberate pin) so it confirms;
// turning it back ON is instant and lives in Actions.targetingToggle.
function openTargetingOffModal(pid) {
  const d = _rulesData; if (!d) return;
  const defV = d.defaults[pid];
  const defTxt = defV != null ? `Version ${defV} (live)` : "the environment default";
  const ignored = d.rules.filter((r) => r.status === "active").map((r) =>
    `<div style="font-size:12.5px;color:var(--mut)">· ${esc(r.comment || r.id)} <span class="faint">→ ${serveTargetPlain(r.serve)}</span></div>`
  ).join("") || '<div class="faint" style="font-size:12.5px">No active rules.</div>';
  openModal(`
    <div style="display:flex;align-items:center;gap:10px">
      <span class="toggle"><span class="knob"></span></span>
      <h3 style="margin:0">Turn targeting off?</h3></div>
    <p class="hint" style="margin-top:10px">Every request will fall through to the default — <b>${defTxt}</b> — and all rules below will be ignored.</p>
    <div style="display:flex;gap:10px;margin:0 0 14px;padding:12px 14px;background:var(--warn-soft);border-radius:10px">
      <span style="font-size:14px;line-height:1.3">⚠</span>
      <div style="font-size:12.5px;color:var(--warn);line-height:1.6">This also overrides versions pinned on purpose. Anyone currently kept on an older version for business reasons will switch to the default too. For those users this may be worse, not safer.</div></div>
    <div class="groupname">RULES THAT WILL BE IGNORED</div>
    <div style="display:flex;flex-direction:column;gap:6px">${ignored}</div>
    <div class="modal-actions" style="align-items:center">
      <span class="faint" style="font-size:11.5px;margin-right:auto">Reversible — toggle back on anytime.</span>
      <button class="btn" data-act="closeModal">Cancel</button>
      <button class="btn danger" data-act="kill" data-pid="${esc(pid)}" data-engage="true">Turn off</button></div>`);
}

async function screenPointers() {
  const pid = State.route.pid;
  const dv = await GET(`/mgmt/prompts/${enc(pid)}/versions?environment=${enc(State.env)}`);
  const version = State.route.q.v ? parseInt(State.route.q.v)
    : (dv.versions.find((v) => v.tip_ahead > 0)?.version || dv.versions.find((v) => v.is_default)?.version || dv.versions[0]?.version);
  const vrow = dv.versions.find((v) => v.version === version) || {};
  const tl = await GET(`/mgmt/envs/${enc(State.env)}/pointers?prompt_id=${enc(pid)}&version=${version}`);
  const moves = tl.moves.map((m, i) => {
    const last = i === tl.moves.length - 1;
    return `<div class="tl-row" style="${m.current ? "background:var(--acc-soft2)" : ""}">
      <div class="tl-col"><span class="tl-dot ${m.current ? "cur" : ""}"></span>${last ? "" : '<span class="tl-line"></span>'}</div>
      <div style="flex:1;padding-bottom:16px;min-width:0">
        <div style="display:flex;gap:10px;align-items:baseline;flex-wrap:wrap">
          <span style="font-size:12.5px;font-weight:600">Published by ${esc(m.by)}</span>
          <span class="muted" style="font-size:11.5px">${new Date(m.at).toLocaleString()}</span>
          ${m.current ? '<span class="pill live" style="font-size:11.5px">● LIVE NOW</span>' : ""}
          <span class="mono faint" style="font-size:11.5px">${esc(m.sha)}</span></div>
        ${m.comment ? `<div style="font-size:12px;color:var(--mut);margin-top:3px;font-style:italic">${esc(m.comment)}</div>` : ""}</div>
      ${!m.current ? `<button class="btn" data-act="revert" data-sha="${m.full_sha}" data-short="${esc(m.sha)}" data-v="${version}">Go back to this</button>` : ""}</div>`;
  }).join("") || '<div class="empty">No publish history yet.</div>';

  const shaChain = tl.moves.map((m) =>
    `${esc(m.sha)}${m.from_sha ? " ← from " + esc(m.from_sha) : " (first publish)"}${m.current ? " · live now" : ""}`).join("<br>");

  const locked = !!State.envs.find((e) => e.id === State.env)?.protected;
  const advance = (vrow.tip_ahead > 0 && vrow.tip_full_sha)
    ? `<button class="tweak-btn" style="width:auto;display:inline-flex" data-act="makeLive" data-sha="${vrow.tip_full_sha}" data-short="${esc(vrow.tip_sha)}" data-v="${version}">✦ Publish latest edits (${vrow.tip_ahead} waiting)</button>
       <span class="faint" style="font-size:11px">preview the impact before it goes live${locked ? ` — ${esc(State.env)} is locked, type the prompt id to confirm` : ""}</span>`
    : `<span style="font-size:12px;color:var(--live);font-weight:600">✓ The latest edits are already live — nothing to publish.</span>`;

  el("main").innerHTML = `<div class="screen">
    <div class="h1row"><span class="h1 sm serif">Publish history — <i>v${version} · ${esc(State.env)}</i></span></div>
    <div style="font-size:12px;color:var(--mut);margin-bottom:18px">Every publish, newest first. The top entry is what people see now. Any earlier state is one click from being live again.</div>
    <div class="card">${moves}</div>
    ${tl.moves.length ? techDetails(shaChain, "commit SHAs") : ""}
    <div style="display:flex;gap:10px;margin-top:14px;align-items:center;flex-wrap:wrap">${advance}</div></div>`;
}

// ── publish impact modal (shared by "Publish latest edits" + "Go back to this") ──
// A wide preview: what changes (rendered before/after), who's affected (+ redundant
// test rules to remove), the edits going live, and an inline type-to-confirm on locked
// envs. On confirm: POST the pointer move, then archive each checked test rule.
async function openPublishModal({ v, toSha, toShort, mode }) {
  const pid = State.route.pid, env = State.env;
  mode = mode || "publish";
  openModal(`<div id="publishModalBody"><div class="empty">Loading impact…</div></div>`, "wide");
  let dv, rd;
  try {
    [dv, rd] = await Promise.all([
      GET(`/mgmt/prompts/${enc(pid)}/versions?environment=${enc(env)}`),
      GET(`/mgmt/envs/${enc(env)}/rules`).catch(() => ({ rules: [] })),
    ]);
  } catch (e) { const b = el("publishModalBody"); if (b) b.innerHTML = `<div class="empty">⚠ ${esc(errText(e))}</div>`; return; }
  let tcs = [];
  try { const t = await GET(`/mgmt/prompts/${enc(pid)}/test-contexts`); tcs = t.test_contexts || []; } catch (_) {}
  const vrow = dv.versions.find((x) => x.version === v) || {};
  // Prompt-scoped rules serving this prompt @tip become redundant once tip is live —
  // mirror stopTestPublish's safety (never auto-archive a global rule).
  const tipRules = activeRulesFor(rd.rules, pid).filter((r) => r.prompt_id && (serveTarget(r.serve) || {}).tip);
  window._publish = {
    pid, env, mode, v, toSha,
    liveFull: vrow.live_full_sha || null, liveShort: vrow.live_sha || null,
    toShort: toShort || vrow.tip_sha || null,
    tipAhead: vrow.tip_ahead || 0, history: vrow.history || [],
    tcs, tc: tcs[0]?.name || null,
    diffMode: tcs.length ? "rendered" : "source",
    tipRules, removeRule: Object.fromEntries(tipRules.map((r) => [r.id, true])),
    locked: !!(State.envs.find((e) => e.id === State.env) || {}).protected,
  };
  renderPublishModal();
  loadPublishDiff();
}
function renderPublishModal() {
  const b = el("publishModalBody"); if (b && window._publish) b.innerHTML = publishModalBodyHtml(window._publish);
}
function publishDiffLabelText(p) {
  return p.diffMode === "rendered"
    ? `showing the rendered result${p.tc ? ` · ${esc(p.tc)}` : ""}`
    : "showing the source changes";
}
function publishDiffBody(p, res) {
  if (res.error) return `<div class="empty">⚠ ${esc(res.error)}</div>`;
  if (p.diffMode === "rendered") {
    const left = res.left || "", right = res.right || "";
    if (left === right)
      return `<div style="padding:7px 14px;font-size:11px;color:var(--faint)">no rendered change for this context</div>
        <div class="diffbox" style="padding:12px 18px;white-space:pre-wrap">${esc(right)}</div>`;
    return `<div class="sxs-heads">${sxsPaneHead("What people get now")}${sxsPaneHead("What they'd get after")}</div>
      <div class="sxs">${renderSideBySide(left, right)}</div>`;
  }
  const html = renderUnifiedDiff(res.diff);
  return html ? `<div class="diffbox">${html}</div>` : '<div class="empty">No source changes.</div>';
}
function fetchPublishDiff(p) {
  let q = `a_version=${p.v}&a_sha=${enc(p.liveFull || p.toSha)}&b_version=${p.v}&b_sha=${enc(p.toSha)}&mode=${p.diffMode}&environment=${enc(p.env)}`;
  if (p.diffMode === "rendered" && p.tc) q += `&test_context=${enc(p.tc)}`;
  return GET(`/mgmt/prompts/${enc(p.pid)}/diff?${q}`);
}
async function loadPublishDiff() {
  const box = el("publishDiffBox"); if (!box) return;
  const p = window._publish;
  box.innerHTML = '<div class="empty">Loading preview…</div>';
  try {
    let res = await fetchPublishDiff(p);
    // No usable rendered diff → fall back to source, and relabel what's shown.
    if (res.error && p.diffMode === "rendered") {
      p.diffMode = "source";
      const lbl = el("publishDiffLabel"); if (lbl) lbl.innerHTML = "· " + publishDiffLabelText(p);
      res = await fetchPublishDiff(p);
    }
    if (el("publishDiffBox")) el("publishDiffBox").innerHTML = publishDiffBody(p, res);
  } catch (e) {
    if (el("publishDiffBox")) el("publishDiffBox").innerHTML = `<div class="empty">⚠ ${esc(errText(e))}</div>`;
  }
}
function publishModalBodyHtml(p) {
  const isRevert = p.mode === "revert";
  const title = isRevert ? `Go back to an earlier state — ${esc(p.env)}` : `Publish to ${esc(p.env)}`;
  const intro = isRevert
    ? "This makes an earlier state of this prompt live again for everyone."
    : "This makes the latest edits live for everyone.";
  const facts = `<div style="display:flex;gap:14px;flex-wrap:wrap;font-size:11.5px;color:var(--mut);margin:2px 0 14px">
    <span class="mono">${esc(p.pid)}</span><span>Version ${p.v}</span>
    <span class="mono faint">${esc(p.liveShort || "—")} → ${esc(p.toShort || "…")}</span></div>`;
  const tcChips = (p.diffMode === "rendered" && p.tcs.length)
    ? `<div id="publishTcChips" style="display:flex;gap:6px;margin-bottom:8px;flex-wrap:wrap">${
        p.tcs.map((t) => `<button type="button" class="chip btn-bare ${t.name === p.tc ? "active" : ""}" aria-pressed="${t.name === p.tc}" data-act="publishTc" data-name="${esc(t.name)}">${esc(t.name)}</button>`).join("")}</div>` : "";
  const whatChanges = `<div class="groupname" style="margin-top:2px">What changes
      <span id="publishDiffLabel" style="font-weight:400;text-transform:none;letter-spacing:0;color:var(--faint)">· ${publishDiffLabelText(p)}</span></div>
    ${tcChips}
    <div class="card"><div class="modal-diff" id="publishDiffBox"><div class="empty">Loading preview…</div></div></div>`;
  const ruleRows = p.tipRules.length
    ? `<div style="font-size:11px;color:var(--faint);margin:8px 0 4px">Already seeing these edits (test rules):</div>` +
      p.tipRules.map((r) => `<label class="cmp-check" style="display:flex;gap:8px;margin:5px 0;align-items:flex-start">
        <input type="checkbox" data-act="publishRuleToggle" data-id="${esc(r.id)}"${p.removeRule[r.id] ? " checked" : ""}>
        <span style="line-height:1.5"><b>${esc(r.comment || r.id)}</b> — <span class="faint">also remove this test rule after publishing (it becomes redundant)</span></span></label>`).join("")
    : "";
  const affected = `<div class="groupname" style="margin-top:16px">Who's affected</div>
    <div style="font-size:12.5px;color:var(--mut)">Everyone currently on <b>Version ${p.v}</b> (the default).</div>${ruleRows}`;
  const subjects = isRevert ? [] : (p.history || []).slice(0, p.tipAhead).map((h) => h.subject).filter(Boolean);
  const edits = subjects.length
    ? `<div class="groupname" style="margin-top:16px">The edits going live</div>
       <ul style="margin:0;padding-left:18px;font-size:12px;color:var(--mut);line-height:1.7">${subjects.map((s) => `<li>${esc(s)}</li>`).join("")}</ul>` : "";
  const confirmBlock = p.locked
    ? `<div style="margin:16px 0 2px;font-size:11px;color:var(--faint)"><b>${esc(p.env)}</b> is locked. Type <span class="mono" style="color:var(--mut)">${esc(p.pid)}</span> to confirm:</div>
       <input id="publishConfirm" data-token="${esc(p.pid)}" spellcheck="false" autocomplete="off" placeholder="${esc(p.pid)}"
         style="width:100%;font-family:'IBM Plex Mono',monospace"
         oninput="var b=document.getElementById('publishBtn');b.disabled=(this.value.trim()!==this.dataset.token)">` : "";
  return `<h3>${title}</h3>
    <p class="hint">${intro}</p>
    ${facts}${whatChanges}${affected}${edits}${confirmBlock}
    <div class="modal-actions">
      <button class="btn" data-act="closeModal">Cancel</button>
      <button id="publishBtn" class="btn primary"${p.locked ? " disabled" : ""} data-act="publishConfirm">${isRevert ? "Make this live again" : "Publish"}</button></div>`;
}

async function screenSegments() {
  const d = await GET(`/mgmt/envs/${enc(State.env)}/segments`);
  const segs = d.segments || [];
  const names = segs.map((s) => s.name);
  let se = window._segEdit;
  // Repair stale state: a selection that no longer exists (env switch, deletion) resets.
  if (se && !se.creating && se.selected && !names.includes(se.selected)) se = null;
  if (!se) {
    se = segs.length
      ? { creating: false, selected: segs[0].name, name: segs[0].name, cb: newCb(segs[0].when), err: "" }
      : { creating: true, selected: null, name: "", cb: newCb(null), err: "" };
  }
  se.raw = segs; se.allSegments = names;
  window._segEdit = se;
  el("main").innerHTML = `<div class="screen">
    <div class="h1row"><span class="h1 sm serif">Segments — <i>${esc(State.env)}</i></span>
      <span class="sub">named groups of users that rules can target</span></div>
    <div class="panelrow">
      <div id="segList" style="flex:1 1 220px;display:flex;flex-direction:column;gap:10px">${segListHtml(se)}</div>
      <div id="segEditor" style="flex:10 1 420px;min-width:0">${segEditorHtml(se)}</div>
    </div></div>`;
}

// Explorable audit — actor/action selects + object substring, applied via query
// re-fetch (module state). Rows open a before/after detail modal.
let _auditFilter = { actor: "", action: "", object: "" };
let _auditRows = [];

async function screenAudit() {
  let q = "limit=200";
  if (_auditFilter.actor) q += `&actor=${enc(_auditFilter.actor)}`;
  if (_auditFilter.action) q += `&action=${enc(_auditFilter.action)}`;
  if (_auditFilter.object) q += `&object=${enc(_auditFilter.object)}`;
  const d = await GET(`/mgmt/audit?${q}`);
  _auditRows = d.audit || [];
  const anyActive = !!(_auditFilter.actor || _auditFilter.action || _auditFilter.object);
  const actorOpts = `<option value="">All actors</option>` +
    (d.actors || []).map((a) => `<option value="${esc(a)}"${a === _auditFilter.actor ? " selected" : ""}>${esc(a)}</option>`).join("");
  const actionOpts = `<option value="">All actions</option>` +
    (d.actions || []).map((a) => `<option value="${esc(a)}"${a === _auditFilter.action ? " selected" : ""}>${esc(a)}</option>`).join("");
  const rows = _auditRows.map((a) =>
    `<tr class="grow-row click">
      <td class="mono muted"><button type="button" class="btn-bare aud-when" data-act="auditDetail" data-id="${esc(a.id)}" aria-label="See change detail">${new Date(a.at).toLocaleString()}</button></td>
      <td><b>${esc(a.actor)}</b></td>
      <td><span class="tag acc">${esc(a.action)}</span></td>
      <td class="mono" style="font-size:11.5px">${a.object_type ? `<span class="faint">${esc(a.object_type)}</span> ` : ""}${esc(a.object_id)}</td></tr>`).join("");
  el("main").innerHTML = `<div class="screen">
    <div class="h1row"><span class="h1 sm serif">Audit</span><span class="sub">every change — who made it and when</span></div>
    <div style="display:flex;gap:10px;flex-wrap:wrap;align-items:center;margin-bottom:14px">
      <select class="envsel" aria-label="Filter by actor" data-act="auditActor">${actorOpts}</select>
      <select class="envsel" aria-label="Filter by action" data-act="auditAction">${actionOpts}</select>
      <input class="search" id="auditObject" aria-label="Filter by object" data-act="auditObject" placeholder="object contains…" spellcheck="false" value="${esc(_auditFilter.object)}">
      ${anyActive ? `<button type="button" class="link btn-bare" data-act="auditClear">Clear filters</button>` : ""}</div>
    <div class="card" style="overflow-x:auto"><table class="grid">
      <thead class="ghead"><tr><th>When</th><th>Actor</th><th>Action</th><th>Object</th></tr></thead>
      <tbody>${rows || `<tr><td colspan="4" class="empty">No audit entries${anyActive ? " match these filters" : ""}.</td></tr>`}</tbody></table></div>
    <div style="font-size:11px;color:var(--faint);margin-top:10px">Click any row to see the before &amp; after of that change.</div></div>`;
}
// Detail modal — before/after as two side-by-side JSON panes. esc() is load-bearing:
// audit payloads are arbitrary user/rule content.
function openAuditDetail(a) {
  const when = new Date(a.at).toLocaleString();
  // Only prompt-scoped objects link somewhere useful; everything else stays plain.
  const link = a.object_type === "prompt" && a.object_id
    ? ` <a href="#/p/${enc(a.object_id)}/overview" data-act="go" data-hash="#/p/${enc(a.object_id)}/overview">view →</a>` : "";
  const pane = (label, val) => {
    const txt = val == null ? "—" : JSON.stringify(val, null, 2);
    return `<div style="flex:1 1 240px;min-width:0"><div class="groupname">${label}</div>
      <pre class="audit-pre">${esc(txt)}</pre></div>`;
  };
  const fact = (k, v) => `<div><span class="faint" style="display:inline-block;min-width:72px">${k}</span> ${v}</div>`;
  openModal(`
    <h3>Change detail</h3>
    <div style="display:flex;flex-direction:column;gap:6px;font-size:12.5px;margin-bottom:14px">
      ${fact("When", esc(when))}
      ${fact("Actor", `<b>${esc(a.actor)}</b>`)}
      ${fact("Action", `<span class="tag acc">${esc(a.action)}</span>`)}
      ${fact("Object", `<span class="mono">${a.object_type ? esc(a.object_type) + " · " : ""}${esc(a.object_id)}</span>${link}`)}
    </div>
    <div style="display:flex;gap:14px;flex-wrap:wrap">${pane("Before", a.before)}${pane("After", a.after)}</div>
    <div class="modal-actions"><button class="btn" data-act="closeModal">Close</button></div>`, "wide");
}

function _scopeLabel(b) {
  if (!b.project_id && !b.environment_id) return "instance-wide";
  const parts = [];
  if (b.project_id) parts.push("project " + b.project_id);
  if (b.environment_id) parts.push("env " + b.environment_id);
  return parts.join(" · ");
}

async function screenAccess() {
  await ensureWhoami();   // for the "you're signed in as" note
  let d;
  try {
    d = await GET(`/mgmt/principals`);
  } catch (e) {
    el("main").innerHTML = `<div class="screen"><div class="h1row"><span class="h1 sm serif">Access</span></div>
      <div class="empty">${e.status === 403 ? "Admin access required to manage users." : esc(errText(e))}</div></div>`;
    return;
  }
  State._access = d;  // roles/projects/envs cached for the modals
  const cards = d.principals.map((p) => {
    const bindings = p.bindings.map((b) =>
      `<span class="tag acc" style="display:inline-flex;gap:6px;align-items:center">
        ${esc(b.role)} · <span class="faint" style="font-weight:400">${esc(_scopeLabel(b))}</span>
        <button type="button" class="link btn-bare" data-act="removeBinding" data-pid="${esc(p.id)}" data-bid="${b.id}" aria-label="Remove ${esc(b.role)} role"
          style="color:var(--danger)">✕</button></span>`).join(" ") ||
      '<span class="faint" style="font-size:11px">no roles</span>';
    const keys = p.keys.map((k) =>
      `<div style="display:flex;gap:10px;align-items:center;font-size:11px">
        <span class="mono ${k.revoked ? "faint" : ""}">${esc(k.prefix)}…</span>
        ${k.revoked ? '<span class="pill warn">revoked</span>'
                    : '<span class="pill live">active</span>'}
        <span class="faint">${k.last_used_at ? "used " + ago(k.last_used_at) : "never used"}</span>
        ${k.revoked ? "" : `<button type="button" class="link btn-bare" data-act="revokeKey" data-kid="${k.id}"
           style="color:var(--danger);margin-left:auto">revoke</button>`}</div>`).join("") ||
      '<span class="faint" style="font-size:11px">no keys</span>';
    return `<div class="card pad">
      <div style="display:flex;gap:10px;align-items:center;flex-wrap:wrap">
        <span style="font-size:13.5px;font-weight:700">${esc(p.name || p.id)}</span>
        <span class="tag mut">${esc(p.kind)}</span>
        <span class="mono faint" style="font-size:11.5px">${esc(p.id)}</span>
        <div class="grow"></div>
        <button class="btn" data-act="addBinding" data-pid="${esc(p.id)}">+ role</button>
        <button class="btn" data-act="issueKey" data-pid="${esc(p.id)}" data-name="${esc(p.name)}">+ key</button></div>
      <div style="margin-top:10px;display:flex;gap:6px;flex-wrap:wrap;align-items:center">${bindings}</div>
      <div style="margin-top:12px;border-top:1px solid var(--line2);padding-top:10px;display:flex;flex-direction:column;gap:6px">${keys}</div>
    </div>`;
  }).join("") || '<div class="empty">No users yet.</div>';

  const me = State.me, myRole = me ? highestRole(me.roles) : null;
  const yourKey = `<div class="card pad" style="margin-bottom:14px">
    <div style="font-size:12.5px">You're signed in as <b>${me ? esc(me.name) : "…"}</b>${myRole ? ` <span class="pill acc">${esc(myRole)}</span>` : ""}.</div>
    <div class="faint" style="font-size:11.5px;margin-top:4px">Keys are shown once at creation and can only be revoked — your current key isn't displayed anywhere. Switch keys from the account menu in the sidebar.</div></div>`;

  el("main").innerHTML = `<div class="screen">
    <div class="h1row"><span class="h1 sm serif">Access</span>
      <span class="sub">users, roles, and API keys</span>
      <div class="grow"></div>
      <button class="btn primary" data-act="newUser">+ New user</button></div>
    ${yourKey}
    <div style="display:flex;flex-direction:column;gap:12px">${cards}</div>
    <div style="font-size:11px;color:var(--faint);margin-top:14px">Roles: renderer &lt; viewer &lt; editor &lt; operator &lt; releaser &lt; admin. A role can be scoped instance-wide, to a project, to an environment, or to both. Keys are shown once at creation and can only be revoked, not recovered.</div></div>`;
}

// Options helpers for the role/scope selectors in the access modals.
function _roleOpts(sel) {
  return (State._access?.roles || []).map((r) =>
    `<option value="${esc(r)}"${r === sel ? " selected" : ""}>${esc(r)}</option>`).join("");
}
function _projectOpts() {
  return '<option value="">— all projects —</option>' +
    (State._access?.projects || []).map((p) => `<option value="${esc(p)}">${esc(p)}</option>`).join("");
}
function _envOpts() {
  return '<option value="">— all environments —</option>' +
    (State._access?.environments || []).map((e) => `<option value="${esc(e)}">${esc(e)}</option>`).join("");
}
function _showKeyModal(key) {
  openModal(`
    <h3>API key created</h3>
    <p class="hint">Copy it now — it is <b>not recoverable</b>. This is the only time it's shown.</p>
    <input readonly onclick="this.select()" value="${esc(key)}"
      style="width:100%;font-family:'IBM Plex Mono',monospace;font-size:12px">
    <div class="modal-actions"><button class="btn primary" data-act="closeModal">Done</button></div>`);
}

function renderPlayResult(r, pinned) {
  const matched = typeof r.matched_rule === "string"
    ? r.matched_rule : `${r.matched_rule.scope}:${r.matched_rule.id}`;
  const versLines = Object.entries(r.versions).map(([k, v]) =>
    `${esc(k)} → v${v.version} · ${esc(v.commit)}${v.fallback ? " (fallback)" : ""}`).join("<br>");
  // Warnings stay visible; the matched rule, rules_version and resolved versions
  // (the reproducible pin) are the technical detail.
  const flags = [
    r.stale_rules ? '<span class="pill warn">stale rules</span>' : "",
    r.content_fallback ? '<span class="pill warn">content fallback</span>' : "",
  ].join(" ");
  const techInner = `matched rule: ${esc(matched)}<br>rules_version ${esc(String(r.rules_version))}` +
    `<br><br>Resolved versions (pin):<br>${versLines}`;
  return `<div class="card pad">
    <div style="display:flex;gap:10px;align-items:center;margin-bottom:10px;flex-wrap:wrap">
      ${pinned ? '<span class="tag acc">PINNED REPLAY</span>' : '<span class="tag mut">rendered</span>'}${flags}
      <div class="grow"></div>
      ${pinned ? '<span class="pill live">pinned</span>' : ""}</div>
    <div class="render-out" style="white-space:pre-wrap;margin:0">${esc(r.prompt)}</div>
    <div style="margin-top:12px;display:flex;gap:10px;align-items:center;flex-wrap:wrap">
      <button class="btn" data-act="pinLast">⚓ Reproduce exactly (pin)</button>
      <span class="faint" style="font-size:11px">re-renders ignoring targeting — same output regardless of flags</span></div>
    ${techDetails(techInner, "matched rule, resolved versions")}</div>`;
}

async function screenPlay() {
  // Prompt picker: enumerate what exists instead of asking for a free-typed id.
  let pids = [];
  try {
    const ov = await GET(`/mgmt/overview?environment=${enc(State.env)}`);
    pids = ov.projects.flatMap((p) => p.prompts.map((x) => x.prompt_id));
  } catch (_) { /* fall back to a text input below */ }
  const pid = window._playPid || State.route.q.pid || pids[0] || "support/system";
  if (pids.length && !pids.includes(pid)) pids.unshift(pid);
  const pidField = pids.length
    ? `<select id="playPid" class="envsel" style="width:100%;padding:9px 12px;border-radius:8px">${
        pids.map((p) => `<option value="${esc(p)}"${p === pid ? " selected" : ""}>${esc(p)}</option>`).join("")}</select>`
    : `<input id="playPid" value="${esc(pid)}" spellcheck="false">`;
  const flags = window._playFlags != null ? window._playFlags : '{"user_id": "u_12"}';
  const vars = window._playVars != null ? window._playVars
    : '{"customer_name": "Acme", "history": []}';
  const pinned = !!window._playPin;
  const last = window._playLast;
  el("main").innerHTML = `<div class="screen">
    <div class="h1row"><span class="h1 sm serif">Playground — <i>${esc(State.env)}</i></span>
      <span class="sub">try a request as any user — see exactly what they'd get, and capture a pin to reproduce it</span></div>
    <div class="panelrow">
      <div style="flex:1 1 300px;display:flex;flex-direction:column;gap:12px">
        <div class="field"><label>Prompt</label>${pidField}</div>
        <div class="field"><label>Flags (JSON)</label>
          <textarea id="playFlags" spellcheck="false" style="min-height:66px">${esc(flags)}</textarea></div>
        <div class="field"><label>Variables (JSON)</label>
          <textarea id="playVars" spellcheck="false" style="min-height:66px">${esc(vars)}</textarea></div>
        <div style="display:flex;gap:10px;align-items:center">
          <button class="btn primary" data-act="play">Render</button>
          ${pinned ? '<span class="pill live">pinned</span><button type="button" class="link mut btn-bare" data-act="unpin">clear pin</button>' : ""}
        </div>
      </div>
      <div style="flex:10 1 440px;min-width:0">${
        last ? renderPlayResult(last, pinned)
             : '<div class="empty">Render a prompt to see the resolved output and its reproducible pin.</div>'}</div>
    </div></div>`;
}

// ══ targeting composer + reusable clause builder ═════════════════════════════
// The clause builder drives three surfaces: the rule composer's "People who match…",
// the segments editor, and the composer's inline "＋ new segment" mini-form. State is a
// plain object {rows, advanced, advancedJson}; each row is
//   {kind:"flag"|"segment", flag, op, value, segment, newSeg}. Rows AND together.
// Shapes the row builder can't express (any / not / nested all) fall back to a raw-JSON
// textarea (advanced=true). window._composer / window._segEdit hold open-surface state
// (the window._dp idiom); the html builders are pure functions for round-trip testing.

const CLAUSE_OPS = [
  ["eq", "is"], ["neq", "is not"], ["in", "is one of"], ["not_in", "is not one of"],
  ["contains", "contains"], ["starts_with", "starts with"], ["ends_with", "ends with"],
  ["matches", "matches regex"], ["gt", ">"], ["gte", "≥"], ["lt", "<"], ["lte", "≤"],
  ["semver_gt", "semver >"], ["semver_lt", "semver <"], ["exists", "exists"],
];

function isLeafClause(c) { return !!c && (c.segment !== undefined || c.flag !== undefined); }
// Coerce a typed value to a JSON scalar (number / bool / string) for the rule payload.
function coerceVal(s) {
  const t = String(s == null ? "" : s).trim();
  if (t === "") return "";
  if (/^-?\d+(\.\d+)?$/.test(t)) return Number(t);
  if (t === "true") return true;
  if (t === "false") return false;
  return t;
}
function clauseToRow(c) {
  if (c.segment !== undefined)
    return { kind: "segment", segment: c.segment, flag: "", op: "eq", value: "", newSeg: null };
  let value = "";
  if (c.values !== undefined) value = (c.values || []).join(", ");
  else if (c.value !== undefined) value = String(c.value);
  return { kind: "flag", flag: c.flag || "", op: c.op || "eq", value, segment: "", newSeg: null };
}
function rowToClause(row) {
  if (row.kind === "segment") {
    const name = row.segment === "__new" ? ((row.newSeg && row.newSeg.name) || "").trim() : row.segment;
    return name ? { segment: name } : null;
  }
  const flag = (row.flag || "").trim();
  if (!flag) return null;
  if (row.op === "exists") return { flag, op: "exists" };
  if (row.op === "in" || row.op === "not_in")
    return { flag, op: row.op, values: (row.value || "").split(",").map((s) => s.trim()).filter(Boolean) };
  return { flag, op: row.op, value: coerceVal(row.value) };
}
// null ⇒ the shape can't be shown in the row builder → caller uses the advanced editor.
function whenToRows(when) {
  if (when == null) return [];
  if (isLeafClause(when)) return [clauseToRow(when)];
  if (when.all && Array.isArray(when.all) && when.all.length && when.all.every(isLeafClause))
    return when.all.map(clauseToRow);
  return null;
}
function rowsToWhen(rows) {
  const clauses = rows.map(rowToClause).filter(Boolean);
  if (!clauses.length) return null;
  if (clauses.length === 1) return clauses[0];
  return { all: clauses };
}
function emptyFlagRow() { return { kind: "flag", flag: "", op: "eq", value: "", segment: "", newSeg: null }; }
function newCb(when) {
  const rows = whenToRows(when);
  if (rows == null) return { rows: [], advanced: true, advancedJson: JSON.stringify(when, null, 2) };
  return { rows, advanced: false, advancedJson: "" };
}

function opOptions(sel) {
  return CLAUSE_OPS.map(([v, label]) =>
    `<option value="${v}"${v === sel ? " selected" : ""}>${esc(label)}</option>`).join("");
}
function segOptions(sel, segments, allowNew) {
  const opts = ['<option value="">— pick a segment —</option>'].concat(
    (segments || []).map((s) => `<option value="${esc(s)}"${s === sel ? " selected" : ""}>${esc(s)}</option>`));
  if (allowNew) opts.push(`<option value="__new"${sel === "__new" ? " selected" : ""}>＋ new segment…</option>`);
  return opts.join("");
}

// cb: {rows, advanced, advancedJson}; prefix: DOM-id namespace; segCtx: {segments, allowNew}.
function cbHtml(cb, prefix, segCtx) {
  const segments = (segCtx && segCtx.segments) || [];
  const allowNew = !(segCtx && segCtx.allowNew === false);
  if (cb.advanced) {
    return `<div class="cb" id="${prefix}-wrap">
      <div class="cb-adv-note">Advanced condition — a shape the simple builder can't show (any-of / not / nesting). Edit the raw JSON.</div>
      <textarea class="cb-adv" id="${prefix}-adv" spellcheck="false">${esc(cb.advancedJson || "")}</textarea>
      <div class="cb-foot"><button type="button" class="link btn-bare" data-act="cbSimple" data-prefix="${prefix}">↺ back to the simple builder</button></div>
    </div>`;
  }
  const rows = cb.rows.map((row, i) => cbRowHtml(row, prefix, i, segments, allowNew)).join("");
  const empty = cb.rows.length === 0
    ? `<div class="cb-empty"><b>Everyone</b> matches — every request. Add a condition to narrow who this applies to.</div>` : "";
  return `<div class="cb" id="${prefix}-wrap">
    ${empty}${rows}
    <div class="cb-foot">
      <button type="button" class="link btn-bare" data-act="cbAdd" data-prefix="${prefix}">＋ add condition</button>
      ${cb.rows.length > 1 ? `<span class="cb-and">all must match · AND</span>` : ""}
      <span class="grow"></span>
      <button type="button" class="link mut btn-bare" data-act="cbAdvanced" data-prefix="${prefix}">edit as JSON</button>
    </div>
    <div class="cb-hint">Rows combine with <b>AND</b>. Need OR? build a segment and reference it here.</div>
  </div>`;
}
function cbRowHtml(row, prefix, i, segments, allowNew) {
  const p = `${prefix}-r${i}`;
  const kindSel = `<select class="cb-sel" id="${p}-kind" data-act="cbKind" data-prefix="${prefix}" data-ri="${i}">
      <option value="flag"${row.kind === "flag" ? " selected" : ""}>flag</option>
      <option value="segment"${row.kind === "segment" ? " selected" : ""}>in segment</option></select>`;
  let controls, nested = "";
  if (row.kind === "segment") {
    controls = `<select class="cb-sel grow" id="${p}-seg" data-act="cbSeg" data-prefix="${prefix}" data-ri="${i}">${segOptions(row.segment, segments, allowNew)}</select>`;
    if (row.segment === "__new") {
      const ns = row.newSeg || { name: "", cb: newCb(null) };
      nested = `<div class="cb-newseg">
        <div class="field" style="margin:0 0 8px"><label>New segment name</label>
          <input id="${p}-nsname" value="${esc(ns.name || "")}" placeholder="e.g. enterprise-us" spellcheck="false" style="font-family:'IBM Plex Mono',monospace"></div>
        <div class="cb-newseg-label">who's in it — match all of:</div>
        ${cbHtml(ns.cb || newCb(null), `${p}-ns`, { segments, allowNew: false })}</div>`;
    }
  } else {
    const isExists = row.op === "exists";
    const valPh = (row.op === "in" || row.op === "not_in") ? "comma,separated,values" : "value";
    controls = `<input class="cb-flag" id="${p}-flag" value="${esc(row.flag)}" placeholder="flag name" spellcheck="false">
      <select class="cb-sel" id="${p}-op" data-act="cbOp" data-prefix="${prefix}" data-ri="${i}">${opOptions(row.op)}</select>
      ${isExists ? "" : `<input class="cb-val grow" id="${p}-val" value="${esc(row.value)}" placeholder="${valPh}" spellcheck="false">`}`;
  }
  return `<div class="cb-row">${kindSel}${controls}
    <button type="button" class="cb-del btn-bare" data-act="cbDel" data-prefix="${prefix}" data-ri="${i}" aria-label="Remove condition">✕</button></div>${nested}`;
}
// Read the live DOM values of one builder back into its state (recurses into new-segments).
function cbSync(cb, prefix) {
  if (cb.advanced) { const ta = el(`${prefix}-adv`); if (ta) cb.advancedJson = ta.value; return; }
  cb.rows.forEach((row, i) => {
    const p = `${prefix}-r${i}`;
    const k = el(`${p}-kind`); if (k) row.kind = k.value;
    if (row.kind === "flag") {
      const f = el(`${p}-flag`); if (f) row.flag = f.value;
      const o = el(`${p}-op`); if (o) row.op = o.value;
      const v = el(`${p}-val`); if (v) row.value = v.value;
    } else {
      const s = el(`${p}-seg`); if (s) row.segment = s.value;
      if (row.segment === "__new") {
        if (!row.newSeg) row.newSeg = { name: "", cb: newCb(null) };
        const n = el(`${p}-nsname`); if (n) row.newSeg.name = n.value;
        cbSync(row.newSeg.cb, `${p}-ns`);
      }
    }
  });
}
// Resolve a builder prefix to its state object + the host surface that owns it.
function cbResolve(prefix) {
  if (prefix === "co") return { cb: window._composer && window._composer.cb, host: "composer" };
  if (prefix === "sg") return { cb: window._segEdit && window._segEdit.cb, host: "seg" };
  const m = prefix.match(/^co-r(\d+)-ns$/);
  if (m && window._composer) {
    const row = window._composer.cb.rows[+m[1]];
    if (row && row.newSeg) return { cb: row.newSeg.cb, host: "composer" };
  }
  return { cb: null, host: null };
}
function cbHostSync(host) { if (host === "composer") composerSync(); else if (host === "seg") segEditSync(); }
function cbHostRender(host) { if (host === "composer") renderComposer(); else if (host === "seg") renderSegEditor(); }

// ── rule model helpers (pure, testable) ──────────────────────────────
function activeOrderedRules(rules, excludeId) {
  return (rules || []).filter((r) => r.status === "active" && r.id !== excludeId)
    .slice().sort((a, b) => a.priority - b.priority);
}
function ruleUpsertBody(r, priority) {
  const x = { id: r.id, scope: r.scope, priority, when: r.when, serve: r.serve, status: r.status, comment: r.comment };
  if (r.scope === "prompt") x.prompt_id = r.prompt_id;
  return x;
}
// Renumber active rules 10/20/30… with the new rule inserted at `slot`; returns the new
// rule's priority + the neighbours whose priority must change (upsert plan).
function renumberPlan(active, slot) {
  const seq = active.slice(); seq.splice(slot, 0, { __new: true });
  let priority = 10; const plan = [];
  seq.forEach((r, i) => {
    const p = (i + 1) * 10;
    if (r.__new) priority = p;
    else if (r.priority !== p) plan.push({ rule: r, priority: p });
  });
  return { priority, plan };
}
// Priority for a rule dropped at `slot` among active rules: midpoint of neighbours when
// an integer gap exists, else a full renumber.
function placePriority(active, slot) {
  const prev = slot > 0 ? active[slot - 1].priority : null;
  const next = slot < active.length ? active[slot].priority : null;
  if (prev == null && next == null) return { priority: 10, plan: [] };
  if (prev == null) { const p = next - 10; return p >= 1 ? { priority: p, plan: [] } : renumberPlan(active, slot); }
  if (next == null) return { priority: prev + 10, plan: [] };
  const mid = Math.floor((prev + next) / 2);
  if (mid > prev && mid < next) return { priority: mid, plan: [] };
  return renumberPlan(active, slot);
}
// The two upsert bodies for a ↑/↓ neighbour swap (or null at an end).
function swapPriorityPayloads(rules, id, dir) {
  const sorted = (rules || []).slice().sort((a, b) => a.priority - b.priority);
  const idx = sorted.findIndex((r) => r.id === id);
  const j = dir === "up" ? idx - 1 : idx + 1;
  if (idx < 0 || j < 0 || j >= sorted.length) return null;
  const a = sorted[idx], b = sorted[j];
  return [ruleUpsertBody(a, b.priority), ruleUpsertBody(b, a.priority)];
}
function slugId(comment) {
  const base = String(comment || "").toLowerCase().replace(/[^a-z0-9]+/g, "-")
    .replace(/^-+|-+$/g, "").slice(0, 40) || "rule";
  return `${base}-${Math.random().toString(36).slice(2, 6)}`;
}
function segmentPayload(name, when) { return { name: String(name || "").trim(), when: when == null ? null : when }; }

// ── serve <-> composer state (pure, testable) ────────────────────────
function defaultVersion(versions, pid, defaults) {
  if (pid && defaults && defaults[pid] != null) return defaults[pid];
  const def = (versions || []).find((v) => v.is_default);
  if (def) return def.version;
  return versions && versions[0] ? versions[0].version : null;
}
function serveToComposer(serve, versions, pid, defaults) {
  const out = { serveMode: "version", version: null, at: "live",
    rollout: { bucket_by: "user_id", weights: [], defaultWeight: 100 }, serveNote: "" };
  if (serve && serve.rollout) {
    out.serveMode = "rollout";
    const ws = serve.rollout.weights || [];
    out.rollout.bucket_by = serve.rollout.bucket_by || "user_id";
    out.rollout.weights = ws.filter((w) => !w.default)
      .map((w) => ({ version: w.version != null ? w.version : "", weight: w.weight, label: w.label || "" }));
    const def = ws.find((w) => w.default);
    out.rollout.defaultWeight = def ? def.weight
      : Math.max(0, 100 - out.rollout.weights.reduce((a, w) => a + (Number(w.weight) || 0), 0));
    out.version = defaultVersion(versions, pid, defaults);
    return out;
  }
  if (serve && serve.version != null) {
    out.version = serve.version; out.at = serve.at === "tip" ? "tip" : "live"; return out;
  }
  if (serve && serve.label) {
    out.serveNote = `This rule serves label “${serve.label}”. Saving replaces it with the version-based serve below.`;
    out.version = defaultVersion(versions, pid, defaults); return out;
  }
  out.version = defaultVersion(versions, pid, defaults);
  return out;
}
// Build the serve payload from composer state; throws a human string on validation error.
function composerServe(co) {
  if (co.serveMode === "rollout") {
    const weights = [];
    for (const w of co.rollout.weights) {
      if (w.version === "" || w.version == null) throw "Pick a version for every rollout arm.";
      const wt = Number(w.weight);
      if (!(wt >= 0)) throw "Every rollout weight must be a number ≥ 0.";
      const arm = { version: Number(w.version), weight: wt };
      if (w.label) arm.label = w.label;
      weights.push(arm);
    }
    if (!weights.length) throw "Add at least one version to the rollout.";
    weights.push({ default: true, weight: Number(co.rollout.defaultWeight) || 0 });
    const sum = weights.reduce((a, w) => a + w.weight, 0);
    if (sum !== 100) throw `Weights must add up to 100 (currently ${sum}).`;
    return { rollout: { bucket_by: (co.rollout.bucket_by || "").trim() || "user_id", weights } };
  }
  if (co.version == null || co.version === "") throw "Pick a version to serve.";
  return co.at === "tip" ? { version: Number(co.version), at: "tip" } : { version: Number(co.version) };
}

// ── composer html (pure builders that read a passed-in state object) ──
function composerVersionHtml(co) {
  const liveVer = defaultVersion(co.versions, co.prompt_id, co.defaults);
  const label = (v) => {
    let tag = "not live";
    if (v.status === "archived") tag = "archived";
    else if (v.version === liveVer) tag = "live for everyone";
    else if (v.live_sha) tag = "live";
    return `Version ${v.version}${v.label ? " · " + v.label : ""} — ${tag}`;
  };
  const picker = (co.versions && co.versions.length)
    ? `<select class="cb-sel" id="co-version" data-act="cbVersion" style="min-width:280px">${
        co.versions.map((v) => `<option value="${v.version}"${v.version === co.version ? " selected" : ""}>${esc(label(v))}</option>`).join("")}</select>`
    : `<input class="cb-flag" id="co-version" value="${esc(co.version == null ? "" : co.version)}" placeholder="version number" style="width:130px" data-act="cbVersion">`;
  return `<div class="cmp-serve">${picker}
    <label class="cmp-check" style="margin-top:10px"><input type="checkbox" id="co-tip" data-act="cbTip"${co.at === "tip" ? " checked" : ""}> the latest unpublished draft <span class="faint">(before it's published)</span></label>
    <div class="cb-hint">${co.at === "tip"
      ? "Serves the newest edits on this version — how you try changes before publishing them for everyone."
      : "Serves what's live now for this version."}</div></div>`;
}
function composerRolloutHtml(co) {
  const rows = co.rollout.weights.map((w, i) => {
    const vsel = (co.versions && co.versions.length)
      ? `<select class="cb-sel" id="co-rv${i}" data-act="cbRoWeight"><option value="">— version —</option>${
          co.versions.map((v) => `<option value="${v.version}"${String(w.version) === String(v.version) ? " selected" : ""}>v${v.version}${v.label ? " · " + esc(v.label) : ""}</option>`).join("")}</select>`
      : `<input class="cb-flag" id="co-rv${i}" value="${esc(w.version)}" placeholder="version" style="width:100px" data-act="cbRoWeight">`;
    return `<div class="ro-row">${vsel}
      <input class="cb-val" id="co-rw${i}" value="${esc(w.weight)}" style="width:80px" aria-label="Weight %" data-act="rolloutWeightInput" inputmode="numeric"><span class="faint" style="font-size:12px">%</span>
      <button type="button" class="cb-del btn-bare" data-act="cbRoDel" data-ri="${i}" aria-label="Remove rollout arm">✕</button></div>`;
  }).join("");
  const sum = co.rollout.weights.reduce((a, w) => a + (Number(w.weight) || 0), 0) + (Number(co.rollout.defaultWeight) || 0);
  return `<div class="cmp-serve">${rows}
    <div class="ro-row"><span class="muted" style="font-size:12.5px;min-width:130px">everyone else (default)</span>
      <input class="cb-val" id="co-rdef" value="${esc(co.rollout.defaultWeight)}" style="width:80px" data-act="rolloutWeightInput" inputmode="numeric"><span class="faint" style="font-size:12px">%</span></div>
    <div class="ro-foot"><button type="button" class="link btn-bare" data-act="cbRoAdd">＋ add a version</button><span class="grow"></span>
      <span class="ro-sum ${sum === 100 ? "ok" : "bad"}" id="rolloutSum">sum: ${sum}%</span></div>
    <div class="field" style="margin-top:12px;margin-bottom:0"><label>Bucket by (flag)</label>
      <input id="co-bucket" value="${esc(co.rollout.bucket_by)}" placeholder="user_id" spellcheck="false" style="max-width:220px;font-family:'IBM Plex Mono',monospace"></div>
    <div class="cb-hint">Each request is bucketed by this flag; a given user stays in the same arm as you ramp the weights.</div></div>`;
}
function composerServeHtml(co) {
  const body = co.serveMode === "rollout" ? composerRolloutHtml(co) : composerVersionHtml(co);
  const note = co.serveNote ? `<div class="cb-hint" style="color:var(--warn)">${esc(co.serveNote)}</div>` : "";
  return `<div class="cmp-modes">
      <button type="button" class="cmp-mode btn-bare ${co.serveMode === "version" ? "active" : ""}" aria-pressed="${co.serveMode === "version"}" data-act="cbServeMode" data-mode="version">A version</button>
      <button type="button" class="cmp-mode btn-bare ${co.serveMode === "rollout" ? "active" : ""}" aria-pressed="${co.serveMode === "rollout"}" data-act="cbServeMode" data-mode="rollout">A percentage rollout</button>
    </div>${body}${note}`;
}
function slotOptions(co) {
  const active = activeOrderedRules(co.rules, co.editing ? co.origId : null);
  const nameOf = (r) => r.comment || r.id;
  const opts = [];
  for (let i = 0; i <= active.length; i++) {
    let label;
    if (i === 0) label = active.length ? `1st — before “${nameOf(active[0])}”` : "1st — the only rule";
    else if (i === active.length) label = `last (${ordinal(i + 1)}) — after “${nameOf(active[i - 1])}”`;
    else label = `${ordinal(i + 1)} — after “${nameOf(active[i - 1])}”`;
    opts.push(`<option value="${i}"${i === co.slot ? " selected" : ""}>${esc(label)}</option>`);
  }
  return opts.join("");
}
function composerShadowHtml(co) {
  const active = activeOrderedRules(co.rules, co.editing ? co.origId : null);
  const before = active.slice(0, Math.min(co.slot, active.length));
  const applies = (r) => co.scope === "global" ? true : (r.scope === "global" || r.prompt_id === co.prompt_id);
  const shadowers = before.filter(applies);
  if (!shadowers.length)
    return `<div class="shadowbox ok">Nothing is checked before this rule — it gets first say for its audience.</div>`;
  const anyEveryone = shadowers.some((r) => r.when == null);
  const rows = shadowers.map((r) => {
    const everyone = r.when == null;
    return `<div class="shadow-row${everyone ? " danger" : ""}">
      <span>${everyone ? "⚠ matches <b>everyone</b>" : describeWhen(r.when)}</span>
      <span class="shadow-arrow">→ ${esc(r.comment || r.id)}</span>
      ${everyone ? `<div class="shadow-note">This earlier rule matches every request — your rule would never be reached.</div>` : ""}
    </div>`;
  }).join("");
  return `<div class="shadowbox${anyEveryone ? " danger" : ""}">
    <div class="shadow-head">Checked before this rule${anyEveryone ? " — this rule is blocked" : ""}:</div>
    ${rows}<div class="shadow-foot">If one of these matches a request first, your rule never fires for that request.</div></div>`;
}
function composerBodyHtml(co) {
  const scopePill = co.scope === "global" ? pill("acc", "all prompts")
    : (co.prompt_id ? `<span class="mono faint" style="font-size:11px">${esc(co.prompt_id)}</span>` : "");
  const idLine = co.editing ? `<span class="mono faint" style="font-size:11px">id ${esc(co.origId)} · can't change</span>` : "";
  return `
    <h3>${co.editing ? "Edit rule" : "New rule"}</h3>
    ${co.err ? `<div class="banner danger" style="margin:6px 0 14px"><span style="font-size:12.5px;font-weight:600">${esc(co.err)}</span></div>` : ""}
    <div class="cmp-sec"><div class="cmp-h">People who match…</div>${cbHtml(co.cb, "co", { segments: co.segments, allowNew: true })}</div>
    <div class="cmp-sec"><div class="cmp-h">…should see</div>${composerServeHtml(co)}</div>
    <div class="cmp-sec"><div class="cmp-h">Position</div>
      <div class="cmp-pos"><span class="muted" style="font-size:12.5px">Checked</span>
        <select class="cb-sel" id="co-slot" data-act="cbPos">${slotOptions(co)}</select></div>
      ${composerShadowHtml(co)}</div>
    <div class="cmp-sec">
      <div class="field" style="margin-bottom:8px"><label>What is this for?</label>
        <input id="co-comment" value="${esc(co.comment)}" placeholder="e.g. Beta on enterprise" spellcheck="false" style="font-family:'Instrument Sans',sans-serif"></div>
      <label class="cmp-check"><input type="checkbox" id="co-global" data-act="cbScope"${co.scope === "global" ? " checked" : ""}> applies to <b>all prompts</b> ${scopePill}</label>
      <div style="margin-top:6px">${idLine}</div></div>
    <div class="modal-actions">
      <button class="btn" data-act="closeModal">Cancel</button>
      <button class="btn primary" data-act="composerSave">${co.editing ? "Save changes" : "Create rule"}</button></div>`;
}
function renderComposer() { const b = el("composerBody"); if (b && window._composer) b.innerHTML = composerBodyHtml(window._composer); }
// Read every composer field from the DOM back into state (before any re-render / save).
function composerSync() {
  const co = window._composer; if (!co) return;
  const c = el("co-comment"); if (c) co.comment = c.value;
  const g = el("co-global"); if (g) co.scope = g.checked ? "global" : "prompt";
  const slot = el("co-slot"); if (slot) co.slot = parseInt(slot.value);
  cbSync(co.cb, "co");
  if (co.serveMode === "version") {
    const v = el("co-version"); if (v) co.version = v.value === "" ? null : (co.versions.length ? Number(v.value) : v.value);
    const t = el("co-tip"); if (t) co.at = t.checked ? "tip" : "live";
  } else {
    co.rollout.weights.forEach((w, i) => {
      const rv = el(`co-rv${i}`); if (rv) w.version = rv.value;
      const rw = el(`co-rw${i}`); if (rw) w.weight = rw.value;
    });
    const rdef = el("co-rdef"); if (rdef) co.rollout.defaultWeight = rdef.value;
    const b = el("co-bucket"); if (b) co.rollout.bucket_by = b.value;
  }
}
function buildComposerState({ rule, duplicate, targetPid, scope, rulesData, segments, versions }) {
  const co = {
    editing: !!rule && !duplicate,
    origId: rule && !duplicate ? rule.id : null,
    routePid: targetPid || null,
    scope: scope === "global" ? "global" : "prompt",
    prompt_id: scope === "global" ? null : (targetPid || null),
    status: rule ? rule.status : "active",
    comment: rule ? (rule.comment || "") : "",
    cb: newCb(rule ? rule.when : null),
    segments: segments || [], versions: versions || [],
    rules: rulesData.rules || [], defaults: rulesData.defaults || {}, protected: !!rulesData.protected,
    err: "",
  };
  Object.assign(co, serveToComposer(rule ? rule.serve : null, versions, targetPid, co.defaults));
  const active = activeOrderedRules(co.rules, co.editing ? rule.id : null);
  if (co.editing) co.slot = active.filter((r) => r.priority < rule.priority).length;
  else if (duplicate && rule) co.slot = active.filter((r) => r.priority <= rule.priority).length;
  else co.slot = active.length;
  return co;
}
async function openComposer({ rule, duplicate, pid }) {
  const env = State.env;
  const targetPid = (rule && rule.prompt_id) || pid || State.route.pid || null;
  openModal(`<div id="composerBody"><div class="empty">Loading…</div></div>`, "wide");
  let rulesData, segsData, versions = [];
  try {
    rulesData = _rulesData || await GET(`/mgmt/envs/${enc(env)}/rules`);
    segsData = await GET(`/mgmt/envs/${enc(env)}/segments`);
  } catch (e) { const b = el("composerBody"); if (b) b.innerHTML = `<div class="empty">⚠ ${esc(errText(e))}</div>`; return; }
  const scope = rule ? rule.scope : (targetPid ? "prompt" : "global");
  if (targetPid) {
    try { const dv = await GET(`/mgmt/prompts/${enc(targetPid)}/versions?environment=${enc(env)}`); versions = dv.versions; }
    catch (_) { /* fall back to a free version-number input */ }
  }
  window._composer = buildComposerState({
    rule, duplicate, targetPid, scope, rulesData,
    segments: (segsData.segments || []).map((s) => s.name), versions,
  });
  renderComposer();
}
// Create any inline "＋ new segment" rows before saving the rule; rewrites the row to
// reference the created segment by name.
async function composerCreateNewSegments(co) {
  for (const row of co.cb.rows) {
    if (row.kind === "segment" && row.segment === "__new" && row.newSeg) {
      const name = (row.newSeg.name || "").trim();
      if (!name) throw "Name the new segment (or pick an existing one).";
      const segWhen = row.newSeg.cb.advanced
        ? (row.newSeg.cb.advancedJson.trim() ? JSON.parse(row.newSeg.cb.advancedJson) : null)
        : rowsToWhen(row.newSeg.cb.rows);
      await POST(`/mgmt/envs/${enc(State.env)}/segments`, segmentPayload(name, segWhen));
      row.segment = name;
    }
  }
}

// ── segments editor (reuses the clause builder) ──────────────────────
function segListHtml(se) {
  const items = se.raw.map((s) =>
    `<button type="button" class="card pad seg-item btn-bare${!se.creating && se.selected === s.name ? " sel" : ""}" data-act="segSelect" data-name="${esc(s.name)}">
      <div style="font-size:13px;font-weight:700">${esc(s.name)}</div>
      <div style="font-size:11.5px;color:var(--mut);margin-top:3px">referenced by ${s.referenced_by} ${plural(s.referenced_by, "rule")}</div></button>`).join("");
  return `${items || '<div class="empty">No segments yet.</div>'}
    <button type="button" class="card pad seg-item btn-bare${se.creating ? " sel" : ""}" data-act="segNew" style="text-align:center;color:var(--acc-ink);font-weight:600">＋ New segment</button>`;
}
function segEditorHtml(se) {
  const existing = !se.creating && !!se.selected;
  const refCount = existing ? ((se.raw.find((s) => s.name === se.selected) || {}).referenced_by || 0) : 0;
  return `<div class="card pad">
    ${se.err ? `<div class="banner danger" style="margin-bottom:12px"><span style="font-size:12.5px;font-weight:600">${esc(se.err)}</span></div>` : ""}
    <div class="field"><label>Segment name${existing ? ` <span style="text-transform:none;font-weight:400">· can't change once created</span>` : ""}</label>
      ${existing ? `<div style="font-size:14px;font-weight:700">${esc(se.name)}</div>`
                 : `<input id="sg-name" value="${esc(se.name)}" placeholder="e.g. enterprise-us" spellcheck="false" style="font-family:'IBM Plex Mono',monospace"></div>`}
    ${existing ? `</div><div style="font-size:11px;color:var(--mut);margin:-4px 0 14px">referenced by ${refCount} ${plural(refCount, "rule")}</div>` : ""}
    <div class="cmp-h" style="font-size:16px;margin-bottom:8px">Who's in it — match all of:</div>
    ${cbHtml(se.cb, "sg", { segments: se.allSegments.filter((n) => n !== se.selected), allowNew: false })}
    <div class="modal-actions" style="justify-content:flex-start;margin-top:16px">
      <button class="btn primary" data-act="segSave">${existing ? "Save segment" : "Create segment"}</button></div>
    <div style="font-size:11.5px;color:var(--faint);margin-top:12px;border-top:1px solid var(--line2);padding-top:10px">A clause referencing an absent flag doesn't match — it never errors. Edits propagate in &lt;2s.</div>
  </div>`;
}
function segEditSync() {
  const se = window._segEdit; if (!se) return;
  if (se.creating) { const n = el("sg-name"); if (n) se.name = n.value; }
  cbSync(se.cb, "sg");
}
function renderSegEditor() {
  const e = el("segEditor"); if (e) e.innerHTML = segEditorHtml(window._segEdit);
  const l = el("segList"); if (l) l.innerHTML = segListHtml(window._segEdit);
}

// ── audience tester ──────────────────────────────────────────────────
function audienceResult(r, pid) {
  const matched = typeof r.matched_rule === "string" ? r.matched_rule : `${r.matched_rule.scope}:${r.matched_rule.id}`;
  const matchId = typeof r.matched_rule === "string" ? null : r.matched_rule.id;
  const vinfo = (r.versions && (r.versions[pid] || Object.values(r.versions)[0])) || {};
  let matchLabel = matched;
  if (matchId && _rulesData) {
    const rule = (_rulesData.rules || []).find((x) => x.id === matchId);
    if (rule) matchLabel = rule.comment || matchId;
  }
  return { matched, matchId, matchLabel, version: vinfo.version, commit: vinfo.commit };
}
function audienceCardHtml(pid) {
  const a = window._audience || {};
  const flags = a.flags != null ? a.flags : '{"user_id": "u_12", "tier": "enterprise"}';
  const vars = a.vars != null ? a.vars : "{}";
  const res = a.result;
  let out = "";
  if (a.err) out = `<div class="banner danger" style="margin:12px 0 0"><span style="font-size:12.5px;font-weight:600">${esc(a.err)}</span></div>`;
  else if (res) {
    const isDefault = res.matched === "default";
    out = `<div class="aud-res${isDefault ? " dflt" : ""}">
      <div class="aud-res-line">→ they'd get <b>${res.version != null ? `Version ${res.version}` : "the default"}</b>${res.commit ? ` <span class="mono faint" style="font-size:11px">${esc(res.commit)}</span>` : ""}</div>
      <div class="aud-res-sub">${isDefault ? "No rule matched — they fall through to the environment default." : `Matched rule <b>${esc(res.matchLabel)}</b> (highlighted above)`}</div></div>`;
  }
  const varsField = a.needVars ? `<div class="field" style="margin-top:10px;margin-bottom:0"><label>Variables (JSON) — this prompt needs them to render</label>
      <textarea id="audVars" spellcheck="false" style="min-height:52px">${esc(vars)}</textarea></div>` : "";
  return `<div class="card pad aud-card">
    <div style="font-size:13px;font-weight:700">Check who gets what</div>
    <div style="font-size:11.5px;color:var(--mut);margin:3px 0 10px">Paste a user's flags — see which rule matches and the version they'd get${pid ? ` for <span class="mono">${esc(pid)}</span>` : ""}.</div>
    <div class="field" style="margin-bottom:0"><label>Flags (JSON)</label>
      <textarea id="audFlags" spellcheck="false" style="min-height:52px">${esc(flags)}</textarea></div>
    ${varsField}
    <div style="display:flex;gap:10px;align-items:center;margin-top:10px">
      <button class="btn primary sm" data-act="audCheck" data-pid="${esc(pid || "")}">Check</button>
      <span class="faint" style="font-size:11px">renders as this user through the real serving path</span></div>
    ${out}</div>`;
}
function renderAudience() { const c = el("audienceCard"); if (c) c.innerHTML = audienceCardHtml((window._audience && window._audience.pid) || State.route.pid); }
function renderAudienceHighlight(res) {
  renderAudience();
  document.querySelectorAll(".ord.aud-hit").forEach((e) => e.classList.remove("aud-hit"));
  if (res && res.matchId) {
    let row = null;
    document.querySelectorAll(".ord[data-ruleid]").forEach((e) => { if (e.dataset.ruleid === res.matchId) row = e; });
    if (row) { row.classList.add("aud-hit"); row.scrollIntoView({ behavior: "smooth", block: "center" }); }
  }
}

const SCREENS = {
  prompts: screenPrompts, overview: screenOverview, draft: screenDraft, compare: screenCompare,
  rules: screenRules, pointers: screenPointers, segments: screenSegments,
  play: screenPlay, audit: screenAudit, access: screenAccess,
};

// ── actions ──────────────────────────────────────────────────────────
const Actions = {
  go(ds) { closeModal(); Actions.navClose(); go(ds.hash); },   // navigating dismisses any modal + the drawer
  // Off-canvas nav drawer (mobile). Pure body-class toggles — no re-render needed.
  navToggle() {
    State.navOpen = !State.navOpen;
    if (document.body && document.body.classList) document.body.classList.toggle("nav-open", State.navOpen);
    const h = document.querySelector && document.querySelector(".hamburger");
    if (h && h.setAttribute) h.setAttribute("aria-expanded", State.navOpen ? "true" : "false");
  },
  navClose() {
    State.navOpen = false;
    if (document.body && document.body.classList) document.body.classList.remove("nav-open");
    const h = document.querySelector && document.querySelector(".hamburger");
    if (h && h.setAttribute) h.setAttribute("aria-expanded", "false");
  },
  theme() {
    State.theme = State.theme === "light" ? "dark" : "light";
    localStorage.setItem("incant_theme", State.theme);
    document.body.dataset.theme = State.theme;
    render();
  },
  env(ds, ev) {
    State.env = ev.target.value;
    localStorage.setItem("incant_env", State.env);
    render();
  },
  // Shared by the sign-in card and the "Switch API key…" modal — whichever input is present.
  async setToken() {
    let val = "";
    for (const id of ["signinKey", "switchKeyIn"]) {
      const e = document.getElementById(id);
      if (e && e.value != null && String(e.value).trim()) { val = String(e.value).trim(); break; }
    }
    if (!val) { toast("Enter an API key", true); return; }
    State.token = val;
    localStorage.setItem("incant_token", State.token);
    // Identity changes with the key — drop the cache + failure flag so it re-fetches.
    State.me = null; State._meFailed = false; State._mePromise = null;
    // Re-fetch environments: if boot() ran against a bad key, State.envs is a bare
    // fallback with no `protected` flags — stale until re-read, which would hide
    // PROTECTED badges and skip the type-to-confirm the server still enforces.
    try {
      const e = await GET("/mgmt/envs");
      State.envs = e.environments;
      if (!State.envs.find((x) => x.id === State.env)) State.env = State.envs[0]?.id || "prod";
    } catch (_) { /* key may still be bad — the 401 screen will handle it */ }
    closeModal();
    toast("API key set");
    render();
  },
  acctMenu() {
    const me = State.me;
    if (!me) { openSwitchKeyModal(); return; }   // not signed in → go straight to key entry
    const roles = (me.roles || []).length
      ? me.roles.map((r) => `<div style="font-size:12px;padding:2px 0">
          <b>${esc(r.role)}</b> <span class="faint">· ${esc(_scopeLabel(r))}</span></div>`).join("")
      : `<div class="faint" style="font-size:12px">no roles assigned</div>`;
    openModal(`
      <h3>${esc(me.name)}</h3>
      <p class="hint">You're signed in. Your roles and where each one applies:</p>
      <div style="margin-bottom:14px">${roles}</div>
      <div style="display:flex;flex-direction:column;gap:8px;border-top:1px solid var(--line2);padding-top:12px;align-items:flex-start">
        <button type="button" class="link btn-bare" data-act="switchKey">Switch API key…</button>
        <a href="#/access" data-act="go" data-hash="#/access" style="font-weight:600">Manage access →</a></div>
      <div class="modal-actions"><button class="btn" data-act="closeModal">Close</button></div>`);
  },
  switchKey() { openSwitchKeyModal(); },
  toggleTweak() {
    State.tweakOpen = !State.tweakOpen;
    if (State.tweakOpen) { _tweak.data = null; _tweak.pid = null; _tweak.env = null; }   // refetch on open
    render();
  },
  toggleTech() {
    State.tech = !State.tech;
    localStorage.setItem("incant_tech", State.tech ? "1" : "0");
    render();
  },
  noop() {},
  closeModal() { closeModal(); },
  search(ds, ev) {
    _promptsFilter.q = (ev.target.value || "").trim();
    updatePromptList();   // rebuild only the list container — keeps the search box focused
  },
  promptFilter(ds) {
    _promptsFilter.key = ds.key;
    document.querySelectorAll("#promptFilters .chip").forEach((c) => {
      const on = c.dataset.key === ds.key;
      c.classList.toggle("active", on); c.setAttribute("aria-pressed", String(on));
    });
    updatePromptList();
  },
  newPrompt() {
    openModal(`
      <h3>New prompt</h3>
      <p class="hint">A prompt id is a path: <span class="mono">project/name</span> (e.g.
        <span class="mono">support/refunds</span>). A new leading segment creates a new project.
        Fragments are just prompts — this can be included by any other.</p>
      <div class="field"><label>Prompt id</label>
        <input id="npId" placeholder="support/refunds" spellcheck="false"></div>
      <div class="field"><label>Description <span style="text-transform:none;font-weight:400">(optional)</span></label>
        <textarea id="npDesc" placeholder="What this prompt is for…"></textarea></div>
      <div class="err" id="npErr"></div>
      <div class="modal-actions">
        <button class="btn" data-act="closeModal">Cancel</button>
        <button class="btn primary" data-act="createPrompt">Create &amp; edit v1</button>
      </div>`);
  },
  async createPrompt() {
    const id = (el("npId").value || "").trim().replace(/^\/+|\/+$/g, "");
    const desc = (el("npDesc").value || "").trim();
    const errEl = el("npErr");
    if (!id || !id.includes("/")) {
      errEl.textContent = "Enter a path like project/name (needs at least one “/”).";
      return;
    }
    if (!/^[a-z0-9]([a-z0-9._\/-]*[a-z0-9])?$/i.test(id)) {
      errEl.textContent = "Use letters, numbers, dashes, dots, and “/” only.";
      return;
    }
    errEl.textContent = "";
    try {
      await POST("/mgmt/prompts", { prompt_id: id, description: desc });
      const draft = await POST(`/mgmt/prompts/${enc(id)}/drafts`,
        { version_number: 1, title: "v1", content: "" });
      closeModal();
      toast(`Created ${id} — start writing v1`);
      go(`#/p/${enc(id)}/draft?draft=${draft.id}`);
    } catch (e) {
      if (e.status === 409) errEl.textContent = "A prompt with that id already exists.";
      else if (e.status === 403) errEl.textContent = "You don't have editor access on that project.";
      else errEl.textContent = errText(e);
    }
  },
  async newVersion(ds) {
    const pid = (ds && ds.pid) || State.route.pid;
    try {
      const dv = await GET(`/mgmt/prompts/${enc(pid)}/versions?environment=${enc(State.env)}`);
      const seed = dv.versions.find((x) => x.is_default)?.version || dv.versions[0]?.version;
      const created = await POST(`/mgmt/prompts/${enc(pid)}/drafts`, { seed_from_version: seed, title: "New version" });
      closeModal();   // dismiss the "Create a new version?" explainer if open
      go(`#/p/${enc(pid)}/draft?draft=${created.id}`);
    } catch (e) { toast(errText(e), true); }
  },
  // A muted "＋ New version…" on the overview opens this explainer before creating one.
  newVersionExplain() {
    openModal(`
      <h3>Create a new version?</h3>
      <p class="hint">Most changes belong as edits to the current version — you can test and publish them without a new number. Create a new version when the prompt's meaning changes (different task, different variables).</p>
      <div class="modal-actions">
        <button class="btn" data-act="closeModal">Cancel</button>
        <button class="btn primary" data-act="newVersion">Create new version</button></div>`);
  },
  async edit(ds) {
    go(`#/p/${enc(State.route.pid)}/draft?v=${ds.v}`);
  },
  diffPick() {
    // Read both selects so changing one side preserves the other.
    const aSel = document.querySelector('select[data-side="a"]');
    const bSel = document.querySelector('select[data-side="b"]');
    const mode = State.route.q.mode || "source";
    go(`#/p/${enc(State.route.pid)}/compare?mode=${mode}&a=${enc(aSel.value)}&b=${enc(bSel.value)}`);
  },
  // ── draft page ──────────────────────────────────────────────────
  draftInput() { scheduleAutosave(); },
  async tc(ds) {
    const dp = window._dp;
    const shapeChanges = (dp.tcActive === "__custom") !== (ds.name === "__custom");
    dp.tcActive = ds.name;
    if (shapeChanges) {
      // The custom-JSON block appears/disappears — rebuild the tab without losing edits.
      await flushAutosave();
      const ta = el("draftTa"); if (ta) dp.draft.content = ta.value;
      const host = el("draftTabBody"); if (host) host.innerHTML = draftWriteTab(dp);
    } else {
      document.querySelectorAll(".chip").forEach((c) => {
        const on = c.dataset.name === ds.name;
        c.classList.toggle("active", on); c.setAttribute("aria-pressed", String(on));
      });
    }
    doRenderDraft();
  },
  tcVarsInput(ds, ev) {
    window._dp.customVars = ev.target.value;
    clearTimeout(window._tcTimer); window._tcTimer = setTimeout(doRenderDraft, 500);
  },
  tcFlagsInput(ds, ev) {
    window._dp.customFlags = ev.target.value;
    clearTimeout(window._tcTimer); window._tcTimer = setTimeout(doRenderDraft, 500);
  },
  saveTestContext() {
    const dp = window._dp;
    try { if ((dp.customVars || "").trim()) JSON.parse(dp.customVars); } catch { return toast("Variables: invalid JSON", true); }
    try { if ((dp.customFlags || "").trim()) JSON.parse(dp.customFlags); } catch { return toast("Flags: invalid JSON", true); }
    openModal(`
      <h3>Save test context</h3>
      <p class="hint">Names this set of variables and flags so anyone editing <span class="mono">${esc(dp.draft.prompt_id)}</span> can render with it — here and in validation.</p>
      <div class="field"><label>Name</label>
        <input id="tcName" placeholder="e.g. enterprise-us" spellcheck="false"></div>
      <div class="err" id="tcErr"></div>
      <div class="modal-actions">
        <button class="btn" data-act="closeModal">Cancel</button>
        <button class="btn primary" data-act="saveTestContextConfirm">Save</button></div>`);
  },
  async saveTestContextConfirm() {
    const dp = window._dp;
    const name = (el("tcName").value || "").trim();
    if (!name) { el("tcErr").textContent = "Enter a name."; return; }
    let vars = {}, flags = {};
    try { vars = (dp.customVars || "").trim() ? JSON.parse(dp.customVars) : {}; } catch {}
    try { flags = (dp.customFlags || "").trim() ? JSON.parse(dp.customFlags) : {}; } catch {}
    try {
      await PUT(`/mgmt/prompts/${enc(dp.draft.prompt_id)}/test-contexts`, { name, flags, variables: vars });
      dp.tcs = dp.tcs.filter((t) => t.name !== name).concat([{ name, flags, variables: vars }]);
      dp.tcActive = name;
      closeModal();
      toast(`Saved test context “${name}”`);
      const ta = el("draftTa"); if (ta) dp.draft.content = ta.value;
      const host = el("draftTabBody"); if (host) host.innerHTML = draftWriteTab(dp);
      doRenderDraft();
    } catch (e) { el("tcErr").textContent = errText(e); }
  },
  draftTab(ds) { go(`#/p/${enc(State.route.pid)}/draft?draft=${enc(window._dp.draft.id)}&tab=${ds.tab}`); },
  switchDraft(ds, ev) {
    const v = ev.target.value;
    if (v === "__new" || v === "__discard") {
      ev.target.value = window._dp.draft.id;   // don't leave the menu on an action item
      return v === "__new" ? Actions.newDraftHere() : Actions.discardDraft();
    }
    go(`#/p/${enc(State.route.pid)}/draft?draft=${enc(v)}`);
  },
  async newDraftHere() {
    const pid = State.route.pid, v = window._dp.draft.version_number;
    try {
      const d = await POST(`/mgmt/prompts/${enc(pid)}/drafts`, {
        version_number: v, seed_from_version: v, title: "Draft v" + v });
      go(`#/p/${enc(pid)}/draft?draft=${d.id}`);
    } catch (e) { toast(errText(e), true); }
  },
  discardDraft() {
    const id = window._dp.draft.id;
    openModal(`
      <h3>Discard draft</h3>
      <p class="hint">This closes the draft and drops its uncommitted content. It can't be undone — start a new draft if you change your mind.</p>
      <div class="modal-actions">
        <button class="btn" data-act="closeModal">Cancel</button>
        <button class="btn danger" data-act="discardConfirm" data-id="${esc(id)}">Discard draft</button></div>`);
  },
  async discardConfirm(ds) {
    try {
      await POST(`/mgmt/drafts/${enc(ds.id)}/discard`, {});
      closeModal();
      toast("Draft discarded");
      go(`#/p/${enc(State.route.pid)}/overview`);
    } catch (e) { toast(errText(e), true); }
  },
  diffAgainst(ds, ev) { window._dp.diffAgainst = ev.target.value; loadDraftDiff(); },
  diffMode(ds) { window._dp.diffMode = ds.mode; renderDraftDiffTab(); },
  diffTc(ds) {
    window._dp.diffTc = ds.name;
    document.querySelectorAll(".chip").forEach((c) => {
      const on = c.dataset.name === ds.name;
      c.classList.toggle("active", on); c.setAttribute("aria-pressed", String(on));
    });
    loadDraftDiff();
  },
  async openCommit() {
    await flushAutosave();
    const draft = window._dp.draft;
    openModal(commitModalHtml(draft), "wide");
    try {   // compact source diff vs base, fetched into the modal
      const res = await GET(`/mgmt/drafts/${enc(draft.id)}/diff?mode=source&environment=${enc(State.env)}`);
      const box = el("commitDiffBox");
      if (box) box.innerHTML = res.error ? `<div class="empty">⚠ ${esc(res.error)}</div>`
        : (renderUnifiedDiff(res.diff) || '<div class="empty">No changes vs base.</div>');
    } catch (e) {
      const box = el("commitDiffBox");
      if (box) box.innerHTML = `<div class="empty">⚠ ${esc(errText(e))}</div>`;
    }
  },
  async commitDraft(ds) {
    await flushAutosave();
    const msg = (el("commitMsg") && el("commitMsg").value || "").trim();
    window._dp.pendingMsg = msg;   // stash for a possible force-retry on conflict
    try {
      const r = await POST(`/mgmt/drafts/${enc(ds.id)}/commit`, { message: msg });
      closeModal();
      toast(`Saved to v${r.version_number} — publish when ready`);
      go(`#/p/${enc(State.route.pid)}/overview`);
    } catch (e) {
      const detail = e.data && e.data.detail;
      if (e.status === 409 && detail && typeof detail === "object") openConflictModal(ds.id, detail);
      else if (e.status === 412) goReviewNotice();
      else toast(errText(e), true);
    }
  },
  async commitForce(ds) {
    try {
      const r = await POST(`/mgmt/drafts/${enc(ds.id)}/commit`,
        { message: (window._dp && window._dp.pendingMsg) || "", force: true });
      closeModal();
      toast(`Saved to v${r.version_number} — publish when ready`);
      go(`#/p/${enc(State.route.pid)}/overview`);
    } catch (e) {
      if (e.status === 412) goReviewNotice();
      else toast(errText(e), true);
    }
  },
  async approve(ds) {
    try {
      // The reviewer is the authenticated principal; self-review is a per-project opt-out.
      await POST(`/mgmt/drafts/${ds.draft}/review`, { state: "approved" });
      toast("Approved — commit unlocked");
      render();
    } catch (e) { toast(errText(e), true); }
  },
  async requestChanges(ds) {
    try {
      // A "changes_requested" verdict re-locks commit (withdraws any prior approval).
      await POST(`/mgmt/drafts/${ds.draft}/review`, { state: "changes_requested" });
      toast("Changes requested");
      render();
    } catch (e) { toast(errText(e), true); }
  },
  reviewTc(ds) {
    window._dp.reviewTc = ds.name;
    document.querySelectorAll("#reviewTcChips .chip").forEach((c) => {
      const on = c.dataset.name === ds.name;
      c.classList.toggle("active", on); c.setAttribute("aria-pressed", String(on));
    });
    loadReviewRendered();
  },
  reviewSrcToggle() {
    const dp = window._dp; if (!dp) return;
    dp.reviewSrcOpen = !dp.reviewSrcOpen;
    const host = el("reviewSrcSection"); if (host) host.innerHTML = reviewSrcSectionInner(dp);
    if (dp.reviewSrcOpen) loadReviewSrc();
  },
  async postComment() {
    const ta = el("reviewCommentBody");
    const body = ((ta && ta.value) || "").trim();
    if (!body) return;
    try {
      await POST(`/mgmt/drafts/${enc(window._dp.draft.id)}/comments`, { body });
      if (ta) ta.value = "";
      const btn = el("reviewCommentBtn"); if (btn) btn.disabled = true;
      await loadReviewComments();   // refresh the thread only — don't re-render the page
      toast("Comment added");
    } catch (e) { toast(errText(e), true); }
  },
  async ruleStatus(ds) {
    try { await PATCH(`/mgmt/envs/${enc(State.env)}/rules/${ds.id}`, { status: ds.status }); toast(`Rule ${ds.id} → ${ds.status}`); render(); }
    catch (e) { toast(errText(e), true); }
  },
  rollback(ds) {
    const body = `Restore <b>${esc(State.env)}</b>'s rules to this earlier point
      <span class="mono faint">rv${esc(ds.rv)}</span>. Rules created after that point stop
      serving. This is itself a change, so history is never rewritten.`;
    // A locked env asks you to type the env name; rollback is env-scoped.
    if (isLocked()) {
      openModal(typeToConfirm({
        title: "Go back to earlier targeting", body, token: State.env,
        confirmLabel: "Go back", act: "rollbackConfirm", data: { rv: ds.rv },
      }));
      return;
    }
    openModal(`
      <h3>Go back to earlier targeting</h3>
      <p class="hint">${body}</p>
      <div class="modal-actions">
        <button class="btn" data-act="closeModal">Cancel</button>
        <button class="btn primary" data-act="rollbackConfirm" data-rv="${esc(ds.rv)}">Go back</button>
      </div>`);
  },
  async rollbackConfirm(ds) {
    try {
      const r = await POST(`/mgmt/envs/${enc(State.env)}/rollback`,
                           { to_rules_version: parseInt(ds.rv), confirm: State.env });
      closeModal();
      toast(`Went back — ${r.rules_changed} rule(s) changed`);
      render();
    } catch (e) { toast(errText(e), true); }
  },
  async kill(ds) {
    const engage = ds.engage === "true";
    try {
      await POST(`/mgmt/envs/${enc(State.env)}/kill?prompt_id=${enc(ds.pid)}`, { engaged: engage });
      closeModal();   // no-op unless invoked from the "turn targeting off" modal
      toast(engage ? "Targeting turned off — everyone gets the default" : "Targeting back on — rules apply again");
      render();
    } catch (e) { toast(errText(e), true); }
  },
  // The header targeting toggle: turning ON (restoring rules) is instant; turning OFF
  // is destructive, so it routes through the confirmation modal.
  targetingToggle(ds) {
    if (ds.engaged === "true") Actions.kill({ pid: ds.pid, engage: "false" });
    else openTargetingOffModal(ds.pid);
  },
  // Publishing (unilateral, releaser) and reverting both open the impact modal, which
  // previews the diff + affected audience and embeds the locked-env type-to-confirm.
  makeLive(ds) { openPublishModal({ v: parseInt(ds.v), toSha: ds.sha, toShort: ds.short, mode: "publish" }); },
  revert(ds) { openPublishModal({ v: parseInt(ds.v), toSha: ds.sha, toShort: ds.short, mode: "revert" }); },
  publishTc(ds) {
    const p = window._publish; if (!p) return;
    p.tc = ds.name;
    document.querySelectorAll("#publishTcChips .chip").forEach((c) => {
      const on = c.dataset.name === ds.name;
      c.classList.toggle("active", on); c.setAttribute("aria-pressed", String(on));
    });
    const lbl = el("publishDiffLabel"); if (lbl) lbl.innerHTML = "· " + publishDiffLabelText(p);
    loadPublishDiff();
  },
  publishRuleToggle(ds, ev) {
    const p = window._publish; if (!p) return;
    p.removeRule[ds.id] = ev.target.checked;
  },
  async publishConfirm() {
    const p = window._publish; if (!p) return;
    const toRemove = p.tipRules.filter((r) => p.removeRule[r.id]);
    try {
      await POST(`/mgmt/envs/${enc(p.env)}/pointers`, {
        prompt_id: p.pid, version_number: parseInt(p.v), to_sha: p.toSha,
        comment: p.mode === "revert" ? "Reverted to an earlier state via the UI" : "Published via the UI",
        confirm: p.pid,
      });
      let removed = 0;
      for (const r of toRemove) { await PATCH(`/mgmt/envs/${enc(p.env)}/rules/${r.id}`, { status: "archived" }); removed++; }
      closeModal();
      const base = p.mode === "revert" ? `Done — an earlier state of v${p.v} is live again` : `Published — v${p.v} live for everyone`;
      toast(base + (removed ? ` · ${removed} test ${plural(removed, "rule")} removed` : ""));
      render();
    } catch (e) { toast(errText(e), true); }
  },
  async play() {
    window._playPid = el("playPid").value.trim();
    window._playFlags = el("playFlags").value;
    window._playVars = el("playVars").value;
    let flags, vars;
    try { flags = window._playFlags.trim() ? JSON.parse(window._playFlags) : {}; }
    catch { return toast("Flags: invalid JSON", true); }
    try { vars = window._playVars.trim() ? JSON.parse(window._playVars) : {}; }
    catch { return toast("Variables: invalid JSON", true); }
    const body = { flags, variables: vars, environment: State.env };
    if (window._playPin) body.pin = window._playPin;
    try {
      window._playLast = await POST(`/prompt/${enc(window._playPid)}`, body);
      render();
    } catch (e) { toast(errText(e), true); }
  },
  pinLast() {
    const r = window._playLast;
    if (!r) return;
    window._playPin = { versions: r.versions, rules_version: r.rules_version };
    toast("Pinned — renders now reproduce this exact result");
    Actions.play();
  },
  unpin() { window._playPin = null; toast("Pin cleared"); render(); },
  async toggleSelfReview(ds) {
    const to = ds.to === "true";
    try {
      await PATCH(`/mgmt/projects/${enc(ds.project)}`, { allow_self_review: to });
      toast(to ? "Self-review allowed for " + ds.project
               : "Distinct reviewer now required for " + ds.project);
      render();
    } catch (e) { toast(errText(e), true); }
  },
  // ── project settings modal (governance) ──────────────────────────
  projectSettings(ds) { openProjectSettings(ds.project, ds.prompt); },
  async setSelfReview(ds) {
    const ps = window._projSettings; if (!ps) return;
    const to = ds.to === "true";
    try {
      const r = await PATCH(`/mgmt/projects/${enc(ps.project)}`, { allow_self_review: to });
      ps.selfOk = r.allow_self_review; ps.canEdit = true; ps.err = "";
      toast(to ? "Anyone can approve their own edits" : "A different person must approve now");
      renderProjectSettings();
    } catch (e) {
      if (e && e.status === 403) { ps.canEdit = false; ps.err = "Only admins can change this."; renderProjectSettings(); }
      else toast(errText(e), true);
    }
  },
  // ── audit exploration ────────────────────────────────────────────
  auditDetail(ds) { const a = (_auditRows || []).find((x) => String(x.id) === String(ds.id)); if (a) openAuditDetail(a); },
  auditActor(ds, ev) { _auditFilter.actor = ev.target.value; render(); },
  auditAction(ds, ev) { _auditFilter.action = ev.target.value; render(); },
  auditObject(ds, ev) { _auditFilter.object = (ev.target.value || "").trim(); render(); },   // change event → re-fetch
  auditClear() { _auditFilter = { actor: "", action: "", object: "" }; render(); },
  // ── access / users ──────────────────────────────────────────────
  newUser() {
    openModal(`
      <h3>New user</h3>
      <p class="hint">Creates a principal with an initial role and issues its first API key.</p>
      <div class="field"><label>Name</label>
        <input id="auName" placeholder="e.g. dana or ci-deploy" spellcheck="false"></div>
      <div class="field"><label>Role</label><select id="auRole" class="envsel" style="width:100%">${_roleOpts("editor")}</select></div>
      <div class="field"><label>Project scope</label><select id="auProject" class="envsel" style="width:100%">${_projectOpts()}</select></div>
      <div class="field"><label>Environment scope</label><select id="auEnv" class="envsel" style="width:100%">${_envOpts()}</select></div>
      <div class="err" id="auErr"></div>
      <div class="modal-actions">
        <button class="btn" data-act="closeModal">Cancel</button>
        <button class="btn primary" data-act="createUser">Create &amp; issue key</button>
      </div>`);
  },
  async createUser() {
    const name = (el("auName").value || "").trim();
    if (!name) { el("auErr").textContent = "Enter a name."; return; }
    try {
      const r = await POST(`/mgmt/keys`, {
        principal_name: name, role: el("auRole").value,
        project_id: el("auProject").value || null, environment_id: el("auEnv").value || null,
      });
      _showKeyModal(r.key);
      render();
    } catch (e) { el("auErr").textContent = errText(e); }
  },
  addBinding(ds) {
    openModal(`
      <h3>Add role</h3>
      <p class="hint">Grant another role to this user, optionally scoped to a project and/or environment.</p>
      <div class="field"><label>Role</label><select id="abRole" class="envsel" style="width:100%">${_roleOpts("viewer")}</select></div>
      <div class="field"><label>Project scope</label><select id="abProject" class="envsel" style="width:100%">${_projectOpts()}</select></div>
      <div class="field"><label>Environment scope</label><select id="abEnv" class="envsel" style="width:100%">${_envOpts()}</select></div>
      <div class="modal-actions">
        <button class="btn" data-act="closeModal">Cancel</button>
        <button class="btn primary" data-act="addBindingConfirm" data-pid="${esc(ds.pid)}">Add role</button>
      </div>`);
  },
  async addBindingConfirm(ds) {
    try {
      await POST(`/mgmt/principals/${enc(ds.pid)}/bindings`, {
        role: el("abRole").value,
        project_id: el("abProject").value || null, environment_id: el("abEnv").value || null,
      });
      closeModal();
      toast("Role added");
      render();
    } catch (e) { toast(errText(e), true); }
  },
  async removeBinding(ds) {
    try {
      await api("DELETE", `/mgmt/principals/${enc(ds.pid)}/bindings/${ds.bid}`);
      toast("Role removed");
      render();
    } catch (e) { toast(errText(e), true); }
  },
  async issueKey(ds) {
    try {
      const r = await POST(`/mgmt/principals/${enc(ds.pid)}/keys`, {});
      _showKeyModal(r.key);
      render();
    } catch (e) { toast(errText(e), true); }
  },
  revokeKey(ds) {
    openModal(`
      <h3>Revoke key</h3>
      <p class="hint">This key stops authenticating immediately. This can't be undone (issue a new key instead).</p>
      <div class="modal-actions">
        <button class="btn" data-act="closeModal">Cancel</button>
        <button class="btn danger" data-act="revokeKeyConfirm" data-kid="${esc(ds.kid)}">Revoke</button>
      </div>`);
  },
  async revokeKeyConfirm(ds) {
    try {
      await POST(`/mgmt/keys/${enc(ds.kid)}/revoke`, {});
      closeModal();
      toast("Key revoked");
      render();
    } catch (e) { toast(errText(e), true); }
  },
  async toggleReq(ds) {
    try {
      const newReq = !(ds.req === "true");
      await PUT(`/mgmt/prompts/${enc(State.route.pid)}/variables?version=${ds.v}`, { name: ds.name, required: newReq });
      toast(`${ds.name} → ${newReq ? "required" : "optional"}`);
      render();
    } catch (e) { toast(errText(e), true); }
  },

  // ── clause builder (composer + segments editor + inline new-segment) ──
  cbAdd(ds) { const { cb, host } = cbResolve(ds.prefix); if (!cb) return; cbHostSync(host); cb.rows.push(emptyFlagRow()); cbHostRender(host); },
  cbDel(ds) { const { cb, host } = cbResolve(ds.prefix); if (!cb) return; cbHostSync(host); cb.rows.splice(+ds.ri, 1); cbHostRender(host); },
  cbKind(ds) {
    const { cb, host } = cbResolve(ds.prefix); if (!cb) return; cbHostSync(host);
    const row = cb.rows[+ds.ri];
    if (row.kind === "flag" && !row.op) row.op = "eq";
    cbHostRender(host);
  },
  cbOp(ds) { const { cb, host } = cbResolve(ds.prefix); if (!cb) return; cbHostSync(host); cbHostRender(host); },
  cbSeg(ds) {
    const { cb, host } = cbResolve(ds.prefix); if (!cb) return; cbHostSync(host);
    const row = cb.rows[+ds.ri];
    if (row.segment === "__new" && !row.newSeg) row.newSeg = { name: "", cb: newCb(null) };
    cbHostRender(host);
  },
  cbAdvanced(ds) {
    const { cb, host } = cbResolve(ds.prefix); if (!cb) return; cbHostSync(host);
    cb.advancedJson = JSON.stringify(rowsToWhen(cb.rows), null, 2);
    cb.advanced = true; cbHostRender(host);
  },
  cbSimple(ds) {
    const { cb, host } = cbResolve(ds.prefix); if (!cb) return; cbHostSync(host);
    let parsed;
    try { parsed = cb.advancedJson.trim() ? JSON.parse(cb.advancedJson) : null; }
    catch (e) { return toast("Invalid JSON — can't switch to the simple builder", true); }
    const rows = whenToRows(parsed);
    if (rows == null) return toast("This condition is too complex for the simple builder — keep editing the JSON", true);
    cb.rows = rows; cb.advanced = false; cbHostRender(host);
  },

  // ── rule composer ────────────────────────────────────────────────
  ruleNew() { openComposer({ pid: State.route.pid || ((_rulesData && _rulesData.rules.find((r) => r.prompt_id)) || {}).prompt_id }); },
  ruleEdit(ds) { const r = (_rulesData && _rulesData.rules || []).find((x) => x.id === ds.id); if (r) openComposer({ rule: r }); },
  ruleDup(ds) { const r = (_rulesData && _rulesData.rules || []).find((x) => x.id === ds.id); if (r) openComposer({ rule: r, duplicate: true }); },
  cbServeMode(ds) {
    composerSync(); const co = window._composer; co.serveMode = ds.mode;
    if (ds.mode === "rollout" && !co.rollout.weights.length)
      co.rollout.weights.push({ version: co.version != null ? co.version : "", weight: 50, label: "" });
    renderComposer();
  },
  cbTip() { composerSync(); renderComposer(); },
  cbVersion() { composerSync(); },
  cbPos() { composerSync(); renderComposer(); },
  cbScope() {
    composerSync(); const co = window._composer;
    co.prompt_id = co.scope === "global" ? null : co.routePid;
    renderComposer();
  },
  cbRoAdd() { composerSync(); window._composer.rollout.weights.push({ version: "", weight: 0, label: "" }); renderComposer(); },
  cbRoDel(ds) { composerSync(); window._composer.rollout.weights.splice(+ds.ri, 1); renderComposer(); },
  cbRoWeight() { composerSync(); },
  rolloutWeightInput() {
    const co = window._composer; if (!co) return;
    let sum = 0;
    co.rollout.weights.forEach((w, i) => { const rw = el(`co-rw${i}`); if (rw) sum += Number(rw.value) || 0; });
    const rdef = el("co-rdef"); if (rdef) sum += Number(rdef.value) || 0;
    const s = el("rolloutSum"); if (s) { s.textContent = `sum: ${sum}%`; s.className = "ro-sum " + (sum === 100 ? "ok" : "bad"); }
  },
  async composerSave() {
    composerSync();
    const co = window._composer; co.err = "";
    let when;
    try {
      if (co.cb.advanced) when = co.cb.advancedJson.trim() ? JSON.parse(co.cb.advancedJson) : null;
      else { await composerCreateNewSegments(co); when = rowsToWhen(co.cb.rows); }
    } catch (e) { co.err = typeof e === "string" ? e : "Condition JSON is invalid."; return renderComposer(); }
    let serve;
    try { serve = composerServe(co); } catch (e) { co.err = typeof e === "string" ? e : String(e); return renderComposer(); }
    const comment = (co.comment || "").trim();
    const id = co.editing ? co.origId : slugId(comment);
    const active = activeOrderedRules(co.rules, co.editing ? co.origId : null);
    const { priority, plan } = placePriority(active, Math.min(co.slot, active.length));
    const base = { id, scope: co.scope, priority, when, serve, status: co.status || "active", comment };
    if (co.scope === "prompt") base.prompt_id = co.prompt_id || co.routePid;
    try {
      for (const p of plan) await POST(`/mgmt/envs/${enc(State.env)}/rules`, ruleUpsertBody(p.rule, p.priority));
      await POST(`/mgmt/envs/${enc(State.env)}/rules`, base);
      closeModal();
      toast(co.editing ? "Rule saved" : "Rule created");
      render();
    } catch (e) { co.err = errText(e); renderComposer(); }
  },

  // ── reorder ↑/↓ (priority swap) ──────────────────────────────────
  async ruleMove(ds) {
    const payloads = swapPriorityPayloads((_rulesData && _rulesData.rules) || [], ds.id, ds.dir);
    if (!payloads) return;
    try {
      await POST(`/mgmt/envs/${enc(State.env)}/rules`, payloads[0]);
      await POST(`/mgmt/envs/${enc(State.env)}/rules`, payloads[1]);
      toast("Reordered");
      render();
    } catch (e) { toast(errText(e), true); }
  },

  // ── stop test & publish (coordinated pointer move + rule archive) ──
  async stopTestPublish(ds) {
    const rule = ((_rulesData && _rulesData.rules) || []).find((r) => r.id === ds.id);
    if (!rule) return;
    const pid = rule.prompt_id, t = serveTarget(rule.serve);
    let dv;
    try { dv = await GET(`/mgmt/prompts/${enc(pid)}/versions?environment=${enc(State.env)}`); }
    catch (e) { return toast(errText(e), true); }
    const vrow = dv.versions.find((v) => v.version === t.version) || {};
    const sha = vrow.tip_full_sha;
    if (!sha) return toast("No unpublished edits to publish for this version", true);
    const body = `Publish <b>v${t.version}</b>'s latest edits to <b>${esc(pid)}</b> for everyone, then remove this test rule so it stops targeting a group.`;
    if (isLocked()) {
      openModal(typeToConfirm({
        title: "Stop test & publish", body: body + ` <b>${esc(State.env)}</b> is locked.`,
        token: pid, confirmLabel: "Publish & stop test", act: "stopTestConfirm",
        data: { id: rule.id, pid, v: t.version, sha },
      }));
      return;
    }
    openModal(`
      <h3>Stop test &amp; publish</h3>
      <p class="hint">${body}</p>
      <div style="display:flex;gap:10px;margin:0 0 6px;padding:12px 14px;background:var(--acc-soft2);border-radius:10px;font-size:12.5px;color:var(--mut);line-height:1.6">Two steps, in order: publish v${t.version}'s latest edits (advance the live pointer), then archive this test rule. Everyone gets the same content — the group test is no longer needed.</div>
      <div class="modal-actions">
        <button class="btn" data-act="closeModal">Cancel</button>
        <button class="btn primary" data-act="stopTestConfirm" data-id="${esc(rule.id)}" data-pid="${esc(pid)}" data-v="${t.version}" data-sha="${esc(sha)}">Publish &amp; stop test</button></div>`);
  },
  async stopTestConfirm(ds) {
    try {
      await POST(`/mgmt/envs/${enc(State.env)}/pointers`, {
        prompt_id: ds.pid, version_number: parseInt(ds.v), to_sha: ds.sha,
        comment: "Published via stop-test-and-publish", confirm: ds.pid,
      });
      await PATCH(`/mgmt/envs/${enc(State.env)}/rules/${ds.id}`, { status: "archived" });
      closeModal();
      toast("Published — test rule removed");
      render();
    } catch (e) { toast(errText(e), true); }
  },

  // ── audience tester ──────────────────────────────────────────────
  async audCheck(ds) {
    const pid = ds.pid || State.route.pid || null;
    if (!pid) return toast("Open a prompt's rules to test its audience", true);
    const a = window._audience = window._audience || {};
    a.pid = pid;
    const fEl = el("audFlags"); if (fEl) a.flags = fEl.value;
    const vEl = el("audVars"); if (vEl) a.vars = vEl.value;
    let flags, vars = {};
    try { flags = (a.flags || "").trim() ? JSON.parse(a.flags) : {}; }
    catch { a.err = "Flags: invalid JSON"; a.result = null; return renderAudience(); }
    if (a.needVars) { try { vars = (a.vars || "").trim() ? JSON.parse(a.vars) : {}; } catch { a.err = "Variables: invalid JSON"; return renderAudience(); } }
    a.err = "";
    try {
      const r = await POST(`/prompt/${enc(pid)}`, { flags, variables: vars, environment: State.env });
      a.result = audienceResult(r, pid); a.needVars = false;
      renderAudienceHighlight(a.result);
    } catch (e) {
      if (e.status === 422) { a.needVars = true; a.result = null; a.err = "This prompt needs variables to render — add minimal variables JSON and check again."; renderAudience(); }
      else { a.err = errText(e); a.result = null; renderAudience(); }
    }
  },

  // ── segments editor ──────────────────────────────────────────────
  segSelect(ds) {
    const se = window._segEdit; const s = se.raw.find((x) => x.name === ds.name); if (!s) return;
    se.creating = false; se.selected = s.name; se.name = s.name; se.cb = newCb(s.when); se.err = "";
    renderSegEditor();
  },
  segNew() {
    const se = window._segEdit;
    se.creating = true; se.selected = null; se.name = ""; se.cb = newCb(null); se.err = "";
    renderSegEditor();
  },
  async segSave() {
    const se = window._segEdit; segEditSync(); se.err = "";
    const name = (se.creating ? se.name : se.selected || "").trim();
    if (!name) { se.err = "Name the segment."; return renderSegEditor(); }
    let when;
    try { when = se.cb.advanced ? (se.cb.advancedJson.trim() ? JSON.parse(se.cb.advancedJson) : null) : rowsToWhen(se.cb.rows); }
    catch (e) { se.err = "Condition JSON is invalid."; return renderSegEditor(); }
    try {
      await POST(`/mgmt/envs/${enc(State.env)}/segments`, segmentPayload(name, when));
      toast(`Saved segment “${name}”`);
      se.selected = name; se.creating = false; se.name = name;
      render();
    } catch (e) { se.err = errText(e); renderSegEditor(); }
  },
};

// ── render + wire ────────────────────────────────────────────────────
function render() {
  if (Auto.timer) fireAutosave();   // flush a pending autosave before the DOM is replaced
  document.body.dataset.theme = State.theme;
  if (document.body && document.body.classList) document.body.classList.toggle("nav-open", State.navOpen);
  State.route = parseRoute();
  el("app").innerHTML = shell(`<div class="empty">Loading…</div>`);
  ensureWhoami();   // lazy whoami → fills the account chip in place
  const fn = SCREENS[State.route.name] || screenPrompts;
  fn().catch((e) => {
    if (e && e.status === 401) {
      // Dedicated sign-in card replaces the screen body; the sidebar stays.
      el("main").innerHTML = signInCard();
    } else {
      el("main").innerHTML = `<div class="empty">⚠ ${esc(errText(e))}</div>`;
    }
  });
}

document.addEventListener("click", (ev) => {
  const t = ev.target.closest("[data-act]");
  if (!t) return;
  const act = t.dataset.act;
  if (Actions[act]) { ev.preventDefault(); Actions[act](t.dataset, ev); }
});
document.addEventListener("change", (ev) => {
  const t = ev.target.closest("[data-act]");
  if (t && Actions[t.dataset.act]) Actions[t.dataset.act](t.dataset, ev);
});
document.addEventListener("input", (ev) => {
  const t = ev.target.closest("[data-act]");
  if (!t) return;
  const act = t.dataset.act;
  if (act === "search" || act === "draftInput" || act === "tcVarsInput" || act === "tcFlagsInput" || act === "rolloutWeightInput") Actions[act](t.dataset, ev);
});
document.addEventListener("keydown", (ev) => {
  if (ev.key === "Escape") { closeModal(); Actions.navClose(); }
});
window.addEventListener("hashchange", render);

async function boot() {
  document.body.dataset.theme = State.theme;
  try {
    const e = await GET("/mgmt/envs");
    State.envs = e.environments;
    if (!State.envs.find((x) => x.id === State.env)) State.env = State.envs[0]?.id || "prod";
  } catch (_) { State.envs = [{ id: State.env }]; }
  render();
}
boot();
