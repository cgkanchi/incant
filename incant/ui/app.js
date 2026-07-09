/* Incant UI — single-page app over the mgmt + serving APIs. Vanilla JS, no build. */
"use strict";

const State = {
  token: localStorage.getItem("incant_token") || "incant_sk_dev_admin",
  env: localStorage.getItem("incant_env") || "prod",
  theme: localStorage.getItem("incant_theme") || "light",
  envs: [],
  me: null,               // cached GET /mgmt/whoami — cleared when the key changes
  tweakOpen: false,
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
  if (!t) { t = document.createElement("div"); t.id = "toast"; document.body.appendChild(t); }
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
function openModal(html, cls) {
  closeModal();
  const o = document.createElement("div");
  o.id = "modal";
  o.className = "modal-overlay";
  o.innerHTML = `<div class="modal ${cls || ""}" data-act="noop">${html}</div>`;
  document.body.appendChild(o);
  const first = o.querySelector("input, textarea");
  if (first) first.focus();
}
function closeModal() {
  const m = el("modal");
  if (m) m.remove();
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
    ["overview", "Overview", "◈"], ["draft", "Draft", "✎"], ["compare", "Compare", "⇄"],
    ["rules", "Targeting", "◐"], ["pointers", "Pointers", "▸"],
  ];
  const cur = State.route.name;
  const head = `<div class="subnav ${cur === "overview" ? "active" : ""}" data-act="go" data-hash="#/p/${enc(pid)}/overview">
    <span style="color:var(--acc-ink)">↳</span>${esc(pid)}</div>`;
  const rows = items.map(([id, label, gl]) =>
    `<div class="subnav ${cur === id ? "active" : ""}" data-act="go" data-hash="#/p/${enc(pid)}/${id}">
      <span class="gl">${gl}</span><span>${label}</span></div>`).join("");
  return head + rows;
}

function sidebar() {
  const pid = State.route.pid;
  const envOpts = State.envs.map((e) =>
    `<option value="${esc(e.id)}" ${e.id === State.env ? "selected" : ""}>${esc(e.id)}</option>`).join("");
  const curEnv = State.envs.find((e) => e.id === State.env) || {};
  return `<div class="sidebar">
    <div class="brand">
      <span class="star">✦</span><span class="name">Incant</span><div class="grow"></div>
      <button class="theme-btn" data-act="theme">${State.theme === "light" ? "☾" : "☀"}</button>
    </div>
    <div class="sect">LIBRARY</div>
    <div class="nav ${State.route.name === "prompts" ? "active" : ""}" data-act="go" data-hash="#/prompts">
      <span class="gl">◈</span><span>Prompts</span></div>
    ${subnav(pid)}
    <div class="nav ${State.route.name === "segments" ? "active" : ""}" data-act="go" data-hash="#/segments">
      <span class="gl">⬡</span><span>Segments</span></div>
    <div class="nav ${State.route.name === "play" ? "active" : ""}" data-act="go" data-hash="#/play">
      <span class="gl">▶</span><span>Playground</span></div>
    <div class="nav ${State.route.name === "audit" ? "active" : ""}" data-act="go" data-hash="#/audit">
      <span class="gl">◷</span><span>Audit</span></div>
    <div class="nav ${State.route.name === "access" ? "active" : ""}" data-act="go" data-hash="#/access">
      <span class="gl">⚿</span><span>Access</span></div>
    <div class="spacer"></div>
    ${pid ? `<button class="tweak-btn" data-act="toggleTweak"><span>✦</span> Tweak flow</button>` : ""}
    <div class="envbar">
      <span class="pill faint" style="font-size:9.5px;letter-spacing:.06em">ENV</span>
      <select class="envsel" data-act="env">${envOpts}</select>
      ${curEnv.protected ? '<span class="pill warn">PROTECTED</span>' : ""}
      ${curEnv.track_tip ? '<span class="pill live">track-tip</span>' : ""}
    </div>
    <div class="tokenbar">
      <span>key</span><input id="tokenIn" value="${esc(State.token)}" spellcheck="false">
      <span class="link" data-act="setToken">set</span>
    </div>
  </div>`;
}

function shell(mainHtml) {
  return `<div class="shell">${sidebar()}
    <div class="main" id="main">${mainHtml}</div>
    ${State.tweakOpen && State.route.pid ? tweakPanel() : ""}
  </div>`;
}

function tweakPanel() {
  const pid = State.route.pid;
  // Targets may carry a query string; the hash is built by plain concatenation below.
  const steps = [
    ["Edit", "Draft on the version — commit lands, serving unchanged", "draft"],
    ["Commit", "Validated + review passed", "draft?tab=review"],
    ["Target a segment", "Rule: cohort → version @ tip", "rules"],
    ["Verify", "Render test contexts + diff against live", "draft?tab=diff"],
    ["Make live", "Advance the pointer, delete the rule", "pointers"],
  ];
  const rows = steps.map(([t, s, target], i) =>
    `<div class="tstep" data-act="go" data-hash="#/p/${enc(pid)}/${target}">
      <span class="tnum">${i + 1}</span>
      <span style="flex:1"><span style="font-size:12px;font-weight:600;display:block">${t}</span>
      <span style="font-size:10.5px;color:var(--mut)">${s}</span></span></div>`).join("");
  return `<div class="tweakpanel">
    <div style="display:flex;align-items:center;gap:8px;margin-bottom:6px">
      <span style="color:var(--acc-ink)">✦</span>
      <span class="serif" style="font-style:italic;font-size:19px">The tweak flow</span>
      <span class="link" style="margin-left:auto" data-act="toggleTweak">✕</span></div>
    <div style="font-size:11.5px;color:var(--mut);margin-bottom:18px">Iterate on a live version without minting a new one. Commits change nothing; the pointer move at the end is the governed act.</div>
    ${rows}
    <div style="border-top:1px solid var(--line2);margin-top:14px;padding-top:12px;font-size:10.5px;color:var(--faint)">The tip↔live gap <i>is</i> the testing window. Delete the cohort rule after making live.</div>
  </div>`;
}

