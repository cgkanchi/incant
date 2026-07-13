/* screen: compare — side-by-side diff of any two committed states */
"use strict";

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
    body = `<div class="empty">${friendlyRenderError(res.error) || "⚠ " + esc(res.error)}</div>`;
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
      <a role="tab" aria-selected="${mode === "source"}" aria-controls="compareBody" tabindex="${mode === "source" ? "0" : "-1"}" class="tab ${mode === "source" ? "active" : ""}" href="#/p/${enc(pid)}/compare?mode=source&${qs}" data-act="go" data-hash="#/p/${enc(pid)}/compare?mode=source&${qs}">Source</a>
      <a role="tab" aria-selected="${mode === "rendered"}" aria-controls="compareBody" tabindex="${mode === "rendered" ? "0" : "-1"}" class="tab ${mode === "rendered" ? "active" : ""}" href="#/p/${enc(pid)}/compare?mode=rendered&${qs}" data-act="go" data-hash="#/p/${enc(pid)}/compare?mode=rendered&${qs}">Rendered</a></div>
    <div class="card" id="compareBody" role="tabpanel"><div style="display:flex;gap:14px;padding:9px 18px;border-bottom:1px solid var(--line2);font-size:11px;color:var(--mut)">
      <span class="mono">${esc(pid)}</span><span style="margin-left:auto">${mode === "rendered" ? "rendered · fragments expanded" : "source, side by side"}</span></div>
      ${body}</div></div>`;
}
