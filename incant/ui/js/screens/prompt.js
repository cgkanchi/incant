/* screen: prompt overview + project settings modal */
"use strict";

async function screenOverview() {
  const pid = State.route.pid;
  const [d, rulesData] = await Promise.all([
    GET(`/mgmt/prompts/${enc(pid)}/versions?environment=${enc(State.env)}`),
    GET(`/mgmt/envs/${enc(State.env)}/rules`).catch(() => ({ rules: [] })),
  ]);
  const rules = rulesData.rules || [];
  const liveV = d.versions.find((v) => v.is_default) || d.versions.find((v) => v.live_sha) || null;
  const liveVersion = liveV ? liveV.version : null;
  const testing = testingFor(rules, pid, liveVersion);

  // ── status hero: only the rows that apply, in fixed order ──
  const heroRows = [];
  if (liveV && liveV.live_sha) {
    const sub = `Published ${ago(liveV.live_at)}${liveV.live_by ? " by " + esc(liveV.live_by) : ""}`;
    heroRows.push(`<div class="hero-row live">
      <span class="hdot live"></span>
      <div class="hero-body"><div class="t">Version ${liveV.version} is live for everyone</div>
        <div class="s">${sub}</div></div>
      <button class="btn olive sm" data-act="go" data-hash="#/p/${enc(pid)}/compare?b=${enc(liveV.version + "@live")}">View live text</button>
    </div>`);
  }
  for (const t of testing) {
    const title = t.tip ? "The latest unpublished draft is being tried by a group"
      : t.version != null ? `Version ${t.version} is being tested with a group`
      : t.label ? `Label ${esc(t.label)} is being tested with a group`
      : "A change is being tested with a group";
    const desc = t.rule.comment ? esc(t.rule.comment) : describeWhen(t.rule.when);
    heroRows.push(`<div class="hero-row">
      <span class="hdot testing"></span>
      <div class="hero-body"><div class="t">${title}</div>
        <div class="s">${desc} · not live for everyone</div></div>
      <a class="link mut" style="font-size:12px" href="#/p/${enc(pid)}/rules" data-act="go" data-hash="#/p/${enc(pid)}/rules">Manage →</a>
    </div>`);
  }
  if (liveV && liveV.tip_ahead > 0) {
    const subjects = (liveV.history || []).slice(0, liveV.tip_ahead).map((h) => h.subject).filter(Boolean);
    let sline = subjects.join(" + ");
    if (sline.length > 80) sline = sline.slice(0, 77) + "…";
    const meta = `last edit ${ago(liveV.tip_when)}${liveV.tip_author ? " by " + esc(liveV.tip_author) : ""} · not live yet`;
    heroRows.push(`<div class="hero-row draft">
      <span class="hdot draft"></span>
      <div class="hero-body"><div class="t">${liveV.tip_ahead} unpublished ${plural(liveV.tip_ahead, "edit")} on Version ${liveV.version}</div>
        <div class="s">${sline ? esc(sline) + " · " : ""}${meta}</div></div>
      <button class="btn primary sm" data-act="go" data-hash="#/p/${enc(pid)}/pointers?v=${liveV.version}">Review &amp; publish →</button>
    </div>`);
  }
  if (!heroRows.length) heroRows.push(`<div class="hero-row">
    <span class="hdot" style="background:var(--faint)"></span>
    <div class="hero-body"><div class="t">Not published yet</div>
      <div class="s">No version of this prompt is live in ${esc(State.env)}.</div></div></div>`);

  // ── technical details (SHAs + rules_version) ──
  const techLines = d.versions.map((v) =>
    `v${v.version} · live ${esc(v.live_sha || "—")} · tip ${esc(v.tip_sha || "—")}`).join("<br>") +
    `<br>rules_version ${esc(String(rulesData.rules_version ?? "—"))}`;

  // ── all versions ──
  const vrows = d.versions.map((v) => {
    const chip = v.label ? pill("acc", esc(v.label)) : "";
    // A non-live version being served to a group reads as "Testing"; the live version
    // itself always reads "Live for everyone" (its draft-testing shows in the hero).
    const vTesting = testing.some((t) => t.version === v.version && v.version !== liveVersion);
    let status;
    if (v.status === "archived")
      status = `<span class="faint" style="font-size:12px">Archived · still serving where pinned, no new changes</span>`;
    else if (v.version === liveVersion && v.live_sha) status = statusLine("live", "Live for everyone");
    else if (vTesting) status = statusLine("testing", "Testing with a group");
    else if (v.live_sha) status = statusLine("live", "Live for everyone");
    else status = `<span class="faint" style="font-size:12px">Not live</span>`;
    const edits = v.tip_ahead > 0 ? pill("acc", `${v.tip_ahead} ${plural(v.tip_ahead, "edit")} waiting`) : "";
    const meta = v.tip_author ? `Updated ${ago(v.tip_when)} · ${esc(v.tip_author)}` : "";
    return `<div class="prow">
      <span style="font-size:14px;font-weight:700;width:34px;flex:none">v${v.version}</span>
      ${chip}${status}${edits}
      <div class="grow"></div>
      <span class="prow-meta">${meta}</span>
      <a class="link sm" style="margin-left:10px;font-size:12px" href="#/p/${enc(pid)}/draft?v=${v.version}" data-act="go" data-hash="#/p/${enc(pid)}/draft?v=${v.version}">Open</a>
    </div>`;
  }).join("");

  // ── side cards (kept) ──
  const vars = d.variables.map((vr) => {
    const cls = vr.required ? "req" : "opt";
    const over = vr.overridden ? " over" : "";
    return `<div class="kv"><span class="varname">${esc(vr.name)}</span><span class="muted">${esc(vr.type)}</span>
      <button type="button" class="reqtag btn-bare ${cls}${over}" data-act="toggleReq" data-name="${esc(vr.name)}" data-v="${d.versions.find(x=>x.is_default)?.version||""}" data-req="${vr.required}" aria-label="${esc(vr.name)} is ${vr.required ? "required" : "optional"} — toggle">${vr.required ? "required" : "optional"}${vr.overridden ? " ·" : ""}</button></div>`;
  }).join("") || '<div class="faint">No variables.</div>';

  const includes = d.includes.length
    ? d.includes.map((i) => `<div style="display:flex;gap:8px;align-items:center"><span style="color:var(--acc-ink)">↳</span><span class="mono" style="font-size:11px">${esc(i)}</span></div>`).join("")
    : '<div class="faint">No includes.</div>';

  el("main").innerHTML = `<div class="screen">
    <div class="crumb"><a href="#/prompts" data-act="go" data-hash="#/prompts">Prompts</a> / ${esc(pid.split("/")[0])} /</div>
    <div class="h1row">
      <div><div class="page-h1 mono">${esc(pid)}</div>
        <div class="page-sub">${d.versions.length} ${plural(d.versions.length, "version")} · ${esc(State.env)}</div></div>
      <div class="grow"></div>
      <button type="button" class="link mut btn-bare" data-act="projectSettings" data-project="${esc(pid.split("/")[0])}" data-prompt="${esc(pid)}" style="font-size:12px">⚙ Project settings</button>
      ${canRole("editor") ? `<button class="btn primary" data-act="go" data-hash="#/p/${enc(pid)}/draft">Edit this prompt</button>` : ""}</div>
    <div class="hero">${heroRows.join("")}</div>
    ${techDetails(techLines, "commit SHAs, rules version")}
    <div style="display:flex;align-items:center;margin-top:22px">
      <div class="groupname" style="margin:0">ALL VERSIONS</div>
      <div class="grow"></div>
      ${canRole("editor") ? `<button type="button" class="link mut btn-bare" data-act="newVersionExplain" style="font-size:12px">＋ New version…</button>` : ""}</div>
    <div class="card">${vrows}</div>
    <div class="panelrow" style="margin-top:18px">
      <div class="card pad" style="flex:1 1 300px;min-width:0">
        <div class="groupname">Effective variables</div>
        <div class="kvs">${vars}</div>
        <div style="font-size:11.5px;color:var(--faint);margin-top:12px;border-top:1px solid var(--line2);padding-top:10px">Inferred from the template — click required/optional to override. Overrides carry forward across edits.</div>
      </div>
      <div class="card pad" style="flex:1 1 260px;min-width:0"><div class="groupname">Includes</div>${includes}</div>
    </div></div>`;
}