// ── screens ──────────────────────────────────────────────────────────
async function screenPrompts() {
  const data = await GET(`/mgmt/overview?environment=${enc(State.env)}`);
  let html = `<div class="screen">
    <div class="h1row"><span class="h1">Prompts</span>
      <span class="sub">${data.projects.length} projects · env ${esc(State.env)} · rules_version ${data.rules_version}</span>
      <div class="grow"></div>
      <input class="search" id="promptSearch" placeholder="Search prompts…" data-act="search" spellcheck="false">
      <button class="btn primary" data-act="newPrompt">New prompt</button></div>`;
  for (const proj of data.projects) {
    html += `<div class="groupname">${esc(proj.project.toUpperCase())}</div>
      <div class="card" style="margin-bottom:20px"><table class="grid">
      <thead class="ghead"><tr><th>Prompt</th><th>Versions</th><th>Live · ${esc(State.env)}</th><th>Status</th><th>Updated</th></tr></thead><tbody>`;
    for (const p of proj.prompts) {
      const live = p.live_version
        ? `<span class="pill live">v${p.live_version} ● live</span>` : `<span class="faint">—</span>`;
      const status = p.tip_ahead > 0
        ? `<span class="pill warn">tip ahead +${p.tip_ahead}</span>` : `<span class="faint">tip = live</span>`;
      const upd = p.updated ? `${ago(p.updated.when)} · ${esc(p.updated.who)}` : "";
      html += `<tr class="grow-row click prow" data-pid="${esc(p.prompt_id)}" data-act="go" data-hash="#/p/${enc(p.prompt_id)}/overview">
        <td><span class="pid">${esc(p.prompt_id)}</span></td>
        <td>${p.versions}</td><td>${live}</td><td>${status}</td>
        <td class="muted">${upd}</td></tr>`;
    }
    html += `</tbody></table></div>`;
  }
  html += `<div style="font-size:11px;color:var(--faint)">Fragments are just prompts — anything here can be included by anything else, resolved through targeting.</div></div>`;
  el("main").innerHTML = html;
}

async function screenOverview() {
  const pid = State.route.pid;
  const d = await GET(`/mgmt/prompts/${enc(pid)}/versions?environment=${enc(State.env)}`);
  const rows = d.versions.map((v) => {
    const badges = [];
    if (v.label) badges.push(`<span class="pill acc">${esc(v.label)}</span>`);
    if (v.live_sha) badges.push(`<span class="pill live">● Live in ${esc(State.env)}</span>`);
    if (v.tip_ahead > 0) badges.push(`<span class="pill warn">Tip ahead +${v.tip_ahead}</span>`);
    if (v.status === "archived") badges.push(`<span class="faint">Archived — pointers keep serving</span>`);
    const actions = [`<span class="link" data-act="edit" data-v="${v.version}">Edit</span>`];
    if (v.tip_ahead > 0) actions.push(`<span class="link" data-act="go" data-hash="#/p/${enc(pid)}/pointers?v=${v.version}">✦ Make live</span>`);
    actions.push(`<span class="link" data-act="go" data-hash="#/p/${enc(pid)}/rules">Target</span>`);
    return `<tr class="grow-row"><td><span class="vnum">v${v.version}</span></td>
      <td><div style="display:flex;gap:6px;flex-wrap:wrap;align-items:center">${badges.join("") || '<span class="faint">—</span>'}</div></td>
      <td class="mono faint">${v.live_sha || "—"}${v.live_at ? ' <span class="faint">· ' + ago(v.live_at) + "</span>" : ""}</td>
      <td class="mono">${v.tip_sha || "—"} <span class="faint">${v.tip_author ? "· " + ago(v.tip_when) + " · " + esc(v.tip_author) : ""}</span></td>
      <td class="right">${actions.join(' <span class="faint">·</span> ')}</td></tr>`;
  }).join("");

  const vars = d.variables.map((vr) => {
    const cls = vr.required ? "req" : "opt";
    const over = vr.overridden ? " over" : "";
    return `<div class="kv"><span class="varname">${esc(vr.name)}</span><span class="muted">${esc(vr.type)}</span>
      <span class="reqtag ${cls}${over}" data-act="toggleReq" data-name="${esc(vr.name)}" data-v="${d.versions.find(x=>x.is_default)?.version||""}" data-req="${vr.required}">${vr.required ? "required" : "optional"}${vr.overridden ? " ·" : ""}</span></div>`;
  }).join("") || '<div class="faint">No variables.</div>';

  const includes = d.includes.length
    ? d.includes.map((i) => `<div style="display:flex;gap:8px;align-items:center"><span style="color:var(--acc-ink)">↳</span><span class="mono" style="font-size:11px">${esc(i)}</span></div>`).join("")
    : '<div class="faint">No includes.</div>';

  el("main").innerHTML = `<div class="screen">
    <div class="crumb"><a data-act="go" data-hash="#/prompts">Prompts</a> / ${esc(pid.split("/")[0])} /</div>
    <div class="h1row"><span class="h1">${esc(pid)}</span>
      <span class="sub">${d.versions.length} versions · env ${esc(State.env)}</span>
      <div class="grow"></div>
      <button class="btn primary" data-act="newVersion">New version</button></div>
    <div class="panelrow">
      <div class="card" style="flex:10 1 600px;min-width:0;overflow-x:auto"><table class="grid">
        <thead class="ghead"><tr><th>Version</th><th>Status</th><th>Live · ${esc(State.env)}</th><th>Tip</th><th></th></tr></thead>
        <tbody>${rows}</tbody></table></div>
      <div style="flex:1 1 272px;display:flex;flex-direction:column;gap:14px">
        <div class="card pad">
          <div class="groupname">Effective variables</div>
          <div class="kvs">${vars}</div>
          <div style="font-size:10.5px;color:var(--faint);margin-top:12px;border-top:1px solid var(--line2);padding-top:10px">Inferred from the template — click required/optional to override. Overrides carry forward across tweaks.</div>
        </div>
        <div class="card pad"><div class="groupname">Includes</div>${includes}</div>
      </div>
    </div></div>`;
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
  return `<button class="btn primary" data-act="openCommit">Commit…</button>`;
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
    tcActive: tcs.test_contexts[0]?.name || null,
    diffAgainst: "base", diffMode: "source", diffTc: tcs.test_contexts[0]?.name || null,
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

  const tabs = [["write", "Write"], ["diff", "Diff"], ["review", "Review"]];
  const tabsHtml = tabs.map(([id, label]) =>
    `<span class="tab ${tab === id ? "active" : ""}" data-act="draftTab" data-tab="${id}">${label}</span>`).join("");

  const body = tab === "diff" ? draftDiffTabShell(window._dp)
             : tab === "review" ? draftReviewTab(window._dp)
             : draftWriteTab(window._dp);

  el("main").innerHTML = `<div class="screen">
    <div class="crumb"><a data-act="go" data-hash="#/prompts">Prompts</a> /
      <a data-act="go" data-hash="#/p/${enc(pid)}/overview">${esc(pid)}</a> /</div>
    <div class="h1row"><span class="h1 sm serif">Draft — <i>v${draft.version_number}</i></span>
      <span class="sub">based on <span class="mono">${esc(draft.base_sha || "—")}</span> ·
        <span class="autochip faint" id="autoChip">saved</span> ·
        <span id="draftLintChip">${lintChipHtml(draft)}</span></span>
      <div class="grow"></div>
      <select class="envsel" data-act="switchDraft" style="max-width:240px">${switcherOpts}</select>
      <span id="draftPrimaryWrap">${draftPrimary(draft)}</span></div>
    <div class="tabs">${tabsHtml}</div>
    <div id="draftTabBody">${body}</div></div>`;

  if (tab === "write" && window._dp.tcActive) doRenderDraft();
  if (tab === "diff") loadDraftDiff();
}

