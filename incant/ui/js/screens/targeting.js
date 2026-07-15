/* screen: targeting — who-sees-what rules, the rule composer, and the audience tester */
"use strict";

// Stashed by screenRules so the "turn targeting off" confirm modal can list the rules.
let _rulesData = null;

async function screenRules() {
  const main = el("main");   // capture before any await (Issue B)
  const env = State.env;
  const pid = State.route.pid;
  // Both reads retry scoped to this prompt's project on a 403, so a project-scoped operator
  // can still manage (and see the history of) the rules that govern their own prompt.
  const [d, rv] = await Promise.all([
    fetchEnvRules(env, pid),
    fetchEnvRevisions(env, pid, 25),
  ]);
  _rulesData = d;   // stashed for the "turn targeting off" confirm modal
  // If the rule list itself couldn't be loaded (outage), the rows below would read as
  // "No rules yet" — warn instead so an empty screen isn't mistaken for empty targeting.
  const rulesWarn = rulesUnavailableNote(d.status);
  // Kill semantics are per-prompt; the header toggle governs the route prompt or,
  // on the env-wide screen, the first prompt-scoped rule's prompt.
  const defaultPid = pid || (d.rules.find((r) => r.prompt_id)?.prompt_id) || null;
  const defV = defaultPid ? d.defaults[defaultPid] : null;
  const killEngaged = !!(defaultPid && d.kills[defaultPid]);
  // Targeting mutations (new rule / edit / reorder / toggle / rollback) need operator+.
  // The "Who sees what" screen itself stays viewable for everyone — only the controls hide.
  const canTarget = canRole("operator");

  // ── header: "Up to date" status + the Targeting toggle (kill reframed) ──
  const rightControls = `${statusLine("live", "Up to date")}${defaultPid && canTarget
    ? `<span class="faint" style="margin:0 4px">·</span>
       <span style="display:inline-flex;align-items:center;gap:8px">
         <span class="muted" style="font-size:12px;font-weight:600">Targeting</span>
         <button type="button" role="switch" aria-checked="${!killEngaged}" aria-label="Targeting for ${esc(defaultPid)}" class="toggle ${killEngaged ? "" : "on"}" data-act="targetingToggle" data-pid="${esc(defaultPid)}" data-engaged="${killEngaged}"><span class="knob"></span></button>
       </span>` : ""}`;

  const killBanner = killEngaged ? `<div class="banner danger">
      <span style="font-size:15px">⏻</span>
      <span style="font-size:12.5px;font-weight:700">Targeting is off for ${esc(defaultPid)} — everyone gets the default. Rules below are being ignored.</span>
      ${canTarget ? `<button type="button" class="link btn-bare" style="margin-left:auto;color:var(--danger)" data-act="kill" data-pid="${esc(defaultPid)}" data-engage="false">Turn targeting back on</button>` : ""}</div>` : "";

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
      ${canTarget ? `<div class="ord-actions">${up}${down}
        <button type="button" class="link btn-bare" data-act="ruleEdit" data-id="${esc(r.id)}">Edit</button>
        <button type="button" class="link btn-bare" data-act="ruleDup" data-id="${esc(r.id)}">Duplicate</button>
        ${stopTest}${statusToggle}</div>` : ""}</div>`;
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
        : (canTarget ? `<button class="btn" data-act="rollback" data-rv="${r.rules_version}">▸ Go back to here</button>` : "")}</td>
    </tr>`).join("") || '<tr><td colspan="6" class="empty">No targeting changes yet.</td></tr>';

  // Audience tester renders through the real serving path for one prompt; reset its
  // cached result when the prompt in focus changes.
  const testerPid = pid || defaultPid;
  if (window._audience && window._audience.pid !== testerPid) window._audience = null;

  main.innerHTML = `<div class="screen">
    <div style="display:flex;align-items:center;gap:12px;margin-bottom:16px;flex-wrap:wrap">
      <span class="page-h1">Who sees what</span>
      ${d.protected ? pill("warn", `${esc(env)} · protected`) : ""}
      <div class="grow"></div>
      ${canTarget ? `<button class="btn primary sm" data-act="ruleNew">＋ New rule</button>
      <span class="faint" style="font-size:11px">·</span>` : ""}${rightControls}</div>
    ${rulesWarn}
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
    // Reuse the screen's stashed rules if present; otherwise fetch, retrying scoped to the
    // target prompt's project on a 403 (a project-scoped operator composing their own rule).
    rulesData = _rulesData || await fetchEnvRules(env, targetPid);
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
