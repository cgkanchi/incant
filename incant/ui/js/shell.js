/* shell — sidebar, subnav, account chip, sign-in, and the "How to publish" tweak panel */
"use strict";

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
// The chip shows the highest held role. Ranking lives in util.js (ROLE_RANK/bestRole) so
// the chrome gating and the chip agree on the hierarchy.
function highestRole(roles) { return bestRole({ roles: roles || [] }); }
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
    let loaded = false;
    try { State.me = await GET("/mgmt/whoami"); loaded = true; }
    catch (e) { State._meFailed = true; State.me = null; }
    finally {
      State._mePromise = null; updateAcctChip();
      // Chrome is role-gated (nav + mutating actions). A freshly loaded identity can
      // reveal/hide it, so re-render once — the next ensureWhoami is a cache hit (no loop).
      if (loaded && typeof render === "function") render();
    }
  })();
  return State._mePromise;
}

// The centered sign-in card that replaces screen content on a 401.
function signInCard() {
  return `<div class="signin-wrap"><div class="signin-card">
    <div class="signin-mark">✦</div>
    <div class="signin-title serif">Sign in to Incant</div>
    <p class="signin-copy">Paste an API key — your admin created one for you, or use the admin key printed when this instance first started.</p>
    <input id="signinKey" type="password" class="signin-input" placeholder="incant_sk_…" spellcheck="false" autocomplete="off"
      data-enter="signinBtn">
    <label class="remember-row"><input type="checkbox" id="signinRemember"> Stay signed in for 30 days on this device</label>
    <button id="signinBtn" class="btn primary" data-act="setToken" style="width:100%;margin-top:10px">Sign in</button>
    <div class="err" id="signinErr" style="margin-top:8px"></div>
    <div class="signin-hint">Keys are managed in <b>Access</b> — an admin can issue you one.</div>
  </div></div>`;
}
// "Sign in with a different key…" — a password-input modal that reuses setToken semantics
// (posts the key to /auth/session for a fresh session cookie).
function openSwitchKeyModal() {
  openModal(`
    <h3>Sign in with a different key</h3>
    <p class="hint">Paste an API key to sign in as a different principal. Keys are managed in Access and never shown here.</p>
    <input id="switchKeyIn" type="password" placeholder="incant_sk_…" spellcheck="false" autocomplete="off"
      style="width:100%;font-family:'IBM Plex Mono',monospace"
      data-enter="switchKeyBtn">
    <label class="remember-row"><input type="checkbox" id="switchRemember"> Stay signed in for 30 days on this device</label>
    <div class="err" id="switchKeyErr"></div>
    <div class="modal-actions">
      <button class="btn" data-act="closeModal">Cancel</button>
      <button id="switchKeyBtn" class="btn primary" data-act="setToken">Sign in</button></div>`);
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
    ${canRole("viewer") ? `<a class="nav ${State.route.name === "audit" ? "active" : ""}" href="#/audit" data-act="go" data-hash="#/audit">
      <span class="gl">◷</span><span>Audit</span></a>` : ""}
    ${canRole("admin") ? `<a class="nav ${State.route.name === "access" ? "active" : ""}" href="#/access" data-act="go" data-hash="#/access">
      <span class="gl">⚿</span><span>Access</span></a>` : ""}
    ${canRole("admin") ? `<a class="nav ${State.route.name === "envs" ? "active" : ""}" href="#/envs" data-act="go" data-hash="#/envs">
      <span class="gl">❖</span><span>Environments</span></a>` : ""}
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
      fetchEnvRules(env, pid),   // retries scoped to the prompt's project on a 403
    ]);
    // fetchEnvRules resolves (never throws) even on an outage — treat an unavailable rule
    // list as the panel's existing error state, so we fall back to the static steps rather
    // than computing the "next step" from a rule set we couldn't actually read.
    if (rd.status === "unavailable") _tweak.data = { error: true };
    else _tweak.data = { versions: dv.versions || [], drafts: dl.drafts || [], rules: rd.rules || [] };
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