function draftWriteTab(dp) {
  const draft = dp.draft;
  const chips = dp.tcs.map((t) =>
    `<span class="chip ${t.name === dp.tcActive ? "active" : ""}" data-act="tc" data-name="${esc(t.name)}">${esc(t.name)}</span>`).join("");
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
        <span style="font-size:10.5px;color:var(--faint)">live · fragments expanded</span></div>
      <div style="display:flex;gap:6px;padding:12px 16px 4px;flex-wrap:wrap">${chips || '<span class="faint">No test contexts</span>'}</div>
      <div class="render-out" id="renderOut">Pick a test context — renders live as you type.</div>
    </div></div>`;
}

function draftReviewTab(dp) {
  const draft = dp.draft, selfOk = draft.allow_self_review;
  const isAuthor = draft.author === (State.me && State.me.name);
  const blocked = isAuthor && !selfOk;
  const policyText = draft.review_policy > 0
    ? `${draft.review_policy} approval(s) to commit · ${selfOk
        ? "self-review counts — the author can approve their own draft"
        : "distinct reviewer required — the author's own approval doesn't count"}`
    : "no approvals required to commit";
  const approvers = draft.reviewers.length
    ? draft.reviewers.map((r) => `<span class="pill live">✓ ${esc(r)}</span>`).join(" ")
    : '<span class="faint">No approvals yet.</span>';
  const notice = _draftNotice
    ? `<div class="banner warn"><span style="font-size:12.5px;font-weight:600">${esc(_draftNotice)}</span></div>` : "";
  _draftNotice = null;   // one-shot
  return `${notice}
    <div class="panelrow">
      <div style="flex:10 1 440px;min-width:0"><div class="card">
        <div style="padding:14px 18px;border-bottom:1px solid var(--line2);display:flex;align-items:center;gap:10px;flex-wrap:wrap">
          <span style="font-size:13px;font-weight:700">Reviewers judge what will be served</span>
          <span class="mono muted" style="font-size:10.5px">${esc(draft.id)} · base ${esc(draft.base_sha || "")}</span>
          <div class="grow"></div>
          ${blocked ? `<button class="btn" disabled>Approve ✓</button>`
                    : `<button class="btn" data-act="approve" data-draft="${esc(draft.id)}">Approve ✓</button>`}</div>
        <div style="padding:12px 18px;font-size:11.5px;color:var(--mut)">${esc(policyText)}</div>
        ${blocked ? `<div style="padding:0 18px 10px;font-size:11.5px;color:var(--warn);font-weight:600">You authored this draft — a distinct reviewer must approve.</div>` : ""}
        <div style="padding:0 18px 14px;display:flex;gap:6px;flex-wrap:wrap;align-items:center">${approvers}</div>
        <div class="render-out" style="margin:0 18px 18px">${esc(draft.content || "")}</div></div>
      </div>
      <div style="flex:1 1 240px;min-width:0"><div class="card pad">
        <div class="groupname">Review policy</div>
        <div style="display:flex;gap:8px;align-items:center;flex-wrap:wrap;margin-bottom:8px">
          <span class="pill ${selfOk ? "live" : "warn"}">${selfOk ? "self-review on" : "four-eyes"}</span>
          <span class="link ${selfOk ? "faint" : ""}" data-act="toggleSelfReview" data-project="${esc(draft.project)}" data-to="${selfOk ? "false" : "true"}">${selfOk ? "require distinct reviewer" : "allow self-review"}</span></div>
        <div style="font-size:10.5px;color:var(--faint);border-top:1px solid var(--line2);padding-top:10px">Review gates what enters the repo — targeting gates who sees it.</div>
      </div></div>
    </div>`;
}

async function doRenderDraft() {
  const out = el("renderOut"); if (!out || !window._dp) return;
  const tc = window._dp.tcActive;
  if (!tc) { out.textContent = "Pick a test context — renders live as you type."; return; }
  out.textContent = "rendering…";
  try {
    const r = await POST(`/mgmt/drafts/${window._dp.draft.id}/render`, {
      environment: State.env, test_context: tc,
    });
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
      revs.push({ v: v.version, sha: v.tip_full_sha, label: `v${v.version} · tip · ${v.tip_sha}` });
    if (v.live_full_sha)
      revs.push({ v: v.version, sha: v.live_full_sha, label: `v${v.version} · live · ${v.live_sha}` });
  }
  const sel = dp.diffAgainst || "base";
  const opts = `<option value="base"${sel === "base" ? " selected" : ""}>base — v${draft.version_number} @ ${esc(draft.base_sha || "—")}</option>` +
    revs.map((r) => { const tok = r.v + ":" + r.sha;
      return `<option value="${esc(tok)}"${sel === tok ? " selected" : ""}>${esc(r.label)}</option>`; }).join("");
  const mode = dp.diffMode || "source";
  const tcRow = mode === "rendered"
    ? `<div style="display:flex;gap:6px;margin-bottom:10px;flex-wrap:wrap">${
        dp.tcs.map((t) => `<span class="chip ${t.name === dp.diffTc ? "active" : ""}" data-act="diffTc" data-name="${esc(t.name)}">${esc(t.name)}</span>`).join("")
        || '<span class="faint">No test contexts</span>'}</div>` : "";
  return `<div style="display:flex;gap:9px;align-items:center;margin-bottom:12px;flex-wrap:wrap">
      <span class="faint" style="font-size:12px">against</span>
      <select class="envsel" data-act="diffAgainst" style="min-width:240px">${opts}</select></div>
    <div class="tabs">
      <span class="tab ${mode === "source" ? "active" : ""}" data-act="diffMode" data-mode="source">Source</span>
      <span class="tab ${mode === "rendered" ? "active" : ""}" data-act="diffMode" data-mode="rendered">Rendered</span></div>
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
    <h3>Commit v${draft.version_number}</h3>
    <p class="hint">Lands a commit on <span class="mono">${esc(draft.prompt_id)}</span> — validated and audited. Serving is unchanged until a pointer moves.</p>
    <div class="field"><label>Commit message</label>
      <input id="commitMsg" placeholder="what changed and why" spellcheck="false"></div>
    <div class="groupname" style="margin:2px 0 6px">Source diff vs base ${esc(draft.base_sha || "")}</div>
    <div class="card"><div class="diffbox modal-diff" id="commitDiffBox"><div class="empty">Loading diff…</div></div></div>
    <div class="modal-actions">
      <button class="btn" data-act="closeModal">Cancel</button>
      <button class="btn primary" data-act="commitDraft" data-id="${esc(draft.id)}">Commit</button></div>`;
}
// 409 → the version moved since this draft's base. Show what landed in between and
// let the author force their edit on top. Replaces the old always-on force checkbox.
function openConflictModal(draftId, c) {
  const diffHtml = c.diff ? renderUnifiedDiff(c.diff) : "";
  openModal(`
    <h3>v${window._dp.draft.version_number} changed since your draft</h3>
    <p class="hint">${esc(c.detail || "The version moved since your draft's base.")} Review what landed in between (${esc(c.base_sha || "")} → ${esc(c.current_sha || "")}), then commit anyway to put your version on top.</p>
    <div class="card"><div class="diffbox modal-diff">${diffHtml || '<div class="empty">No intervening source diff.</div>'}</div></div>
    <div class="modal-actions">
      <button class="btn" data-act="closeModal">Cancel</button>
      <button class="btn danger" data-act="commitForce" data-id="${esc(draftId)}">Commit anyway</button></div>`, "wide");
}
// 412 → review required. Don't strand the user with a toast: land them on the review tab.
function goReviewNotice() {
  _draftNotice = "Review required before commit — approve below (or adjust the policy).";
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
                  short: v.tip_sha, label: `v${v.version} · latest commit (tip) · ${v.tip_sha}` });
    if (v.live_full_sha)
      revs.push({ token: `${v.version}@live`, version: v.version, sha: v.live_full_sha,
                  short: v.live_sha, label: `v${v.version} · what ${State.env} serves (live) · ${v.live_sha}` });
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
  let body;
  if (res.error) {
    body = `<div class="empty">⚠ ${esc(res.error)}</div>`;
  } else {
    const html = renderUnifiedDiff(res.diff);
    body = html || '<div class="empty">No differences — these two states are identical.</div>';
  }

  const qs = `a=${enc(aTok)}&b=${enc(bTok)}`;
  el("main").innerHTML = `<div class="screen">
    <div class="h1row"><span class="h1 sm serif">Compare — <i>${esc(pid)}</i></span>
      ${res.context ? `<span class="pill acc">${esc(res.context)}</span>` : ""}</div>
    <div style="display:flex;gap:9px;align-items:center;margin-bottom:12px;flex-wrap:wrap">
      <select class="envsel" data-act="diffPick" data-side="a" style="min-width:230px">${opts(aTok)}</select>
      <span class="faint" style="font-size:13px">→</span>
      <select class="envsel" data-act="diffPick" data-side="b" style="min-width:230px">${opts(bTok)}</select>
      <span class="mono muted" style="font-size:10.5px;margin-left:4px">${esc(a.short)} → ${esc(b.short)}</span></div>
    <div class="tabs">
      <span class="tab ${mode === "source" ? "active" : ""}" data-act="go" data-hash="#/p/${enc(pid)}/compare?mode=source&${qs}">Source</span>
      <span class="tab ${mode === "rendered" ? "active" : ""}" data-act="go" data-hash="#/p/${enc(pid)}/compare?mode=rendered&${qs}">Rendered</span></div>
    <div class="card"><div style="display:flex;gap:14px;padding:9px 18px;border-bottom:1px solid var(--line2);font-size:11px;color:var(--mut)">
      <span class="mono">${esc(pid)}</span><span style="margin-left:auto">${mode === "rendered" ? "rendered · fragments expanded" : "unified source"}</span></div>
      <div class="diffbox">${body}</div></div></div>`;
}

async function screenRules() {
  const env = State.env;
  const [d, rv] = await Promise.all([
    GET(`/mgmt/envs/${enc(env)}/rules`),
    GET(`/mgmt/envs/${enc(env)}/revisions?limit=25`),
  ]);
  const pid = State.route.pid;
  const anyKill = Object.entries(d.kills).filter(([, v]) => v).map(([k]) => k);
  const killBanner = anyKill.length
    ? `<div class="banner danger"><span style="font-size:15px">⏻</span>
        <span style="font-size:12.5px;font-weight:700">Kill switch engaged for ${anyKill.map(esc).join(", ")} — serving the environment default, rules bypassed.</span>
        <span class="link" style="margin-left:auto;color:var(--danger)" data-act="kill" data-pid="${esc(anyKill[0])}" data-engage="false">Restore rules</span></div>`
    : "";
  const rules = d.rules.map((r) => {
    const scopeTag = r.scope === "global"
      ? `<span class="tag acc">GLOBAL · P${r.priority}</span>`
      : `<span class="tag mut">PROMPT · ${esc(r.prompt_id)} · P${r.priority}</span>`;
    const statusCol = r.status === "active"
      ? `<span style="font-size:10.5px;color:var(--live);font-weight:600">● Active</span>`
      : `<span class="faint" style="font-size:10.5px">${esc(r.status)}</span>`;
    const toggle = r.status === "active"
      ? `<span class="link faint" data-act="ruleStatus" data-id="${esc(r.id)}" data-status="archived">Archive</span>`
      : `<span class="link" data-act="ruleStatus" data-id="${esc(r.id)}" data-status="active">Activate</span>`;
    return `<div class="rule"><div class="rule-head">
        <span style="color:var(--faint)">⠿</span>${scopeTag}
        <span style="font-size:13px;font-weight:700">${esc(r.id)}</span>
        <span style="font-size:11px;color:var(--mut)">${esc(r.comment || "")}</span>
        <div class="grow"></div>${statusCol}${toggle}</div>
      <div class="rule-when">If ${describeWhen(r.when)} → serve ${describeServe(r.serve)}</div></div>`;
  }).join("") || '<div class="empty">No rules — everyone gets the environment default.</div>';

  const defaultPid = pid || (d.rules.find((r) => r.prompt_id)?.prompt_id);
  const defV = defaultPid ? d.defaults[defaultPid] : null;

  const revRows = rv.revisions.map((r, i) => `
    <tr class="grow-row">
      <td class="mono muted">rv${r.rules_version}</td>
      <td><span class="tag ${r.kind === "rollback" ? "acc" : "mut"}">${esc(r.kind)}</span>
        ${r.rule_id ? `<span class="mono" style="font-size:11px">${esc(r.rule_id)}</span>` : ""}</td>
      <td><b>${esc(r.actor || "—")}</b></td>
      <td class="muted" style="font-size:11px">${esc(r.comment || "")}</td>
      <td class="mono muted" style="font-size:11px">${new Date(r.at).toLocaleString()}</td>
      <td style="text-align:right;white-space:nowrap">${i === 0
        ? '<span class="faint" style="font-size:10.5px">current</span>'
        : `<button class="btn" data-act="rollback" data-rv="${r.rules_version}">▸ Roll back here</button>`}</td>
    </tr>`).join("") || '<tr><td colspan="6" class="empty">No targeting changes yet.</td></tr>';

  el("main").innerHTML = `<div class="screen">
    <div class="h1row"><span class="h1 sm serif">Targeting — <i>${esc(env)}</i></span>
      ${d.protected ? '<span class="pill warn">PROTECTED</span>' : ""}
      <div class="grow"></div>
      <span class="sub">rules_version <b>${d.rules_version}</b> · <span style="color:var(--live)">● synced &lt;2s</span></span></div>
    ${killBanner}
    <div class="card">${rules}
      <div class="default-row"><span class="muted" style="font-size:12px">Everyone else →</span>
        <span style="font-size:12.5px;font-weight:700">Default: ${defaultPid ? "v" + (defV || "?") + " @ live" : "environment default"}</span>
        <div class="grow"></div>
        ${defaultPid ? `<button class="btn danger" data-act="kill" data-pid="${esc(defaultPid)}" data-engage="true">⏻ Kill switch</button>` : ""}</div>
    </div>
    <div style="font-size:11px;color:var(--faint);margin-top:12px">First match wins, top to bottom. Rule edits apply in &lt;2s, no approval ceremony — rules can only reference validated SHAs. Pointer moves and default changes are the governed acts.</div>
    <div class="h1row" style="margin-top:26px"><span class="h1 sm serif">Change history</span>
      <span class="sub">every targeting mutation, stamped with the rules_version it produced</span></div>
    <div class="card" style="overflow-x:auto"><table class="grid">
      <thead class="ghead"><tr><th>Version</th><th>Change</th><th>Actor</th><th>Comment</th><th>When</th><th></th></tr></thead>
      <tbody>${revRows}</tbody></table></div>
    <div style="font-size:11px;color:var(--faint);margin-top:12px">Rolling back restores the rule set to that version — rules created afterward are archived. Itself a change (bumps rules_version), so history is append-only.</div></div>`;
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
          <span class="mono" style="font-size:12.5px;font-weight:600">${esc(m.sha)}</span>
          ${m.current ? '<span class="pill live" style="font-size:10px">● LIVE NOW</span>' : ""}
          <span class="muted" style="font-size:11px">${esc(m.by)} · ${new Date(m.at).toLocaleString()}${m.from_sha ? " · from " + esc(m.from_sha) : ""}</span></div>
        <div style="font-size:12px;color:var(--mut);margin-top:3px;font-style:italic">${esc(m.comment || "")}</div></div>
      ${!m.current ? `<button class="btn" data-act="revert" data-sha="${m.full_sha}" data-v="${version}">▸ Revert here</button>` : ""}</div>`;
  }).join("") || '<div class="empty">No pointer history yet.</div>';

  const advance = (vrow.tip_ahead > 0 && vrow.tip_full_sha)
    ? `<button class="tweak-btn" style="width:auto;display:inline-flex" data-act="makeLive" data-sha="${vrow.tip_full_sha}" data-v="${version}">✦ Advance to tip → ${esc(vrow.tip_sha)}</button>
       <span class="faint" style="font-size:11px">releaser-gated, applied immediately${State.envs.find((e) => e.id === State.env)?.protected ? " — locked env: type the prompt id to confirm" : " — pointer moves are unilateral"}</span>`
    : `<span style="font-size:12px;color:var(--live);font-weight:600">✓ Tip is live — nothing to advance.</span>`;

  el("main").innerHTML = `<div class="screen">
    <div class="h1row"><span class="h1 sm serif">Live pointers — <i>v${version} · ${esc(State.env)}</i></span></div>
    <div style="font-size:12px;color:var(--mut);margin-bottom:18px">Append-only. The newest entry is what serves. Any prior entry is one click from live again — the blame view for "which make-live changed behavior at 3pm?"</div>
    <div class="card">${moves}</div>
    <div style="display:flex;gap:10px;margin-top:14px;align-items:center;flex-wrap:wrap">${advance}</div></div>`;
}

