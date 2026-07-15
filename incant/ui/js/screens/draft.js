/* screen: draft — write/diff/review tabs, autosave, rendered previews, commit + conflict */
"use strict";

// ── draft page: autosave ─────────────────────────────────────────────
// Autosave state lives at module scope so a pending save survives the re-render
// that a tab switch or navigation triggers — a keystroke is never dropped.
// baseSha chains autosaves (echoed as base_revision); conflict holds a 409 stale_write
// until the author picks "load newer" or "keep mine".
const Auto = { draftId: null, timer: null, seq: 0, applied: 0, inflight: null, baseSha: null, conflict: null };
let _draftNotice = null;   // one-shot notice shown atop the review tab (e.g. after a 412)

function scheduleAutosave() {
  if (Auto.conflict) return;   // paused until the conflict is resolved
  clearTimeout(Auto.timer);
  Auto.timer = setTimeout(fireAutosave, 800);   // ~800ms debounce after the last keystroke
}
function fireAutosave() {
  clearTimeout(Auto.timer); Auto.timer = null;
  const ta = el("draftTa");
  if (!ta || !Auto.draftId || Auto.conflict) return;
  const draftId = Auto.draftId, content = ta.value, seq = ++Auto.seq;
  setAutosaveChip("saving");
  Auto.inflight = (async () => {
    try {
      // Chain each PUT off the sha the editor state came from; re-chain from the response.
      const r = await PUT(`/mgmt/drafts/${draftId}/content`, { content, base_revision: Auto.baseSha });
      if (seq < Auto.applied) return;   // out-of-order guard: a newer save already landed
      Auto.applied = seq;
      if (r && r.draft_sha) Auto.baseSha = r.draft_sha;   // advance the chain
      if (window._dp && window._dp.draft && window._dp.draft.id === r.id) {
        applyDraftUpdate(r);
        setAutosaveChip("saved");
        doRenderDraft();                // refresh the test render off the saved content
      }
    } catch (e) {
      const detail = e && e.data && e.data.detail;
      if (e && e.status === 409 && detail && typeof detail === "object" && detail.error === "stale_write")
        enterAutosaveConflict(detail);
      else setAutosaveChip("failed");
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
  else if (state === "conflict") { c.textContent = "conflict"; c.className = "autochip err"; }
  else { c.textContent = "saved"; c.className = "autochip faint"; }
}

// ── autosave conflict (409 stale_write) ──────────────────────────────
// The draft changed elsewhere while the author typed. Stop autosaving, hold the server's
// current sha + content, and offer to load the newer version or overwrite with theirs.
function enterAutosaveConflict(detail) {
  Auto.conflict = { current_sha: detail.current_sha, current_content: detail.current_content };
  clearTimeout(Auto.timer); Auto.timer = null;
  setAutosaveChip("conflict");
  const box = el("autosaveConflict"); if (box) box.innerHTML = autosaveConflictHtml();
}
function autosaveConflictHtml() {
  if (!Auto.conflict) return "";
  return `<div class="banner warn autosave-conflict">
    <span style="font-size:12.5px;font-weight:600;flex:1;min-width:0">This draft changed somewhere else while you were typing.</span>
    <button class="btn sm" data-act="conflictLoadNewer">Load the newer version</button>
    <button class="btn sm primary" data-act="conflictKeepMine">Keep mine</button></div>`;
}
// choice: "load" adopts the server's content + re-chains; "keep" resends the author's
// text on top of the newer revision, overwriting it. Both resume autosaving.
function resolveAutosaveConflict(choice) {
  const c = Auto.conflict; if (!c) return;
  Auto.conflict = null;
  const box = el("autosaveConflict"); if (box) box.innerHTML = "";
  Auto.baseSha = c.current_sha;   // both paths chain onto the server's current revision
  if (choice === "load") {
    const ta = el("draftTa"); if (ta) ta.value = c.current_content || "";
    if (window._dp && window._dp.draft) window._dp.draft.content = c.current_content || "";
    setAutosaveChip("saved");
    doRenderDraft();
  } else {
    fireAutosave();   // resend the current textarea content chained onto current_sha
  }
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
  const main = el("main");   // capture before any await (Issue B); renderDraftStart writes through it too
  await flushAutosave();               // never lose a pending edit when re-entering
  const pid = State.route.pid, q = State.route.q;
  const vq = q.v ? parseInt(q.v) : null;
  const tab = q.tab || "write";
  main.innerHTML = `<div class="empty">Opening draft…</div>`;

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
    // No deep-link and no open draft of the viewer's on the target version → a read-only
    // "start" state, not a silent auto-create. "Start editing vN" creates the draft.
    const targetV = vq || dv.versions.find((x) => x.is_default)?.version || dv.versions[0]?.version || 1;
    renderDraftStart(main, pid, targetV, dv.versions);
    return;
  }

  const [draft, tcs, rulesRes, segsRes] = await Promise.all([
    GET(`/mgmt/drafts/${enc(draftId)}`),
    GET(`/mgmt/prompts/${enc(pid)}/test-contexts`),
    // Targeting data feeds the ad-hoc context form: the flags rules actually
    // check for this prompt (and its includes), with their candidate values. Retries
    // scoped to this prompt's project on a 403 so a project-scoped editor still gets them.
    // On a limited/unavailable result `rules` is [] and the form simply offers no flag
    // suggestions (the editor types them by hand) — a graceful degrade, not a misleading
    // "no rules" claim, so no warning strip is needed here.
    fetchEnvRules(State.env, pid),
    GET(`/mgmt/envs/${enc(State.env)}/segments`).catch(() => ({ segments: [] })),
  ]);

  // Page state survives in-tab updates (test contexts, diff controls); autosave is
  // tracked separately in Auto so it isn't lost across re-renders.
  window._dp = {
    draft, drafts: list.drafts, versions: dv.versions, tcs: tcs.test_contexts,
    // No saved contexts is not a dead end — fall back to the ad-hoc context form.
    tcActive: tcs.test_contexts[0]?.name || "__custom",
    customVars: null, customFlags: null,
    flagDefs: targetingFlags(rulesRes.rules, segsRes.segments, [pid, ...(dv.includes || [])]),
    varDefs: (dv.variables || []).slice(), ctx: null, ctxJson: false,
    diffAgainst: "base", diffMode: "source", diffTc: tcs.test_contexts[0]?.name || null,
    // Review tab: which context drives the rendered before/after, and whether the
    // secondary source-diff section is expanded.
    reviewTc: tcs.test_contexts[0]?.name || "__custom", reviewSrcOpen: false,
    pendingMsg: "",
  };
  window._draft = draft;   // codebase idiom — keep the alias current
  Auto.draftId = draft.id;
  Auto.baseSha = draft.draft_sha || null;   // start the autosave chain from the draft's tip
  Auto.conflict = null;

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
    `<button type="button" role="tab" id="draftTab-${id}" aria-selected="${tab === id}" aria-controls="draftTabBody" tabindex="${tab === id ? "0" : "-1"}" class="tab btn-bare ${tab === id ? "active" : ""}" data-act="draftTab" data-tab="${id}">${label}</button>`).join("");

  const body = tab === "diff" ? draftDiffTabShell(window._dp)
             : tab === "review" ? draftReviewTab(window._dp)
             : draftWriteTab(window._dp);

  main.innerHTML = `<div class="screen">
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
    <div id="draftTabBody" role="tabpanel" aria-labelledby="draftTab-${tab}">${body}</div></div>`;

  if (tab === "write" && window._dp.tcActive) doRenderDraft();
  if (tab === "diff") loadDraftDiff();
  if (tab === "review") { loadReviewRendered(); loadReviewComments(); }
}

// The read-only "start" state — shown when there's no draft to open (no ?draft, no open
// draft of the viewer's on the target version). It shows the version's current text with
// a primary "Start editing vN"; creating the draft is an explicit choice, not a side effect.
function renderDraftStart(main, pid, v, versions) {
  const vrow = (versions || []).find((x) => x.version === v) || {};
  Auto.draftId = null; Auto.baseSha = null; Auto.conflict = null;   // no live editor here
  // `main` is the node screenDraft captured at entry, so a superseded draft screen writes
  // its read-only start state into a detached node rather than over the current route.
  main.innerHTML = `<div class="screen">
    <div class="crumb"><a href="#/prompts" data-act="go" data-hash="#/prompts">Prompts</a> /
      <a href="#/p/${enc(pid)}/overview" data-act="go" data-hash="#/p/${enc(pid)}/overview">${esc(pid)}</a> /</div>
    <div class="h1row"><span class="h1 sm serif">Edit — <i>v${v}</i></span>
      <span class="sub">no open draft yet — this is the current text, read-only</span>
      <div class="grow"></div>
      <button class="btn primary" data-act="startDraft" data-v="${esc(String(v))}">Start editing v${v}</button></div>
    <div class="editor-wrap">
      <div class="card editor start-editor">
        <div class="ed-head"><span class="mono">v${v}.j2</span><span>·</span><span>current text · read-only</span></div>
        <textarea class="ta" id="draftStartTa" readonly spellcheck="false" placeholder="Loading current text…"></textarea>
        <div class="ed-foot"><span class="faint">Start editing to make changes — nothing goes live until you publish.</span></div>
        <div class="start-overlay"><button class="btn primary" data-act="startDraft" data-v="${esc(String(v))}">✎ Start editing v${v}</button></div>
      </div>
    </div></div>`;
  loadDraftStartContent(pid, v, vrow);
}
async function loadDraftStartContent(pid, v, vrow) {
  const ta = el("draftStartTa"); if (!ta) return;
  const sha = vrow.tip_full_sha || vrow.live_full_sha;
  if (!sha) { ta.value = ""; return; }
  try {
    // Read the version's current source via a self-compare — no draft required.
    const res = await GET(`/mgmt/prompts/${enc(pid)}/diff?a_version=${v}&a_sha=${enc(sha)}&b_version=${v}&b_sha=${enc(sha)}&mode=source&environment=${enc(State.env)}`);
    const t = el("draftStartTa"); if (t) t.value = res.right != null ? res.right : (res.left || "");
  } catch (_) { /* leave the placeholder */ }
}

function draftWriteTab(dp) {
  const draft = dp.draft;
  const chips = dp.tcs.map((t) =>
    `<button type="button" class="chip btn-bare ${t.name === dp.tcActive ? "active" : ""}" aria-pressed="${t.name === dp.tcActive}" data-act="tc" data-name="${esc(t.name)}">${esc(t.name)}</button>`).join("") +
    `<button type="button" class="chip btn-bare ${dp.tcActive === "__custom" ? "active" : ""}" aria-pressed="${dp.tcActive === "__custom"}" data-act="tc" data-name="__custom">＋ custom</button>`;
  const custom = dp.tcActive === "__custom" ? `
      <div id="ctxFormWrap" style="padding:8px 16px 4px;display:flex;flex-direction:column">${ctxFormHtml(dp)}</div>` : "";
  return `<div id="autosaveConflict">${autosaveConflictHtml()}</div>
    <div class="editor-wrap">
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
  // to the legacy reviewers[] (approval names only). A non-current approval (reviewed an
  // earlier revision) is greyed and clearly doesn't count — the policy line already
  // reflects the server's counting.
  const reviews = draft.reviews || [];
  let verdicts;
  if (reviews.length)
    verdicts = reviews.map((r) => {
      if (r.state !== "approved")
        return `<span class="pill warn">⨯ ${esc(r.reviewer)} requested changes</span>`;
      return r.current === false
        ? `<span class="pill neutral" title="approved an earlier revision — doesn't count toward the policy">✓ ${esc(r.reviewer)} · earlier revision</span>`
        : `<span class="pill live">✓ ${esc(r.reviewer)}</span>`;
    }).join(" ");
  else if ((draft.reviewers || []).length)
    verdicts = draft.reviewers.map((r) => `<span class="pill live">✓ ${esc(r)}</span>`).join(" ");
  else verdicts = '<span class="faint">No verdicts yet.</span>';

  // If the viewer's own approval went stale, relabel Approve → Re-approve.
  const myName = State.me && State.me.name;
  const myStale = reviews.some((r) => r.reviewer === myName && r.state === "approved" && r.current === false);
  const approveLabel = myStale ? "Re-approve" : "Approve ✓";

  const verdictBtns = blocked
    ? `<button class="btn" disabled>${approveLabel}</button>
       <button class="btn" disabled>Request changes</button>`
    : `<button class="btn olive" data-act="approve" data-draft="${esc(draft.id)}">${approveLabel}</button>
       <button class="btn danger" data-act="requestChanges" data-draft="${esc(draft.id)}">Request changes</button>`;

  const notice = _draftNotice
    ? `<div class="banner warn"><span style="font-size:12.5px;font-weight:600">${esc(_draftNotice)}</span></div>` : "";
  _draftNotice = null;   // one-shot

  const tcChips = dp.tcs.map((t) => `<button type="button" class="chip btn-bare ${t.name === dp.reviewTc ? "active" : ""}" aria-pressed="${t.name === dp.reviewTc}" data-act="reviewTc" data-name="${esc(t.name)}">${esc(t.name)}</button>`).join("") +
    `<button type="button" class="chip btn-bare ${dp.reviewTc === "__custom" ? "active" : ""}" aria-pressed="${dp.reviewTc === "__custom"}" data-act="reviewTc" data-name="__custom">＋ custom</button>`;
  const reviewCtx = dp.reviewTc === "__custom"
    ? `<div id="ctxFormWrap" style="padding:0 0 10px;display:flex;flex-direction:column">${ctxFormHtml(dp)}</div>` : "";

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
          <div id="reviewTcChips" style="display:flex;gap:6px;margin-bottom:10px;flex-wrap:wrap">${tcChips}</div>
          <div id="reviewCtxWrap">${reviewCtx}</div></div>
        <div id="reviewRendered" class="sxs-frame"><div class="empty">Loading rendered preview…</div></div>
        <div style="padding:0 18px 12px;font-size:11.5px;color:var(--mut)">${varsLine(draft) || "no declared variables"}</div>
        <div id="reviewSrcSection" style="padding:0 18px 16px">${reviewSrcSectionInner(dp)}</div>
        <div style="border-top:1px solid var(--line2);padding:14px 18px">
          <div class="groupname">Discussion</div>
          <div id="reviewCommentsThread"><div class="empty">Loading comments…</div></div>
          <div style="margin-top:10px">
            <textarea id="reviewCommentBody" class="cmt-input" spellcheck="false"
              data-act="commentInput" data-btn="reviewCommentBtn"
              placeholder="Leave a note for the author or other reviewers…"></textarea>
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

// The rendered before/after body for the review tab (draft diff, mode=rendered).
function reviewRenderedBody(res) {
  if (res.error) {
    const friendly = friendlyRenderError(res.error);
    const hint = res.error_kind === "serving" ? ""
      : `<div style="margin-top:8px;font-size:11.5px;color:var(--mut)">Adjust the values above, or pick another test context.</div>`;
    return `<div class="empty">${friendly || "⚠ " + esc(res.error)}${hint}</div>`;
  }
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
  if (tc === "__custom") {
    ensureCtx(dp);
    q += `&flags=${enc(dp.customFlags || "{}")}&variables=${enc(dp.customVars || "{}")}`;
  } else if (tc) {
    q += `&test_context=${enc(tc)}`;
  }
  try {
    const res = await GET(`/mgmt/drafts/${enc(dp.draft.id)}/diff?${q}`);
    if (el("reviewRendered")) el("reviewRendered").innerHTML = reviewRenderedBody(res);
    // Cheap failure probe: only the active context. If it errors, warn atop the tab.
    if (el("reviewBanner"))
      el("reviewBanner").innerHTML = res.error
        ? `<div class="banner warn" style="margin:12px 18px 0"><span style="font-size:12.5px;font-weight:600">${
            friendlyRenderError(res.error)
            || `This draft doesn't render for ${tc && tc !== "__custom" ? `context “${esc(tc)}”` : "these values"} — ${esc(res.error)}`}</span></div>`
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
    const friendly = friendlyRenderError(errText(e));
    if (el("renderOut")) {
      if (friendly) el("renderOut").innerHTML = friendly;
      else el("renderOut").textContent = "⚠ " + errText(e);
    }
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
      <button type="button" role="tab" aria-selected="${mode === "source"}" aria-controls="draftDiffBox" tabindex="${mode === "source" ? "0" : "-1"}" class="tab btn-bare ${mode === "source" ? "active" : ""}" data-act="diffMode" data-mode="source">Source</button>
      <button type="button" role="tab" aria-selected="${mode === "rendered"}" aria-controls="draftDiffBox" tabindex="${mode === "rendered" ? "0" : "-1"}" class="tab btn-bare ${mode === "rendered" ? "active" : ""}" data-act="diffMode" data-mode="rendered">Rendered</button></div>
    ${tcRow}
    <div class="card"><div id="draftDiffBox" role="tabpanel"><div class="empty">Loading diff…</div></div></div>`;
}
function draftDiffBody(res) {
  if (res.error) return `<div class="empty">${friendlyRenderError(res.error) || "⚠ " + esc(res.error)}</div>`;
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
