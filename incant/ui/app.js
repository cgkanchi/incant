/* Incant UI — single-page app over the mgmt + serving APIs. Vanilla JS, no build. */
"use strict";

const State = {
  token: localStorage.getItem("incant_token") || "incant_sk_dev_admin",
  env: localStorage.getItem("incant_env") || "prod",
  theme: localStorage.getItem("incant_theme") || "light",
  envs: [],
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
function openModal(html) {
  closeModal();
  const o = document.createElement("div");
  o.id = "modal";
  o.className = "modal-overlay";
  o.innerHTML = `<div class="modal" data-act="noop">${html}</div>`;
  document.body.appendChild(o);
  const first = o.querySelector("input, textarea");
  if (first) first.focus();
}
function closeModal() {
  const m = el("modal");
  if (m) m.remove();
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
  if (parts[0] === "approvals") return { name: "approvals", pid: null, q };
  if (parts[0] === "play") return { name: "play", pid: null, q };
  if (parts[0] === "audit") return { name: "audit", pid: null, q };
  if (parts[0] === "p") {
    const pid = decodeURIComponent(parts[1] || "");
    const screen = parts[2] || "overview";
    return { name: screen, pid, q };
  }
  return { name: "prompts", pid: null, q };
}

// ── shell / sidebar ──────────────────────────────────────────────────
function subnav(pid) {
  if (!pid) return "";
  const items = [
    ["overview", "Overview", "◈"], ["editor", "Draft editor", "✎"], ["diff", "Diff", "⇄"],
    ["review", "Review", "☰"], ["rules", "Targeting", "◐"], ["pointers", "Pointers", "▸"],
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
    <div class="nav ${State.route.name === "approvals" ? "active" : ""}" data-act="go" data-hash="#/approvals">
      <span class="gl">⧉</span><span>Approvals</span></div>
    <div class="nav ${State.route.name === "play" ? "active" : ""}" data-act="go" data-hash="#/play">
      <span class="gl">▶</span><span>Playground</span></div>
    <div class="nav ${State.route.name === "audit" ? "active" : ""}" data-act="go" data-hash="#/audit">
      <span class="gl">◷</span><span>Audit</span></div>
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
  const steps = [
    ["Edit", "Draft on the version — commit lands, serving unchanged", "editor"],
    ["Commit", "Validated + review passed", "review"],
    ["Target a segment", "Rule: cohort → version @ tip", "rules"],
    ["Verify", "Render test contexts + diff against live", "diff"],
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

async function ensureDraft(pid, version, seed) {
  const list = await GET(`/mgmt/prompts/${enc(pid)}/drafts`);
  let draft = list.drafts.find((x) => x.version_number === version) || list.drafts[0];
  if (draft) return draft.id;
  const created = await POST(`/mgmt/prompts/${enc(pid)}/drafts`, {
    version_number: version, seed_from_version: seed || version, title: "Draft v" + version,
  });
  return created.id;
}

async function screenEditor() {
  const pid = State.route.pid;
  const vq = State.route.q.v ? parseInt(State.route.q.v) : null;
  el("main").innerHTML = `<div class="empty">Opening draft…</div>`;
  // find or create a draft
  let draftId = State.route.q.draft;
  if (!draftId) {
    const dv = await GET(`/mgmt/prompts/${enc(pid)}/versions?environment=${enc(State.env)}`);
    const targetV = vq || (dv.versions.find((x) => x.is_default)?.version) || dv.versions[0]?.version || 1;
    draftId = await ensureDraft(pid, targetV, targetV);
  }
  const [draft, tcs] = await Promise.all([
    GET(`/mgmt/drafts/${draftId}`),
    GET(`/mgmt/prompts/${enc(pid)}/test-contexts`),
  ]);
  window._draft = draft;
  window._tcs = tcs.test_contexts;
  window._tcActive = tcs.test_contexts[0]?.name || null;

  const lint = draft.lint || {};
  const lintPill = lint.status === "valid"
    ? `<span class="pill live">✓ lint clean</span>`
    : `<span class="pill danger">${esc(lint.error || "invalid")}</span>`;
  const vars = draft.variables
    ? `variables: ${draft.variables.required.map((n) => "<b>" + esc(n) + "</b>").join(" · ")}${draft.variables.optional.length ? " · " + draft.variables.optional.map((n) => esc(n) + "?").join(" · ") : ""}`
    : "";
  const chips = window._tcs.map((t) =>
    `<span class="chip ${t.name === window._tcActive ? "active" : ""}" data-act="tc" data-name="${esc(t.name)}">${esc(t.name)}</span>`).join("");

  el("main").innerHTML = `<div class="screen">
    <div class="h1row"><span class="h1 sm serif">Draft — <i>v${draft.version_number}</i></span>
      <span class="mono muted" style="font-size:10.5px">base ${esc(draft.base_sha || "")}</span>${lintPill}
      <div class="grow"></div>
      <button class="btn" data-act="go" data-hash="#/p/${enc(pid)}/diff">View diff</button>
      <button class="btn" data-act="saveDraft">Save draft</button>
      <button class="btn primary" data-act="go" data-hash="#/p/${enc(pid)}/review">Request review</button></div>
    <div class="editor-wrap">
      <div class="card editor">
        <div class="ed-head"><span class="mono">v${draft.version_number}.j2</span><span>·</span><span>Jinja2</span>
          <span style="margin-left:auto" class="mono">${esc(draft.id)}</span></div>
        <textarea class="ta" id="draftTa" spellcheck="false">${esc(draft.content || "")}</textarea>
        <div class="ed-foot"><span id="varLine">${vars}</span>
          <span style="margin-left:auto" id="lintLine" class="${lint.status === "valid" ? "" : "muted"}" style="color:var(--live)">${lint.status === "valid" ? "includes resolve ✓ · no cycles ✓" : ""}</span></div>
      </div>
      <div class="card testpanel">
        <div style="padding:12px 16px;border-bottom:1px solid var(--line2);display:flex;align-items:center;gap:8px">
          <span style="font-size:12px;font-weight:700">Test render</span>
          <span style="font-size:10.5px;color:var(--faint)">live · fragments expanded</span></div>
        <div style="display:flex;gap:6px;padding:12px 16px 4px;flex-wrap:wrap">${chips || '<span class="faint">No test contexts</span>'}</div>
        <div class="render-out" id="renderOut">Pick a context and Save draft to render.</div>
      </div>
    </div>
    <div style="margin-top:14px;display:flex;gap:10px;align-items:center;flex-wrap:wrap">
      <button class="btn live" data-act="commit">Commit draft</button>
      <label style="font-size:11px;color:var(--mut)"><input type="checkbox" id="forceCommit"> force (override concurrency)</label>
      <span class="faint" style="font-size:11px">Commit is gated by the project review policy.</span>
    </div></div>`;
  if (window._tcActive) doRender();
}

async function doRender() {
  const out = el("renderOut");
  if (!out) return;
  out.textContent = "rendering…";
  try {
    const r = await POST(`/mgmt/drafts/${window._draft.id}/render`, {
      environment: State.env, test_context: window._tcActive || undefined,
    });
    out.textContent = r.rendered;
  } catch (e) {
    out.textContent = "⚠ " + errText(e);
  }
}

async function screenDiff() {
  const pid = State.route.pid;
  const d = await GET(`/mgmt/prompts/${enc(pid)}/versions?environment=${enc(State.env)}`);
  const mode = State.route.q.mode || "source";

  // Prefer the classic "v @ live → v @ tip" tweak diff. If tip == live everywhere,
  // fall back to a cross-version diff of the two newest versions at their live SHAs.
  const tweak = d.versions.find((v) => v.tip_ahead > 0 && v.live_full_sha && v.tip_full_sha);
  let a, b, title;
  if (tweak) {
    a = { version: tweak.version, sha: tweak.live_full_sha, short: tweak.live_sha };
    b = { version: tweak.version, sha: tweak.tip_full_sha, short: tweak.tip_sha };
    title = `v${tweak.version} @ live → @ tip`;
  } else {
    const withLive = d.versions.filter((v) => v.live_full_sha);
    if (withLive.length >= 2) {
      const [hi, lo] = [withLive[0], withLive[1]];
      a = { version: lo.version, sha: lo.live_full_sha, short: lo.live_sha };
      b = { version: hi.version, sha: hi.live_full_sha, short: hi.live_sha };
      title = `v${lo.version} @ live → v${hi.version} @ live`;
    } else {
      el("main").innerHTML = `<div class="screen"><div class="h1row"><span class="h1 sm serif">Diff</span></div><div class="empty">Nothing to diff yet — need two committed states.</div></div>`;
      return;
    }
  }

  const q = `a_version=${a.version}&a_sha=${a.sha}&b_version=${b.version}&b_sha=${b.sha}&mode=${mode}&environment=${enc(State.env)}`;
  const res = await GET(`/mgmt/prompts/${enc(pid)}/diff?${q}`);
  let body;
  if (res.error) {
    body = `<div class="empty">⚠ ${esc(res.error)}</div>`;
  } else {
    const lines = (res.diff || "").split("\n").map((ln) => {
      let cls = "";
      if (ln.startsWith("+") && !ln.startsWith("+++")) cls = "add";
      else if (ln.startsWith("-") && !ln.startsWith("---")) cls = "del";
      if (ln.startsWith("@@") || ln.startsWith("+++") || ln.startsWith("---")) return `<div class="diffline"><span class="gut"></span><span class="txt faint">${esc(ln)}</span></div>`;
      return `<div class="diffline ${cls}"><span class="gut">${cls === "add" ? "+" : cls === "del" ? "−" : ""}</span><span class="txt">${esc(ln.replace(/^[+-]/, ""))}</span></div>`;
    }).join("");
    body = lines || '<div class="empty">No differences — these two states are identical.</div>';
  }

  el("main").innerHTML = `<div class="screen">
    <div class="h1row"><span class="h1 sm serif">Diff — <i>${title}</i></span>
      <span class="mono muted" style="font-size:10.5px">${esc(a.short)} → ${esc(b.short)}</span>
      ${res.context ? `<span class="pill acc">${esc(res.context)}</span>` : ""}</div>
    <div class="tabs">
      <span class="tab ${mode === "source" ? "active" : ""}" data-act="go" data-hash="#/p/${enc(pid)}/diff?mode=source">Source</span>
      <span class="tab ${mode === "rendered" ? "active" : ""}" data-act="go" data-hash="#/p/${enc(pid)}/diff?mode=rendered">Rendered</span></div>
    <div class="card"><div style="display:flex;gap:14px;padding:9px 18px;border-bottom:1px solid var(--line2);font-size:11px;color:var(--mut)">
      <span class="mono">${esc(pid)}</span><span style="margin-left:auto">${mode === "rendered" ? "rendered · fragments expanded" : "unified source"}</span></div>
      <div class="diffbox">${body}</div></div></div>`;
}

async function screenReview() {
  const pid = State.route.pid;
  const list = await GET(`/mgmt/prompts/${enc(pid)}/drafts`);
  const sel = State.route.q.draft || list.drafts[0]?.id;
  const cards = list.drafts.map((dr) => {
    const badge = dr.status === "approved"
      ? `<span class="pill live">approved</span>` : `<span class="pill warn">awaiting review</span>`;
    return `<div class="card pad" style="${dr.id === sel ? "border:1.5px solid var(--acc)" : ""};cursor:pointer" data-act="go" data-hash="#/p/${enc(pid)}/review?draft=${dr.id}">
      <div style="display:flex;gap:8px;align-items:center;font-size:12.5px;font-weight:700">${esc(dr.title || "v" + dr.version_number + " draft")}<span style="margin-left:auto">${badge}</span></div>
      <div style="font-size:11px;color:var(--mut);margin-top:4px">${esc(dr.author)} · v${dr.version_number} · approvals: ${dr.approvals.length}</div></div>`;
  }).join("") || '<div class="empty">No open drafts.</div>';

  let detail = '<div class="empty">Select a draft.</div>';
  if (sel) {
    const draft = await GET(`/mgmt/drafts/${sel}`);
    const lint = draft.lint || {};
    detail = `<div class="card"><div style="padding:14px 18px;border-bottom:1px solid var(--line2);display:flex;align-items:center;gap:10px;flex-wrap:wrap">
        <span style="font-size:13px;font-weight:700">${esc(draft.title || "Draft")}</span>
        <span class="mono muted" style="font-size:10.5px">${esc(draft.id)} · base ${esc(draft.base_sha || "")}</span>
        <div class="grow"></div>
        <button class="btn" data-act="approve" data-draft="${draft.id}">Approve ✓</button>
        <button class="btn live" data-act="commitReview" data-draft="${draft.id}">Commit</button></div>
      <div style="padding:12px 18px;font-size:10.5px;color:var(--faint)">${lint.status === "valid" ? "✓ validates — includes resolve, no cycles" : "⚠ " + esc(lint.error || "")}</div>
      <div class="render-out" style="margin:8px 18px 18px">${esc(draft.content || "")}</div></div>`;
  }
  el("main").innerHTML = `<div class="screen">
    <div class="h1row"><span class="h1 sm serif">Review</span><span class="sub">${esc(pid)} · reviewers judge what will be served</span></div>
    <div class="panelrow"><div style="flex:1 1 240px;display:flex;flex-direction:column;gap:10px">${cards}</div>
      <div style="flex:10 1 440px;min-width:0">${detail}</div></div></div>`;
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
       <span class="faint" style="font-size:11px">protected env: proposes for approval unless forced (approver ≠ proposer)</span>`
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

async function screenApprovals() {
  const d = await GET(`/mgmt/envs/${enc(State.env)}/approvals`);
  const rows = d.approvals.map((a) => {
    const c = a.change || {};
    const what = c.kind === "make_live"
      ? `make live <b>${esc(c.prompt_id)}</b> v${c.version} → <span class="mono">${esc((c.to_sha || "").slice(0, 7))}</span>`
      : esc(a.change ? JSON.stringify(a.change) : "");
    return `<tr class="grow-row">
      <td class="mono muted">#${a.id}</td>
      <td>${what}</td>
      <td><b>${esc(a.proposed_by)}</b></td>
      <td class="mono muted">${new Date(a.created_at).toLocaleString()}</td>
      <td style="text-align:right;white-space:nowrap">
        <button class="btn primary" data-act="approveChange" data-id="${a.id}">Approve ✓</button>
        <button class="btn" data-act="rejectChange" data-id="${a.id}">Reject</button></td></tr>`;
  }).join("");
  el("main").innerHTML = `<div class="screen">
    <div class="h1row"><span class="h1 sm serif">Approvals — <i>${esc(State.env)}</i></span>
      <span class="sub">pointer-class changes proposed in a protected environment</span></div>
    <div class="card" style="overflow-x:auto"><table class="grid">
      <thead class="ghead"><tr><th>#</th><th>Change</th><th>Proposed by</th><th>When</th><th></th></tr></thead>
      <tbody>${rows || '<tr><td colspan="5" class="empty">No pending approvals.</td></tr>'}</tbody></table></div>
    <div style="font-size:11px;color:var(--faint);margin-top:12px">A releaser other than the proposer approves; approving advances the live pointer immediately. Break-glass <span class="mono">force</span> releases bypass this and are gated to releaser.</div></div>`;
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
  prompts: screenPrompts, overview: screenOverview, editor: screenEditor, diff: screenDiff,
  review: screenReview, rules: screenRules, pointers: screenPointers, segments: screenSegments,
  approvals: screenApprovals, play: screenPlay, audit: screenAudit,
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
      go(`#/p/${enc(id)}/editor?draft=${draft.id}`);
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
      go(`#/p/${enc(pid)}/editor?draft=${created.id}`);
    } catch (e) { toast(errText(e), true); }
  },
  async edit(ds) {
    go(`#/p/${enc(State.route.pid)}/editor?v=${ds.v}`);
  },
  tc(ds) { window._tcActive = ds.name; document.querySelectorAll(".chip").forEach((c) => c.classList.toggle("active", c.dataset.name === ds.name)); doRender(); },
  async saveDraft() {
    try {
      const content = el("draftTa").value;
      const r = await PUT(`/mgmt/drafts/${window._draft.id}/content`, { content });
      window._draft = r;
      const lint = r.lint || {};
      el("lintLine").innerHTML = lint.status === "valid" ? "includes resolve ✓ · no cycles ✓" : "";
      if (r.variables) el("varLine").innerHTML = `variables: ${r.variables.required.map((n) => "<b>" + esc(n) + "</b>").join(" · ")}`;
      toast(lint.status === "valid" ? "Draft saved · lint clean" : "Saved · lint: " + (lint.error || "invalid"), lint.status !== "valid");
      doRender();
    } catch (e) { toast(errText(e), true); }
  },
  async commit() {
    try {
      const force = el("forceCommit")?.checked;
      // Identity (author) comes from the authenticated key, not the client.
      const r = await POST(`/mgmt/drafts/${window._draft.id}/commit`, { force });
      toast(`Committed ${r.sha} · v${r.version_number} · ${r.validation.status}`);
      go(`#/p/${enc(State.route.pid)}/overview`);
    } catch (e) {
      if (e.status === 412) toast("Review required before commit — approve in Review", true);
      else toast(errText(e), true);
    }
  },
  async approve(ds) {
    try {
      // The reviewer is the authenticated principal; you can't approve your own draft.
      await POST(`/mgmt/drafts/${ds.draft}/review`, { state: "approved" });
      toast("Approved — commit unlocked");
      render();
    } catch (e) { toast(errText(e), true); }
  },
  async commitReview(ds) {
    try {
      const r = await POST(`/mgmt/drafts/${ds.draft}/commit`, {});
      toast(`Committed ${r.sha} · v${r.version_number}`);
      go(`#/p/${enc(State.route.pid)}/overview`);
    } catch (e) {
      if (e.status === 412) toast("Needs an approval first", true);
      else toast(errText(e), true);
    }
  },
  async ruleStatus(ds) {
    try { await PATCH(`/mgmt/envs/${enc(State.env)}/rules/${ds.id}`, { status: ds.status }); toast(`Rule ${ds.id} → ${ds.status}`); render(); }
    catch (e) { toast(errText(e), true); }
  },
  rollback(ds) {
    openModal(`
      <h3>Roll back targeting</h3>
      <p class="hint">Restore <b>${esc(State.env)}</b>'s rule set to <span class="mono">rules_version ${esc(ds.rv)}</span>.
        Rules created after that version are archived (they stop serving). This is itself
        a change and bumps the rules_version — history stays append-only.</p>
      <div class="modal-actions">
        <button class="btn" data-act="closeModal">Cancel</button>
        <button class="btn primary" data-act="rollbackConfirm" data-rv="${esc(ds.rv)}">Roll back to rv${esc(ds.rv)}</button>
      </div>`);
  },
  async rollbackConfirm(ds) {
    try {
      const r = await POST(`/mgmt/envs/${enc(State.env)}/rollback`, { to_rules_version: parseInt(ds.rv) });
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
  async makeLive(ds) {
    try {
      // No force: a protected env returns "proposed" and the change waits in the
      // approval queue for a different releaser to approve.
      const r = await POST(`/mgmt/envs/${enc(State.env)}/pointers`, {
        prompt_id: State.route.pid, version_number: parseInt(ds.v), to_sha: ds.sha, comment: "make live via UI",
      });
      toast(r.status === "live" ? "Pointer advanced — tip is live"
                                : "Proposed — awaiting approval (see Approvals)");
      render();
    } catch (e) { toast(errText(e), true); }
  },
  async revert(ds) {
    try {
      const r = await POST(`/mgmt/envs/${enc(State.env)}/pointers`, {
        prompt_id: State.route.pid, version_number: parseInt(ds.v), to_sha: ds.sha, comment: "revert via UI",
      });
      toast(r.status === "live" ? "Reverted — pointer moved"
                                : "Revert proposed — awaiting approval");
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
  async approveChange(ds) {
    try {
      await POST(`/mgmt/envs/${enc(State.env)}/approvals/${ds.id}/approve`, {});
      toast("Approved — change is live");
      render();
    } catch (e) { toast(errText(e), true); }
  },
  async rejectChange(ds) {
    try {
      await POST(`/mgmt/envs/${enc(State.env)}/approvals/${ds.id}/reject`, {});
      toast("Rejected");
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
  if (t && t.dataset.act === "env") Actions.env(t.dataset, ev);
});
document.addEventListener("input", (ev) => {
  const t = ev.target.closest("[data-act]");
  if (t && t.dataset.act === "search") Actions.search(t.dataset, ev);
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