async function screenSegments() {
  const d = await GET(`/mgmt/envs/${enc(State.env)}/segments`);
  const list = d.segments.map((s, i) =>
    `<div class="card pad" style="${i === 0 ? "border:1.5px solid var(--acc)" : ""}">
      <div style="font-size:13px;font-weight:700">${esc(s.name)}</div>
      <div style="font-size:11px;color:var(--mut);margin-top:3px">referenced by ${s.referenced_by} rule(s)</div></div>`).join("")
    || '<div class="empty">No segments.</div>';
  const detail = d.segments[0] ? `<div class="card pad">
      <div style="display:flex;align-items:center;gap:10px;margin-bottom:14px"><span style="font-size:14px;font-weight:700">${esc(d.segments[0].name)}</span>
      <span class="faint" style="font-size:11px">match all of:</span></div>
      <div class="render-out" style="margin:0">${describeWhen(d.segments[0].when)}</div>
      <div style="border-top:1px solid var(--line2);margin-top:14px;padding-top:12px;font-size:11px;color:var(--faint)">A clause referencing an absent flag does not match — never errors. Edits propagate in &lt;2s.</div></div>`
    : "";
  el("main").innerHTML = `<div class="screen">
    <div class="h1row"><span class="h1 sm serif">Segments — <i>${esc(State.env)}</i></span></div>
    <div class="panelrow"><div style="flex:1 1 220px;display:flex;flex-direction:column;gap:10px">${list}</div>
      <div style="flex:10 1 420px;min-width:0">${detail}</div></div></div>`;
}

