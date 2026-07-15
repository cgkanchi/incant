/* screen: publish history + the publish/revert impact modal */
"use strict";

async function screenPointers() {
  const main = el("main");   // capture before any await (Issue B)
  const pid = State.route.pid;
  const dv = await GET(`/mgmt/prompts/${enc(pid)}/versions?environment=${enc(State.env)}`);
  const version = State.route.q.v ? parseInt(State.route.q.v)
    : (dv.versions.find((v) => v.tip_ahead > 0)?.version || dv.versions.find((v) => v.is_default)?.version || dv.versions[0]?.version);
  const vrow = dv.versions.find((v) => v.version === version) || {};
  // Publishing (and reverting) need releaser+. Publish history stays viewable for all.
  const canPublish = canRole("releaser");
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
      ${!m.current && canPublish ? `<button class="btn" data-act="revert" data-sha="${m.full_sha}" data-short="${esc(m.sha)}" data-v="${version}">Go back to this</button>` : ""}</div>`;
  }).join("") || '<div class="empty">No publish history yet.</div>';

  const shaChain = tl.moves.map((m) =>
    `${esc(m.sha)}${m.from_sha ? " ← from " + esc(m.from_sha) : " (first publish)"}${m.current ? " · live now" : ""}`).join("<br>");

  const locked = !!State.envs.find((e) => e.id === State.env)?.protected;
  const hasWaiting = vrow.tip_ahead > 0 && vrow.tip_full_sha;
  const advance = !canPublish
    ? (hasWaiting
        ? `<span style="font-size:12px;color:var(--mut)">${vrow.tip_ahead} unpublished ${plural(vrow.tip_ahead, "edit")} waiting — a releaser can publish them.</span>`
        : `<span style="font-size:12px;color:var(--live);font-weight:600">✓ The latest edits are already live.</span>`)
    : hasWaiting
      ? `<button class="tweak-btn" style="width:auto;display:inline-flex" data-act="makeLive" data-sha="${vrow.tip_full_sha}" data-short="${esc(vrow.tip_sha)}" data-v="${version}">✦ Publish latest edits (${vrow.tip_ahead} waiting)</button>
         <span class="faint" style="font-size:11px">preview the impact before it goes live${locked ? ` — ${esc(State.env)} is locked, type the prompt id to confirm` : ""}</span>`
      : `<span style="font-size:12px;color:var(--live);font-weight:600">✓ The latest edits are already live — nothing to publish.</span>`;

  main.innerHTML = `<div class="screen">
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
      fetchEnvRules(env, pid),   // retries scoped to the prompt's project on a 403
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
    // If we couldn't load the rule list (outage), tipRules is empty NOT because there are no
    // test rules but because we're blind to them — surface that in the modal, since publishing
    // without seeing which test rules become redundant is risky.
    rulesStatus: rd.status,
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
    <div style="font-size:12.5px;color:var(--mut)">Everyone currently on <b>Version ${p.v}</b> (the default).</div>
    ${rulesUnavailableNote(p.rulesStatus)}${ruleRows}`;
  const subjects = isRevert ? [] : (p.history || []).slice(0, p.tipAhead).map((h) => h.subject).filter(Boolean);
  const edits = subjects.length
    ? `<div class="groupname" style="margin-top:16px">The edits going live</div>
       <ul style="margin:0;padding-left:18px;font-size:12px;color:var(--mut);line-height:1.7">${subjects.map((s) => `<li>${esc(s)}</li>`).join("")}</ul>` : "";
  const confirmBlock = p.locked
    ? `<div style="margin:16px 0 2px;font-size:11px;color:var(--faint)"><b>${esc(p.env)}</b> is locked. Type <span class="mono" style="color:var(--mut)">${esc(p.pid)}</span> to confirm:</div>
       <input id="publishConfirm" data-act="confirmInput" data-token="${esc(p.pid)}" data-btn="publishBtn"
         spellcheck="false" autocomplete="off" placeholder="${esc(p.pid)}"
         style="width:100%;font-family:'IBM Plex Mono',monospace">` : "";
  return `<h3>${title}</h3>
    <p class="hint">${intro}</p>
    ${facts}${whatChanges}${affected}${edits}${confirmBlock}
    <div class="modal-actions">
      <button class="btn" data-act="closeModal">Cancel</button>
      <button id="publishBtn" class="btn primary"${p.locked ? " disabled" : ""} data-act="publishConfirm">${isRevert ? "Make this live again" : "Publish"}</button></div>`;
}
