/* main — screen map, Actions, router, render loop, event delegation, boot */
"use strict";

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

const SCREENS = {
  prompts: screenPrompts, overview: screenOverview, draft: screenDraft, compare: screenCompare,
  rules: screenRules, pointers: screenPointers, segments: screenSegments,
  play: screenPlay, audit: screenAudit, access: screenAccess,
};

// ── account sessions (menu block) ────────────────────────────────────
// Sign-out tail shared by "Sign out" and "Sign out everywhere": drop local state, dismiss the
// modal, land on the sign-in card (render() sees no session), and toast honestly. `reached` is
// false when the DELETE failed (offline / non-2xx) — we still clear locally, but the HttpOnly
// cookie may outlive us, so we say so rather than the reassuring "Signed out".
function finishSignOut(reached) {
  clearSession();
  closeModal();
  if (reached) toast("Signed out");
  else toast("Couldn't reach the server — your session may still be active elsewhere. It expires on its own.", true);
  render();
}
// One session row: a "this device" pill when current, else when-signed-in / last-seen, with a
// muted expiry (keyExpiryHtml formats the optional expires_at exactly as the Access screen does).
function sessionRowHtml(s) {
  const label = s.current
    ? `<span class="pill live">this device</span>`
    : `<span>signed in ${esc(ago(s.created_at))} · last seen ${esc(ago(s.last_seen_at))}</span>`;
  return `<div style="display:flex;gap:8px;align-items:center;font-size:12px;padding:3px 0">
    ${label}<div class="grow"></div>${keyExpiryHtml({ expires_at: s.expires_at })}</div>`;
}
// The account-menu "Sessions" block. `sessions`: array = loaded, null = load failed,
// "loading" = in flight. The destructive "Sign out everywhere" appears once past loading.
function acctSessionsInner(sessions) {
  const title = `<div class="groupname" style="margin-bottom:8px">Signed-in sessions</div>`;
  let body;
  if (sessions === "loading") body = `<div class="faint" style="font-size:12px">Loading…</div>`;
  else if (sessions == null) body = `<div class="faint" style="font-size:12px">Couldn't load your other sessions.</div>`;
  else if (!sessions.length) body = `<div class="faint" style="font-size:12px">No active sessions.</div>`;
  else body = sessions.map(sessionRowHtml).join("");
  const everywhere = `<button type="button" class="link btn-bare" data-act="signOutEverywhere"
    style="color:var(--danger);margin-top:10px">Sign out everywhere</button>`;
  return title + body + (sessions === "loading" ? "" : everywhere);
}
// Fill the account menu's sessions container once GET /auth/sessions resolves. A fetch failure
// degrades to a muted note (the everywhere action still shows); a closed menu is a no-op.
async function loadAcctSessions() {
  let sessions = null;
  try { const r = await GET("/auth/sessions"); sessions = (r && r.sessions) || []; }
  catch (_) { sessions = null; }
  const host = el("acctSessions");
  if (host) host.innerHTML = acctSessionsInner(sessions);
}

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
  // Shared by the sign-in card and the "Sign in with a different key…" modal — whichever
  // input is present. Exchanges the API key for a server-side session (HttpOnly cookie); the
  // key is posted once and never stored in the browser.
  async setToken() {
    let val = "";
    for (const id of ["signinKey", "switchKeyIn"]) {
      const e = document.getElementById(id);
      if (e && e.value != null && String(e.value).trim()) { val = String(e.value).trim(); break; }
    }
    const errEl = el("signinErr") || el("switchKeyErr");
    if (errEl) errEl.textContent = "";
    if (!val) { if (errEl) errEl.textContent = "Enter an API key."; else toast("Enter an API key", true); return; }
    // "Stay signed in" (default OFF) maps to the session lifetime: checked → a 30-day
    // persistent cookie, unchecked → a session-only cookie the browser drops on close.
    let remember = false;
    for (const id of ["signinRemember", "switchRemember"]) {
      const c = document.getElementById(id);
      if (c) { remember = !!c.checked; break; }
    }
    let session;
    try {
      session = await POST("/auth/session", { key: val, remember });
    } catch (e) {
      const msg = e && e.status === 429 ? "Too many attempts — wait a minute"
        : e && e.status === 401 ? "That key didn't work — check it and try again"
        : errText(e);
      if (errEl) errEl.textContent = msg; else toast(msg, true);
      return;
    }
    applySession(session);   // caches the CSRF token + identity; never touches storage
    // Clear the just-used key from the input so it doesn't linger in the DOM.
    for (const id of ["signinKey", "switchKeyIn"]) { const e = document.getElementById(id); if (e) e.value = ""; }
    // Re-fetch environments: a prior signed-out boot() left State.envs a bare fallback with
    // no `protected` flags — stale until re-read, which would hide PROTECTED badges and skip
    // the type-to-confirm the server still enforces.
    try {
      const e = await GET("/mgmt/envs");
      State.envs = e.environments;
      if (!State.envs.find((x) => x.id === State.env)) State.env = State.envs[0]?.id || "prod";
    } catch (_) { /* session may still be bad — the 401 screen will handle it */ }
    closeModal();
    toast("Signed in");
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
      <div id="acctSessions" style="border-top:1px solid var(--line2);padding-top:12px;margin-bottom:14px">${acctSessionsInner("loading")}</div>
      <div style="display:flex;flex-direction:column;gap:8px;border-top:1px solid var(--line2);padding-top:12px;align-items:flex-start">
        <button type="button" class="link btn-bare" data-act="switchKey">Sign in with a different key…</button>
        <button type="button" class="link btn-bare" data-act="signOut" style="color:var(--danger)">Sign out</button>
        <a href="#/access" data-act="go" data-hash="#/access" style="font-weight:600">Manage access →</a></div>
      <div class="modal-actions"><button class="btn" data-act="closeModal">Close</button></div>`);
    loadAcctSessions();   // async fill-in — GET /auth/sessions after the menu is on screen
  },
  switchKey() { openSwitchKeyModal(); },
  // "Sign out" — ends the server-side session (DELETE carries the CSRF header via api.js),
  // then drops the cached identity so the 401 sign-in card takes over. A failed DELETE still
  // clears locally, but the toast stays honest (finishSignOut): the cookie may outlive us.
  async signOut() {
    let reached = true;
    try { await api("DELETE", "/auth/session"); } catch (_) { reached = false; }
    finishSignOut(reached);
  },
  // "Sign out everywhere" (account menu Sessions block) — kills every session for the caller,
  // this one included. Routes through a confirm modal; the DELETE then clears local state.
  signOutEverywhere() {
    openModal(`
      <h3>Sign out everywhere</h3>
      <p class="hint">Ends every signed-in session for your account, including this one.</p>
      <div class="modal-actions">
        <button class="btn" data-act="closeModal">Cancel</button>
        <button class="btn danger" data-act="signOutEverywhereConfirm">Sign out everywhere</button></div>`);
  },
  async signOutEverywhereConfirm() {
    let reached = true;
    try { await api("DELETE", "/auth/sessions"); } catch (_) { reached = false; }
    finishSignOut(reached);   // honest-failure toast, same as single sign-out
  },
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
  // Type-to-confirm inputs (locked-env modals + publish impact modal): enable the target
  // button (data-btn) only on an exact token (data-token) match. One delegated input act
  // replaces the two old inline oninput handlers.
  confirmInput(ds, ev) {
    const b = document.getElementById(ds.btn);
    if (b) b.disabled = (String(ev.target.value || "").trim() !== ds.token);
  },
  // Review comment box: enable the Comment button (data-btn) while there's non-empty text.
  commentInput(ds, ev) {
    const b = document.getElementById(ds.btn);
    if (b) b.disabled = !String(ev.target.value || "").trim();
  },
  // "Show key once" input: select-all on click so the one-time secret is easy to copy.
  selectAll(ds, ev) { if (ev.target && ev.target.select) ev.target.select(); },
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
  // Explicit draft creation from the read-only "start" state: create the draft (the same
  // POST the route used to fire automatically) then deep-link into the live editor.
  async startDraft(ds) {
    const pid = State.route.pid, v = parseInt(ds.v);
    try {
      const d = await POST(`/mgmt/prompts/${enc(pid)}/drafts`, {
        version_number: v, seed_from_version: v, title: "Draft v" + v });
      go(`#/p/${enc(pid)}/draft?draft=${d.id}`);
    } catch (e) { toast(errText(e), true); }
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
  // Autosave conflict (409 stale_write): "Load the newer version" replaces the editor
  // with the server's current content and re-chains; "Keep mine" resends on top of it.
  conflictLoadNewer() { resolveAutosaveConflict("load"); },
  conflictKeepMine() { resolveAutosaveConflict("keep"); },
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
  ctxInput(ds, ev) {
    const dp = window._dp; ensureCtx(dp);
    dp.ctx[ds.kind === "flag" ? "flags" : "vars"][ds.name] = ev.target.value;
    syncCtxJson(dp);
    clearTimeout(window._tcTimer);
    window._tcTimer = setTimeout(() => {
      if ((State.route.q.tab || "write") === "review") loadReviewRendered();
      else doRenderDraft();
    }, 500);
  },
  ctxAddFlag() {
    const dp = window._dp, inp = el("ctxNewFlag");
    const name = (inp && inp.value || "").trim();
    if (!name) return;
    if (!(dp.flagDefs || []).some((f) => f.name === name)) dp.flagDefs.push({ name, values: [] });
    ensureCtx(dp);
    if (dp.ctx.flags[name] == null) dp.ctx.flags[name] = "";
    const w = el("ctxFormWrap"); if (w) w.innerHTML = ctxFormHtml(dp);
    const f = el("ctxf-" + name); if (f) f.focus();
  },
  ctxJsonToggle() {
    const dp = window._dp; ensureCtx(dp);
    if (dp.ctxJson) {
      // Leaving JSON mode — parse the mirrors back into the form's value maps and
      // surface any keys the JSON added as form rows.
      try {
        const vars = (dp.customVars || "").trim() ? JSON.parse(dp.customVars) : {};
        const flags = (dp.customFlags || "").trim() ? JSON.parse(dp.customFlags) : {};
        dp.ctx.vars = {}; dp.ctx.flags = {};
        for (const [k, v] of Object.entries(vars)) dp.ctx.vars[k] = typeof v === "string" ? v : JSON.stringify(v);
        for (const [k, v] of Object.entries(flags)) dp.ctx.flags[k] = typeof v === "string" ? v : JSON.stringify(v);
        for (const k of Object.keys(flags)) if (!dp.flagDefs.some((f) => f.name === k)) dp.flagDefs.push({ name: k, values: [] });
        for (const k of Object.keys(vars)) if (!dp.varDefs.some((v2) => v2.name === k)) dp.varDefs.push({ name: k, type: "string", required: false });
      } catch { return toast("Fix the JSON first", true); }
    }
    dp.ctxJson = !dp.ctxJson;
    const w = el("ctxFormWrap"); if (w) w.innerHTML = ctxFormHtml(dp);
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
    const dp = window._dp;
    dp.reviewTc = ds.name;
    document.querySelectorAll("#reviewTcChips .chip").forEach((c) => {
      const on = c.dataset.name === ds.name;
      c.classList.toggle("active", on); c.setAttribute("aria-pressed", String(on));
    });
    const w = el("reviewCtxWrap");
    if (w) w.innerHTML = ds.name === "__custom"
      ? `<div id="ctxFormWrap" style="padding:0 0 10px;display:flex;flex-direction:column">${ctxFormHtml(dp)}</div>` : "";
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
    // One atomic act: move the pointer AND archive the checked test rules server-side. The
    // pointer can no longer move while an archive fails; the response reports how many landed.
    const archive_rule_ids = p.tipRules.filter((r) => p.removeRule[r.id]).map((r) => r.id);
    try {
      const res = await POST(`/mgmt/envs/${enc(p.env)}/publish`, {
        prompt_id: p.pid, version_number: parseInt(p.v), to_sha: p.toSha,
        comment: p.mode === "revert" ? "Reverted to an earlier state via the UI" : "Published via the UI",
        confirm: p.pid, archive_rule_ids,
      });
      closeModal();
      const removed = res.archived || 0;
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
      <div class="field"><label>Expires in (days) <span style="text-transform:none;font-weight:400">· optional, blank = never</span></label>
        <input id="auExpires" type="number" min="1" placeholder="never" spellcheck="false"></div>
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
      const body = {
        principal_name: name, role: el("auRole").value,
        project_id: el("auProject").value || null, environment_id: el("auEnv").value || null,
      };
      const days = parseInt((el("auExpires") && el("auExpires").value) || "", 10);
      if (!isNaN(days) && days > 0) body.expires_in_days = days;
      const r = await POST(`/mgmt/keys`, body);
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
  // Admin revoke of every session a principal holds (the "revoke sessions" action next to the
  // session-count pill on the Access screen). Confirm → DELETE → toast + re-render (fresh count).
  revokeSessions(ds) {
    openModal(`
      <h3>Revoke sessions</h3>
      <p class="hint">Signs <b>${esc(ds.name || ds.pid)}</b> out of every device immediately — all of their active sessions end. They'll need to sign in again with a valid key. This can't be undone.</p>
      <div class="modal-actions">
        <button class="btn" data-act="closeModal">Cancel</button>
        <button class="btn danger" data-act="revokeSessionsConfirm" data-pid="${esc(ds.pid)}" data-name="${esc(ds.name || "")}">Revoke sessions</button>
      </div>`);
  },
  async revokeSessionsConfirm(ds) {
    try {
      await api("DELETE", `/mgmt/principals/${enc(ds.pid)}/sessions`);
      closeModal();
      toast("Sessions revoked");
      render();
    } catch (e) { toast(errText(e), true); }
  },
  issueKey(ds) {
    openModal(`
      <h3>Issue key</h3>
      <p class="hint">Issues a new API key for this user. It's shown once at creation — copy it immediately.</p>
      <div class="field"><label>Expires in (days) <span style="text-transform:none;font-weight:400">· optional, blank = never</span></label>
        <input id="ikExpires" type="number" min="1" placeholder="never" spellcheck="false"></div>
      <div class="modal-actions">
        <button class="btn" data-act="closeModal">Cancel</button>
        <button class="btn primary" data-act="issueKeyConfirm" data-pid="${esc(ds.pid)}">Issue key</button>
      </div>`);
  },
  async issueKeyConfirm(ds) {
    try {
      const body = {};
      const days = parseInt((el("ikExpires") && el("ikExpires").value) || "", 10);
      if (!isNaN(days) && days > 0) body.expires_in_days = days;
      const r = await POST(`/mgmt/principals/${enc(ds.pid)}/keys`, body);
      _showKeyModal(r.key);   // openModal replaces the issue modal with the one-time key
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
  rotateKey(ds) {
    openModal(`
      <h3>Rotate key</h3>
      <p class="hint">Issues a replacement key and revokes this one immediately — update anything using it.</p>
      <div class="modal-actions">
        <button class="btn" data-act="closeModal">Cancel</button>
        <button class="btn primary" data-act="rotateKeyConfirm" data-kid="${esc(ds.kid)}">Rotate key</button>
      </div>`);
  },
  async rotateKeyConfirm(ds) {
    try {
      // Rotate issues a new key + revokes the old one server-side; the response is the
      // same shape as issuance, so surface the new secret via the one-time show-key modal.
      const r = await POST(`/mgmt/keys/${enc(ds.kid)}/rotate`, {});
      _showKeyModal(r.key);
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
    // One atomic batch: the priority-shift plan first, then the new/edited rule. A failure
    // no longer half-applies the renumber (server rolls the whole request back).
    const rules = plan.map((p) => ruleUpsertBody(p.rule, p.priority)).concat([base]);
    try {
      await POST(`/mgmt/envs/${enc(State.env)}/rules/batch`, { rules });
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
      // Both swapped priorities in one atomic batch — a failure after the first no longer
      // leaves the two rules at colliding priorities.
      await POST(`/mgmt/envs/${enc(State.env)}/rules/batch`, { rules: payloads });
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
      // Pointer move + archive of the redundant test rule in one atomic request.
      await POST(`/mgmt/envs/${enc(State.env)}/publish`, {
        prompt_id: ds.pid, version_number: parseInt(ds.v), to_sha: ds.sha,
        comment: "Published via stop-test-and-publish", confirm: ds.pid,
        archive_rule_ids: [ds.id],
      });
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
  // Invariant: screens write through the #main node captured at entry; render() replaces
  // #main's parent shell (a fresh #main) on every navigation, so a superseded async screen
  // — or this catch — holds a now-detached node and its late write lands harmlessly. We
  // capture #main here, BEFORE fn() runs, so the catch writes through the same node.
  const main = el("main");
  // No session and no bearer → straight to the sign-in card. Firing authenticated fetches
  // unauthenticated would just 401 (and, repeated, look like a brute-force attempt to the
  // server's auth throttle).
  if (!State.token && !State.session) {
    main.innerHTML = signInCard();
    return;
  }
  ensureWhoami();   // lazy whoami → fills the account chip in place
  const fn = SCREENS[State.route.name] || screenPrompts;
  fn().catch((e) => {
    if (e && e.status === 401) {
      // Session expired mid-use → drop it so the guard above holds next render, and swap the
      // dedicated sign-in card into the screen body; the sidebar stays.
      State.session = null; State.csrf = "";
      main.innerHTML = signInCard();
    } else {
      main.innerHTML = `<div class="empty">⚠ ${esc(errText(e))}</div>`;
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
  if (act === "search" || act === "draftInput" || act === "tcVarsInput" || act === "tcFlagsInput" || act === "rolloutWeightInput" || act === "ctxInput" || act === "confirmInput" || act === "commentInput") Actions[act](t.dataset, ev);
});
document.addEventListener("keydown", (ev) => {
  if (ev.key === "Escape") { closeModal(); Actions.navClose(); }
});
// Enter-to-submit for single-line inputs that name a button via data-enter (sign-in +
// switch-key). Replaces the inline onkeydown handlers so nothing runs inline under CSP.
document.addEventListener("keydown", (ev) => {
  if (ev.key !== "Enter") return;
  const t = ev.target && ev.target.closest && ev.target.closest("[data-enter]");
  if (!t) return;
  const b = document.getElementById(t.dataset.enter);
  if (b) { ev.preventDefault(); b.click(); }
});
// Roving-tabindex arrow-key movement for any [role="tablist"] group (draft + compare +
// the diff-mode sub-tabs). Left/Right cycle, Home/End jump; focus moves, Enter/Space
// activates via the native <button>/<a>. One delegated handler for every tablist.
document.addEventListener("keydown", (ev) => {
  if (ev.key !== "ArrowLeft" && ev.key !== "ArrowRight" && ev.key !== "Home" && ev.key !== "End") return;
  const list = ev.target && ev.target.closest && ev.target.closest('[role="tablist"]');
  if (!list) return;
  const tabs = Array.prototype.slice.call(list.querySelectorAll('[role="tab"]'));
  if (!tabs.length) return;
  const cur = tabs.indexOf(ev.target.closest('[role="tab"]'));
  if (cur < 0) return;
  ev.preventDefault();
  let ni;
  if (ev.key === "Home") ni = 0;
  else if (ev.key === "End") ni = tabs.length - 1;
  else ni = ev.key === "ArrowLeft" ? (cur - 1 + tabs.length) % tabs.length : (cur + 1) % tabs.length;
  const next = tabs[ni];
  if (next && next.focus) next.focus();
});
window.addEventListener("hashchange", render);

async function boot() {
  document.body.dataset.theme = State.theme;
  if (State.token) {
    // Harness/debug bearer path — no session, no CSRF; behaves exactly as before.
    try {
      const e = await GET("/mgmt/envs");
      State.envs = e.environments;
      if (!State.envs.find((x) => x.id === State.env)) State.env = State.envs[0]?.id || "prod";
    } catch (_) { State.envs = [{ id: State.env }]; }
  } else {
    // Cookie path: resolve the session first (plain GET, cookie-based). Valid → adopt the
    // identity + load envs; absent → straight to the sign-in card with zero further fetches.
    const s = await fetchSession();
    if (s) {
      applySession(s);
      try {
        const e = await GET("/mgmt/envs");
        State.envs = e.environments;
        if (!State.envs.find((x) => x.id === State.env)) State.env = State.envs[0]?.id || "prod";
      } catch (_) { State.envs = [{ id: State.env }]; }
    } else {
      State.envs = [{ id: State.env }];   // signed out — setToken re-fetches after sign-in
    }
  }
  render();
}
boot();