async function screenAudit() {
  const d = await GET(`/mgmt/audit?limit=60`);
  const rows = d.audit.map((a) =>
    `<tr class="grow-row"><td class="mono muted">${new Date(a.at).toLocaleString()}</td>
      <td><b>${esc(a.actor)}</b></td><td><span class="tag acc">${esc(a.action)}</span></td>
      <td class="mono" style="font-size:11px">${esc(a.object_id)}</td></tr>`).join("");
  el("main").innerHTML = `<div class="screen">
    <div class="h1row"><span class="h1 sm serif">Audit</span><span class="sub">every control-plane mutation, newest first</span></div>
    <div class="card" style="overflow-x:auto"><table class="grid">
      <thead class="ghead"><tr><th>When</th><th>Actor</th><th>Action</th><th>Object</th></tr></thead>
      <tbody>${rows || '<tr><td colspan="4" class="empty">No audit entries.</td></tr>'}</tbody></table></div></div>`;
}

function _scopeLabel(b) {
  if (!b.project_id && !b.environment_id) return "instance-wide";
  const parts = [];
  if (b.project_id) parts.push("project " + b.project_id);
  if (b.environment_id) parts.push("env " + b.environment_id);
  return parts.join(" · ");
}

async function screenAccess() {
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
        <span class="link" data-act="removeBinding" data-pid="${esc(p.id)}" data-bid="${b.id}" title="remove"
          style="color:var(--danger)">✕</span></span>`).join(" ") ||
      '<span class="faint" style="font-size:11px">no roles</span>';
    const keys = p.keys.map((k) =>
      `<div style="display:flex;gap:10px;align-items:center;font-size:11px">
        <span class="mono ${k.revoked ? "faint" : ""}">${esc(k.prefix)}…</span>
        ${k.revoked ? '<span class="pill warn">revoked</span>'
                    : '<span class="pill live">active</span>'}
        <span class="faint">${k.last_used_at ? "used " + ago(k.last_used_at) : "never used"}</span>
        ${k.revoked ? "" : `<span class="link" data-act="revokeKey" data-kid="${k.id}"
           style="color:var(--danger);margin-left:auto">revoke</span>`}</div>`).join("") ||
      '<span class="faint" style="font-size:11px">no keys</span>';
    return `<div class="card pad">
      <div style="display:flex;gap:10px;align-items:center;flex-wrap:wrap">
        <span style="font-size:13.5px;font-weight:700">${esc(p.name || p.id)}</span>
        <span class="tag mut">${esc(p.kind)}</span>
        <span class="mono faint" style="font-size:10.5px">${esc(p.id)}</span>
        <div class="grow"></div>
        <button class="btn" data-act="addBinding" data-pid="${esc(p.id)}">+ role</button>
        <button class="btn" data-act="issueKey" data-pid="${esc(p.id)}" data-name="${esc(p.name)}">+ key</button></div>
      <div style="margin-top:10px;display:flex;gap:6px;flex-wrap:wrap;align-items:center">${bindings}</div>
      <div style="margin-top:12px;border-top:1px solid var(--line2);padding-top:10px;display:flex;flex-direction:column;gap:6px">${keys}</div>
    </div>`;
  }).join("") || '<div class="empty">No users yet.</div>';

  el("main").innerHTML = `<div class="screen">
    <div class="h1row"><span class="h1 sm serif">Access</span>
      <span class="sub">users, roles, and API keys</span>
      <div class="grow"></div>
      <button class="btn primary" data-act="newUser">+ New user</button></div>
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
  const vers = Object.entries(r.versions).map(([k, v]) =>
    `<div class="mono" style="font-size:11.5px">${esc(k)} → v${v.version} · ${esc(v.commit)}${
      v.fallback ? ' <span class="pill warn">fallback</span>' : ""}</div>`).join("");
  const flags = [
    r.stale_rules ? '<span class="pill warn">stale_rules</span>' : "",
    r.content_fallback ? '<span class="pill warn">content_fallback</span>' : "",
  ].join(" ");
  return `<div class="card pad">
    <div style="display:flex;gap:10px;align-items:center;margin-bottom:10px;flex-wrap:wrap">
      <span class="tag ${pinned ? "acc" : "mut"}">${pinned ? "PINNED REPLAY" : "matched: " + esc(matched)}</span>
      <span class="faint" style="font-size:11px">rules_version ${r.rules_version}</span>${flags}</div>
    <div class="render-out" style="white-space:pre-wrap;margin:0">${esc(r.prompt)}</div>
    <div style="border-top:1px solid var(--line2);margin-top:12px;padding-top:10px">
      <div style="font-size:11px;font-weight:600;letter-spacing:.03em;color:var(--faint);text-transform:uppercase;margin-bottom:6px">Resolved versions (pin)</div>
      ${vers}
      <div style="margin-top:10px;display:flex;gap:10px;align-items:center;flex-wrap:wrap">
        <button class="btn" data-act="pinLast">⚓ Reproduce exactly (pin)</button>
        <span class="faint" style="font-size:11px">re-renders ignoring targeting — same output regardless of flags</span></div>
    </div></div>`;
}

