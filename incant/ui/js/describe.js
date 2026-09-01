/* describe — rule/serve prose, rule<->prompt joins, the ad-hoc context form, and the reusable clause builder */
"use strict";

// ── describe rules ───────────────────────────────────────────────────
const OPSYM = { eq: "=", neq: "≠", in: "∈", not_in: "∉", contains: "⊇", starts_with: "starts", ends_with: "ends",
  gt: ">", gte: "≥", lt: "<", lte: "≤", semver_gt: "semver>", semver_lt: "semver<", exists: "exists" };
function describeWhen(c) {
  if (c == null) return '<span class="muted">always</span>';
  if (c.all) return c.all.map(describeWhen).join(' <span class="muted">and</span> ');
  if (c.any) return c.any.map(describeWhen).join(' <span class="muted">or</span> ');
  if (c.not) return '<span class="muted">not</span> ' + describeWhen(c.not);
  if (c.flag) {
    const val = c.values ? c.values.join(", ") : c.value;
    if (c.op === "exists") return '<span class="codeinline">' + esc(c.flag) + " exists</span>";
    return '<span class="codeinline">' + esc(c.flag) + " " + (OPSYM[c.op] || c.op) + " " + esc(val) + "</span>";
  }
  return esc(JSON.stringify(c));
}
function describeServe(s) {
  if (!s) return "";
  if (s.version != null) return "<b>v" + s.version + " @ " + (s.at || "live") + "</b>";
  return esc(JSON.stringify(s));
}

// Which active rules apply to a prompt. Paused/archived excluded.
function activeRulesFor(rules, pid) {
  return (rules || []).filter((r) => r.status === "active" && r.prompt_id === pid);
}
// What a rule's serve targets: {version, tip}. Null when nothing concrete is served.
function serveTarget(serve) {
  if (!serve) return null;
  if (serve.version != null) return { version: serve.version, tip: serve.at === "tip" };
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
    out.push({ rule: r, version: t.version, tip: t.tip });
  }
  return out;
}

// ── plain-language rule helpers (for "Who sees what") ────────────────
function ordinal(n) {
  const s = ["th", "st", "nd", "rd"], v = n % 100;
  return n + (s[(v - 20) % 10] || s[v] || s[0]);
}
// A rule's serve target in a short plain phrase — returns trusted HTML (numbers are
// safe). Used in the "rules that will be ignored" list.
function serveTargetPlain(serve) {
  const t = serveTarget(serve);
  if (!t) return "the default";
  if (t.tip) return `latest draft of Version ${t.version}`;
  return `Version ${t.version}`;
}
// The prose body line under an ordinal rule row: "See Version N — who it's for".
// Trusted HTML; describeWhen already esc()s its values.
function ruleServeLine(r) {
  const t = serveTarget(r.serve);
  if (t && t.tip)
    return `See the <b>latest unpublished draft of Version ${t.version}</b> <span class="muted">— how you try changes before publishing them for everyone</span>`;
  if (t)
    return `See <b>Version ${t.version}</b> <span class="muted">— ${describeWhen(r.when)}</span>`;
  return `<span class="muted">${describeWhen(r.when)} → ${describeServe(r.serve)}</span>`;
}

