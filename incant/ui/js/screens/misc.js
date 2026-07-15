/* screens: segments, playground, audit, and access (+ their modals) */
"use strict";

async function screenSegments() {
  const main = el("main");   // capture before any await (Issue B: stale screens write a detached node)
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
  main.innerHTML = `<div class="screen">
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
  const main = el("main");   // capture before any await (Issue B)
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
  main.innerHTML = `<div class="screen">
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
  const main = el("main");   // capture before any await (Issue B)
  await ensureWhoami();   // for the "you're signed in as" note
  let d;
  try {
    d = await GET(`/mgmt/principals`);
  } catch (e) {
    main.innerHTML = `<div class="screen"><div class="h1row"><span class="h1 sm serif">Access</span></div>
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
    const keys = p.keys.map((k) => {
      // Expiry is optional in the contract — hide it entirely when the field is absent.
      const exp = k.revoked ? "" : keyExpiryHtml(k);
      const actions = k.revoked ? "" :
        `<button type="button" class="link btn-bare" data-act="rotateKey" data-kid="${k.id}" data-name="${esc(p.name)}"
           style="margin-left:auto">rotate</button>
         <button type="button" class="link btn-bare" data-act="revokeKey" data-kid="${k.id}"
           style="color:var(--danger)">revoke</button>`;
      return `<div style="display:flex;gap:10px;align-items:center;font-size:11px">
        <span class="mono ${k.revoked ? "faint" : ""}">${esc(k.prefix)}…</span>
        ${k.revoked ? '<span class="pill warn">revoked</span>'
                    : '<span class="pill live">active</span>'}
        <!-- last_used_at is deliberately never written on the serving path (auth.py), so a
             null means "we don't track this", not "unused" — say so honestly. -->
        <span class="faint">${k.last_used_at ? "used " + ago(k.last_used_at) : "usage not tracked"}</span>
        ${exp}${actions}</div>`;
    }).join("") ||
      '<span class="faint" style="font-size:11px">no keys</span>';
    // Active-session count is optional in the contract — a pill only when the field is present
    // and > 0, so the card degrades gracefully before/without the backend field. Admins get a
    // revoke-sessions action next to it (the Access screen is admin-only, but gate it anyway).
    const sessCount = typeof p.sessions === "number" && p.sessions > 0 ? p.sessions : 0;
    const sessionUi = sessCount
      ? `<span class="pill acc">${sessCount} ${plural(sessCount, "session")}</span>` +
        (canRole("admin")
          ? `<button type="button" class="link btn-bare" data-act="revokeSessions" data-pid="${esc(p.id)}" data-name="${esc(p.name || "")}"
               style="color:var(--danger)">revoke sessions</button>`
          : "")
      : "";
    return `<div class="card pad">
      <div style="display:flex;gap:10px;align-items:center;flex-wrap:wrap">
        <span style="font-size:13.5px;font-weight:700">${esc(p.name || p.id)}</span>
        <span class="tag mut">${esc(p.kind)}</span>
        <span class="mono faint" style="font-size:11.5px">${esc(p.id)}</span>
        ${sessionUi}
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

  main.innerHTML = `<div class="screen">
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
// Key expiry indicator for the access rows. Absent expires_at → "" (field hidden, so the
// screen degrades gracefully before the backend contract lands). Past → warn pill; future
// → muted "expires in Nd".
function keyExpiryHtml(k) {
  if (!k || !k.expires_at) return "";
  const ms = new Date(k.expires_at).getTime() - Date.now();
  if (!(ms > 0)) return '<span class="pill warn">expired</span>';
  const days = Math.max(1, Math.ceil(ms / 86400000));
  return `<span class="faint">expires in ${days}d</span>`;
}
function _showKeyModal(key) {
  openModal(`
    <h3>API key created</h3>
    <p class="hint">Copy it now — it is <b>not recoverable</b>. This is the only time it's shown.</p>
    <input readonly data-act="selectAll" value="${esc(key)}"
      style="width:100%;font-family:'IBM Plex Mono',monospace;font-size:12px">
    <div class="modal-actions"><button class="btn primary" data-act="closeModal">Done</button></div>`);
}

function renderPlayResult(r, pinned) {
  const matched = typeof r.matched_rule === "string"
    ? r.matched_rule : `${r.matched_rule.scope}:${r.matched_rule.id}`;
  const versLines = Object.entries(r.versions).map(([k, v]) =>
    `${esc(k)} → v${v.version} · ${esc(String(v.commit).slice(0, 7))}${v.fallback ? " (fallback)" : ""}`).join("<br>");
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
  const main = el("main");   // capture before any await (Issue B)
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
  main.innerHTML = `<div class="screen">
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