async function screenPlay() {
  const pid = window._playPid || State.route.q.pid || "support/system";
  const flags = window._playFlags != null ? window._playFlags : '{"user_id": "u_12"}';
  const vars = window._playVars != null ? window._playVars
    : '{"customer_name": "Acme", "history": []}';
  const pinned = !!window._playPin;
  const last = window._playLast;
  el("main").innerHTML = `<div class="screen">
    <div class="h1row"><span class="h1 sm serif">Playground — <i>${esc(State.env)}</i></span>
      <span class="sub">render through the live serving path; capture a pin to reproduce it exactly</span></div>
    <div class="panelrow">
      <div style="flex:1 1 300px;display:flex;flex-direction:column;gap:12px">
        <div class="field"><label>Prompt id</label>
          <input id="playPid" value="${esc(pid)}" spellcheck="false"></div>
        <div class="field"><label>Flags (JSON)</label>
          <textarea id="playFlags" spellcheck="false" style="min-height:66px">${esc(flags)}</textarea></div>
        <div class="field"><label>Variables (JSON)</label>
          <textarea id="playVars" spellcheck="false" style="min-height:66px">${esc(vars)}</textarea></div>
        <div style="display:flex;gap:10px;align-items:center">
          <button class="btn primary" data-act="play">Render</button>
          ${pinned ? '<span class="pill live">pinned</span><span class="link faint" data-act="unpin">clear pin</span>' : ""}
        </div>
      </div>
      <div style="flex:10 1 440px;min-width:0">${
        last ? renderPlayResult(last, pinned)
             : '<div class="empty">Render a prompt to see the resolved output and its reproducible pin.</div>'}</div>
    </div></div>`;
}