// ── ad-hoc test context: structured form ─────────────────────────────
// The flags targeting actually consults for this prompt (and its includes):
// collected from active rules' clauses, each with the candidate values rules compare
// against.
function targetingFlags(rules, pids) {
  const found = new Map();
  const add = (name, vals) => {
    if (!found.has(name)) found.set(name, new Set());
    (vals || []).forEach((v) => found.get(name).add(String(v)));
  };
  const walk = (c, depth) => {
    if (!c || depth > 6) return;
    if (c.all) return c.all.forEach((x) => walk(x, depth + 1));
    if (c.any) return c.any.forEach((x) => walk(x, depth + 1));
    if (c.not) return walk(c.not, depth + 1);
    if (c.flag) add(c.flag, c.values || (c.value != null ? [c.value] : []));
  };
  for (const r of rules || []) {
    if (r.status !== "active") continue;
    if (r.prompt_id && !pids.includes(r.prompt_id)) continue;
    walk(r.when, 0);
  }
  return [...found.entries()].map(([name, vals]) => ({ name, values: [...vals] }));
}
// Lazily build the form's value maps: one entry per template variable and per
// targeting flag, all editable in place.
function ensureCtx(dp) {
  if (dp.ctx) return dp.ctx;
  const vars = {};
  for (const v of dp.varDefs || [])
    vars[v.name] = v.default != null ? (typeof v.default === "string" ? v.default : JSON.stringify(v.default))
      : v.type === "list" ? "[]" : v.type === "dict" ? "{}" : "";
  const flags = {};
  for (const f of dp.flagDefs || []) flags[f.name] = "";
  dp.ctx = { flags, vars };
  syncCtxJson(dp);
  return dp.ctx;
}
function coerceCtxVal(raw, type) {
  const s = String(raw).trim();
  if (s === "") return undefined;
  if (type === "list" || type === "dict") { try { return JSON.parse(s); } catch { return raw; } }
  if (type === "int" || type === "float" || type === "number") { const n = Number(s); return isNaN(n) ? raw : n; }
  if (type === "bool") return s === "true" || s === "1" || s === "yes";
  if (type == null) {   // flags are untyped — numbers/booleans should compare as such
    if (/^-?\d+(\.\d+)?$/.test(s)) return Number(s);
    if (s === "true" || s === "false") return s === "true";
  }
  return raw;
}
// Keep the JSON mirrors in sync — they feed the render call, save-as-test-context,
// and the "edit as JSON" fallback. Empty variables stay as "" (renders immediately);
// empty flags are omitted (the flag is simply absent from the request).
function syncCtxJson(dp) {
  const types = {};
  (dp.varDefs || []).forEach((v) => { types[v.name] = v.type; });
  const vars = {}, flags = {};
  for (const [k, v] of Object.entries(dp.ctx.vars)) {
    const c = coerceCtxVal(v, types[k] || "string");
    vars[k] = c === undefined ? "" : c;
  }
  for (const [k, v] of Object.entries(dp.ctx.flags)) {
    const c = coerceCtxVal(v, null);
    if (c !== undefined) flags[k] = c;
  }
  dp.customVars = JSON.stringify(vars, null, 2);
  dp.customFlags = JSON.stringify(flags, null, 2);
}
function ctxFormHtml(dp) {
  ensureCtx(dp);
  if (dp.ctxJson) {
    return `
      <div class="field" style="margin-bottom:0"><label>Variables (JSON)</label>
        <textarea id="tcVars" data-act="tcVarsInput" spellcheck="false" style="min-height:72px">${esc(dp.customVars)}</textarea></div>
      <div class="field" style="margin-bottom:0"><label>Flags (JSON)</label>
        <textarea id="tcFlags" data-act="tcFlagsInput" spellcheck="false" style="min-height:40px">${esc(dp.customFlags)}</textarea></div>
      <div style="display:flex;gap:14px;align-items:center;margin-top:6px">
        <button type="button" class="link btn-bare" style="font-size:12px" data-act="ctxJsonToggle">Back to the simple form</button>
        <button type="button" class="link btn-bare" style="font-size:12px" data-act="saveTestContext">Save as a test context…</button></div>`;
  }
  const varRows = (dp.varDefs || []).map((v) => `
    <div class="ctx-row"><label for="ctxv-${esc(v.name)}"><span class="mono">${esc(v.name)}</span>${v.required ? ' <b class="ctx-req">required</b>' : ""}${v.type && v.type !== "string" ? ` <span class="faint">${esc(v.type)}</span>` : ""}</label>
      <input id="ctxv-${esc(v.name)}" data-act="ctxInput" data-kind="var" data-name="${esc(v.name)}" value="${esc(dp.ctx.vars[v.name] ?? "")}" spellcheck="false" autocomplete="off"></div>`).join("")
    || '<div class="faint" style="font-size:12px">This template has no variables.</div>';
  const flagRows = (dp.flagDefs || []).map((f) => {
    const dl = f.values.length ? `<datalist id="ctxdl-${esc(f.name)}">${f.values.map((v) => `<option value="${esc(v)}">`).join("")}</datalist>` : "";
    return `<div class="ctx-row"><label for="ctxf-${esc(f.name)}"><span class="mono">${esc(f.name)}</span>${f.values.length ? ` <span class="faint">try: ${esc(f.values.join(" · "))}</span>` : ""}</label>
      <input id="ctxf-${esc(f.name)}"${f.values.length ? ` list="ctxdl-${esc(f.name)}"` : ""} data-act="ctxInput" data-kind="flag" data-name="${esc(f.name)}" value="${esc(dp.ctx.flags[f.name] ?? "")}" spellcheck="false" autocomplete="off">${dl}</div>`;
  }).join("");
  return `
    <div class="groupname" style="margin:2px 0 6px">Variables</div>${varRows}
    <div class="groupname" style="margin:12px 0 2px">Who's asking</div>
    <div class="faint" style="font-size:11.5px;margin:0 0 6px">the flags targeting checks for this prompt — set them to preview what that person gets</div>
    ${flagRows || '<div class="faint" style="font-size:12px">No targeting rules reference flags for this prompt.</div>'}
    <div class="ctx-row" style="flex-direction:row;gap:8px;margin-top:4px;align-items:center">
      <input id="ctxNewFlag" placeholder="another flag…" spellcheck="false" autocomplete="off" style="max-width:150px" aria-label="Add another flag">
      <button type="button" class="btn sm" data-act="ctxAddFlag">add</button></div>
    <div style="display:flex;gap:14px;align-items:center;margin-top:8px">
      <button type="button" class="link btn-bare" style="font-size:12px" data-act="ctxJsonToggle">Edit as JSON</button>
      <button type="button" class="link btn-bare" style="font-size:12px" data-act="saveTestContext">Save as a test context…</button></div>`;
}