// ── project settings (governance) ────────────────────────────────────
// Self-review policy lives on the project, not the prompt. Opening this reads the
// current value with a no-op PATCH (returns the state; admins only). A non-admin gets
// 403 → we fall back to reading the value off an open draft and show it read-only.
async function openProjectSettings(project, prompt) {
  window._projSettings = { project, prompt, selfOk: null, canEdit: false, err: "", loading: true };
  openModal(`<div id="projSettingsBody">${projectSettingsBodyHtml(window._projSettings)}</div>`);
  const ps = window._projSettings;
  try {
    const r = await PATCH(`/mgmt/projects/${enc(project)}`, {});   // no-op read
    ps.selfOk = r.allow_self_review; ps.canEdit = true;
  } catch (e) {
    if (e && e.status === 403) {
      ps.canEdit = false;
      try {
        const dl = await GET(`/mgmt/prompts/${enc(prompt)}/drafts`);
        if (dl.drafts && dl.drafts.length) {
          const d = await GET(`/mgmt/drafts/${enc(dl.drafts[0].id)}`);
          ps.selfOk = d.allow_self_review;
        }
      } catch (_) { /* leave selfOk unknown */ }
    } else {
      ps.err = errText(e);
    }
  } finally {
    ps.loading = false;
    renderProjectSettings();
  }
}
function projectSettingsBodyHtml(ps) {
  const ON = "Anyone can approve their own edits";
  const OFF = "A different person must approve";
  let control;
  if (ps.loading) {
    control = `<div class="empty">Loading…</div>`;
  } else if (ps.selfOk == null && !ps.canEdit) {
    control = `<div class="faint" style="font-size:12px">The self-review policy is managed by admins — ask one to view or change it.</div>`;
  } else {
    const selfOk = !!ps.selfOk;
    const effect = selfOk
      ? "Authors can approve and commit their own drafts — no second reviewer needed."
      : "Every draft needs an approval from someone other than its author before it can be saved.";
    const stateLine = `<div style="display:flex;align-items:center;gap:10px;margin-bottom:8px">
        <span class="pill ${selfOk ? "live" : "warn"}">${selfOk ? "self-review on" : "four-eyes"}</span>
        <span style="font-size:13px;font-weight:600">${selfOk ? ON : OFF}</span></div>
      <div style="font-size:12px;color:var(--mut)">${effect}</div>`;
    control = ps.canEdit
      ? `${stateLine}
        <div style="display:flex;gap:8px;margin-top:14px;flex-wrap:wrap">
          <button class="btn ${selfOk ? "primary" : ""}" data-act="setSelfReview" data-to="true">${ON}</button>
          <button class="btn ${!selfOk ? "primary" : ""}" data-act="setSelfReview" data-to="false">${OFF}</button></div>`
      : `${stateLine}
        <div class="faint" style="font-size:11.5px;margin-top:12px;border-top:1px solid var(--line2);padding-top:10px">Read-only — admins can change this.</div>`;
  }
  return `<h3>Project settings</h3>
    <p class="hint">Settings for project <span class="mono">${esc(ps.project)}</span>.</p>
    ${ps.err ? `<div class="banner danger" style="margin-bottom:12px"><span style="font-size:12.5px;font-weight:600">${esc(ps.err)}</span></div>` : ""}
    <div class="groupname">Who can approve edits</div>
    ${control}
    <div class="modal-actions"><button class="btn" data-act="closeModal">Close</button></div>`;
}
function renderProjectSettings() {
  const b = el("projSettingsBody");
  if (b && window._projSettings) b.innerHTML = projectSettingsBodyHtml(window._projSettings);
}