const SCREENS = {
  prompts: screenPrompts, overview: screenOverview, draft: screenDraft, compare: screenCompare,
  rules: screenRules, pointers: screenPointers, segments: screenSegments,
  play: screenPlay, audit: screenAudit, access: screenAccess,
};

// ── actions ──────────────────────────────────────────────────────────
const Actions = {
  go(ds) { go(ds.hash); },
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
  setToken() {
    State.token = el("tokenIn").value.trim();
    localStorage.setItem("incant_token", State.token);
    State.me = null;   // identity changes with the key — re-fetch on next draft page
    toast("API key updated");
    render();
  },
  toggleTweak() { State.tweakOpen = !State.tweakOpen; render(); },
  noop() {},
  closeModal() { closeModal(); },
  search(ds, ev) {
    const q = (ev.target.value || "").toLowerCase().trim();
    document.querySelectorAll("tr.prow").forEach((r) => {
      r.style.display = !q || r.dataset.pid.toLowerCase().includes(q) ? "" : "none";
    });
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
  async newVersion() {
    const pid = State.route.pid;
    try {
      const dv = await GET(`/mgmt/prompts/${enc(pid)}/versions?environment=${enc(State.env)}`);
      const seed = dv.versions.find((x) => x.is_default)?.version || dv.versions[0]?.version;
      const created = await POST(`/mgmt/prompts/${enc(pid)}/drafts`, { seed_from_version: seed, title: "New version" });
      go(`#/p/${enc(pid)}/draft?draft=${created.id}`);
    } catch (e) { toast(errText(e), true); }
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
  tc(ds) {
    window._dp.tcActive = ds.name;
    document.querySelectorAll(".chip").forEach((c) => c.classList.toggle("active", c.dataset.name === ds.name));
    doRenderDraft();
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
    document.querySelectorAll(".chip").forEach((c) => c.classList.toggle("active", c.dataset.name === ds.name));
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
      toast(`Committed ${r.sha} · v${r.version_number} · ${r.validation.status}`);
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
      toast(`Committed ${r.sha} · v${r.version_number} · ${r.validation.status}`);
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
  async ruleStatus(ds) {
    try { await PATCH(`/mgmt/envs/${enc(State.env)}/rules/${ds.id}`, { status: ds.status }); toast(`Rule ${ds.id} → ${ds.status}`); render(); }
    catch (e) { toast(errText(e), true); }
  },
  rollback(ds) {
    const body = `Restore <b>${esc(State.env)}</b>'s rule set to <span class="mono">rules_version ${esc(ds.rv)}</span>.
      Rules created after that version are archived (they stop serving). This is itself
      a change and bumps the rules_version — history stays append-only.`;
    // A locked env asks you to type the env name; rollback is env-scoped.
    if (isLocked()) {
      openModal(typeToConfirm({
        title: "Roll back targeting", body, token: State.env,
        confirmLabel: "Roll back to rv" + ds.rv, act: "rollbackConfirm", data: { rv: ds.rv },
      }));
      return;
    }
    openModal(`
      <h3>Roll back targeting</h3>
      <p class="hint">${body}</p>
      <div class="modal-actions">
        <button class="btn" data-act="closeModal">Cancel</button>
        <button class="btn primary" data-act="rollbackConfirm" data-rv="${esc(ds.rv)}">Roll back to rv${esc(ds.rv)}</button>
      </div>`);
  },
  async rollbackConfirm(ds) {
    try {
      const r = await POST(`/mgmt/envs/${enc(State.env)}/rollback`,
                           { to_rules_version: parseInt(ds.rv), confirm: State.env });
      closeModal();
      toast(`Rolled back to rv${ds.rv} — ${r.rules_changed} rule(s) changed`);
      render();
    } catch (e) { toast(errText(e), true); }
  },
  async kill(ds) {
    const engage = ds.engage === "true";
    try { await POST(`/mgmt/envs/${enc(State.env)}/kill?prompt_id=${enc(ds.pid)}`, { engaged: engage }); toast(engage ? "Kill switch engaged" : "Rules restored"); render(); }
    catch (e) { toast(errText(e), true); }
  },
  makeLive(ds) {
    // Pointer moves are unilateral (releaser); a locked env asks you to type the
    // prompt id first, LaunchDarkly-style.
    if (isLocked()) {
      openModal(typeToConfirm({
        title: "Advance the live pointer",
        body: `<b>${esc(State.env)}</b> is locked. This immediately changes what <b>${esc(State.route.pid)}</b> serves.`,
        token: State.route.pid, confirmLabel: "Make live",
        act: "makeLiveConfirm", data: { sha: ds.sha, v: ds.v },
      }));
      return;
    }
    Actions.makeLiveConfirm(ds);
  },
  async makeLiveConfirm(ds) {
    try {
      await POST(`/mgmt/envs/${enc(State.env)}/pointers`, {
        prompt_id: State.route.pid, version_number: parseInt(ds.v), to_sha: ds.sha,
        comment: "make live via UI", confirm: State.route.pid,
      });
      closeModal();
      toast("Pointer advanced — tip is live");
      render();
    } catch (e) { toast(errText(e), true); }
  },
  revert(ds) {
    if (isLocked()) {
      openModal(typeToConfirm({
        title: "Revert the live pointer",
        body: `<b>${esc(State.env)}</b> is locked. This moves <b>${esc(State.route.pid)}</b> back to a previous version, live.`,
        token: State.route.pid, confirmLabel: "Revert",
        act: "revertConfirm", data: { sha: ds.sha, v: ds.v },
      }));
      return;
    }
    Actions.revertConfirm(ds);
  },
  async revertConfirm(ds) {
    try {
      await POST(`/mgmt/envs/${enc(State.env)}/pointers`, {
        prompt_id: State.route.pid, version_number: parseInt(ds.v), to_sha: ds.sha,
        comment: "revert via UI", confirm: State.route.pid,
      });
      closeModal();
      toast("Reverted — pointer moved");
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
};

// ── render + wire ────────────────────────────────────────────────────
function render() {
  if (Auto.timer) fireAutosave();   // flush a pending autosave before the DOM is replaced
  document.body.dataset.theme = State.theme;
  State.route = parseRoute();
  el("app").innerHTML = shell(`<div class="empty">Loading…</div>`);
  const fn = SCREENS[State.route.name] || screenPrompts;
  fn().catch((e) => {
    if (e && e.status === 401) {
      el("main").innerHTML = `<div class="empty">Unauthorized. Set a valid API key in the sidebar (default: <span class="mono">incant_sk_dev_admin</span>).</div>`;
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
  if (act === "search" || act === "draftInput") Actions[act](t.dataset, ev);
});
document.addEventListener("keydown", (ev) => {
  if (ev.key === "Escape") closeModal();
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