// ══ targeting composer + reusable clause builder ═════════════════════════════
// The clause builder drives the rule composer's "People who match…". State is a plain
// object {rows, advanced, advancedJson}; each row is {flag, op, value}. Rows AND
// together. Shapes the row builder can't express (any / not / nested all) fall back to a
// raw-JSON textarea (advanced=true). window._composer holds open-surface state (the
// window._dp idiom); the html builders are pure functions for round-trip testing.

const CLAUSE_OPS = [
  ["eq", "is"], ["neq", "is not"], ["in", "is one of"], ["not_in", "is not one of"],
  ["contains", "contains"], ["starts_with", "starts with"], ["ends_with", "ends with"],
  ["gt", ">"], ["gte", "≥"], ["lt", "<"], ["lte", "≤"],
  ["semver_gt", "semver >"], ["semver_lt", "semver <"], ["exists", "exists"],
];

function isLeafClause(c) { return !!c && c.flag !== undefined; }
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
  let value = "";
  if (c.values !== undefined) value = (c.values || []).join(", ");
  else if (c.value !== undefined) value = String(c.value);
  return { flag: c.flag || "", op: c.op || "eq", value };
}
function rowToClause(row) {
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
function emptyFlagRow() { return { flag: "", op: "eq", value: "" }; }
function newCb(when) {
  const rows = whenToRows(when);
  if (rows == null) return { rows: [], advanced: true, advancedJson: JSON.stringify(when, null, 2) };
  return { rows, advanced: false, advancedJson: "" };
}

function opOptions(sel) {
  return CLAUSE_OPS.map(([v, label]) =>
    `<option value="${v}"${v === sel ? " selected" : ""}>${esc(label)}</option>`).join("");
}
// cb: {rows, advanced, advancedJson}; prefix: DOM-id namespace.
function cbHtml(cb, prefix) {
  if (cb.advanced) {
    return `<div class="cb" id="${prefix}-wrap">
      <div class="cb-adv-note">Advanced condition — a shape the simple builder can't show (any-of / not / nesting). Edit the raw JSON.</div>
      <textarea class="cb-adv" id="${prefix}-adv" spellcheck="false">${esc(cb.advancedJson || "")}</textarea>
      <div class="cb-foot"><button type="button" class="link btn-bare" data-act="cbSimple" data-prefix="${prefix}">↺ back to the simple builder</button></div>
    </div>`;
  }
  const rows = cb.rows.map((row, i) => cbRowHtml(row, prefix, i)).join("");
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
    <div class="cb-hint">Rows combine with <b>AND</b>. Need OR or NOT? edit as JSON (<span class="mono">any</span> / <span class="mono">not</span>).</div>
  </div>`;
}
function cbRowHtml(row, prefix, i) {
  const p = `${prefix}-r${i}`;
  const isExists = row.op === "exists";
  const valPh = (row.op === "in" || row.op === "not_in") ? "comma,separated,values" : "value";
  // data-sugg opts the inputs into the §7 typeahead (suggest.js): flag names known to
  // the environment, then values traffic has sent for the flag named beside them.
  const controls = `<input class="cb-flag" id="${p}-flag" value="${esc(row.flag)}" placeholder="flag name" spellcheck="false" autocomplete="off" data-sugg="flag" aria-autocomplete="list">
      <select class="cb-sel" id="${p}-op" data-act="cbOp" data-prefix="${prefix}" data-ri="${i}">${opOptions(row.op)}</select>
      ${isExists ? "" : `<input class="cb-val grow" id="${p}-val" value="${esc(row.value)}" placeholder="${valPh}" spellcheck="false" autocomplete="off" data-sugg="value" data-flag-input="${p}-flag" data-op-input="${p}-op" aria-autocomplete="list">`}`;
  return `<div class="cb-row">${controls}
    <button type="button" class="cb-del btn-bare" data-act="cbDel" data-prefix="${prefix}" data-ri="${i}" aria-label="Remove condition">✕</button></div>`;
}
// Read the live DOM values of one builder back into its state.
function cbSync(cb, prefix) {
  if (cb.advanced) { const ta = el(`${prefix}-adv`); if (ta) cb.advancedJson = ta.value; return; }
  cb.rows.forEach((row, i) => {
    const p = `${prefix}-r${i}`;
    const f = el(`${p}-flag`); if (f) row.flag = f.value;
    const o = el(`${p}-op`); if (o) row.op = o.value;
    const v = el(`${p}-val`); if (v) row.value = v.value;
  });
}
// Resolve a builder prefix to its state object + the host surface that owns it.
function cbResolve(prefix) {
  if (prefix === "co") return { cb: window._composer && window._composer.cb, host: "composer" };
  return { cb: null, host: null };
}
function cbHostSync(host) { if (host === "composer") composerSync(); }
function cbHostRender(host) { if (host === "composer") renderComposer(); }
