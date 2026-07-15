/* screen: library — the prompts list with filters + search */
"use strict";

// ── screens ──────────────────────────────────────────────────────────
// Library filters — single-select chips that combine with the search text. State is
// in-memory (persists across renders); the fetched data is cached so search/filter
// changes rebuild only the list, never re-fetch.
const PROMPT_FILTERS = [
  ["all", "All"], ["edits", "Unpublished edits"], ["testing", "Being tested"],
  // "notlive" matches when the NEWEST version isn't live (an older version may well be) —
  // the label says exactly that. "review" now counts drafts that truly need review.
  ["notlive", "Newest not live"], ["review", "Needs review"], ["recent", "Recently published"],
];
let _promptsFilter = { key: "all", q: "" };
let _promptsCache = null;   // { env, data, rules }

function within7Days(iso) {
  if (!iso) return false;
  const t = new Date(iso).getTime();
  if (!isFinite(t)) return false;
  const diff = Date.now() - t;
  return diff >= 0 && diff < 7 * 24 * 3600 * 1000;
}
// Pure predicates (testable): does a prompt row match a filter chip / the search text?
// `rules` is null when the caller lacks access to the env rule list — the testing predicate
// then reports no match (the chip is disabled separately, so this only guards stray calls).
function promptMatchesFilter(p, key, rules) {
  switch (key) {
    case "edits": return (p.tip_ahead > 0) || (p.open_drafts > 0);
    case "testing": return !!rules && testingFor(rules, p.prompt_id, p.live_version).length > 0;
    case "notlive": return p.newest_version != null && !p.newest_version_live;
    // Truthful "Needs review": open drafts under a review-policy project (server-computed),
    // not "any open-or-approved draft" regardless of policy.
    case "review": return p.drafts_needing_review > 0;
    case "recent": return within7Days(p.live_at);
    case "all": default: return true;
  }
}
function promptMatchesSearch(p, q) {
  if (!q) return true;
  const s = q.toLowerCase();
  return String(p.prompt_id || "").toLowerCase().includes(s) ||
         String(p.description || "").toLowerCase().includes(s);
}
function promptRowHtml(p, rules) {
  const bits = [];
  // green — live for everyone
  if (p.live && p.live_version != null) bits.push(statusLine("live", `Version ${p.live_version} live`));
  // amber — being tested with a group (dedupe by rendered label). Omitted entirely when
  // rules === null (no access to the env rule list) so we never imply "not being tested".
  const seen = new Set();
  if (rules) for (const t of testingFor(rules, p.prompt_id, p.live_version)) {
    const lbl = t.tip ? "draft testing" : (t.version != null ? `v${t.version} testing`
              : (t.label ? `${esc(t.label)} testing` : "testing"));
    if (seen.has(lbl)) continue; seen.add(lbl);
    bits.push(pill("warn", lbl));
  }
  // indigo — unpublished edits waiting
  if (p.tip_ahead > 0) bits.push(pill("acc", `${p.tip_ahead} ${plural(p.tip_ahead, "edit")} waiting`));
  // neutral — a newer version exists but was never published here
  if (p.newest_version != null && p.newest_version_live === false &&
      (!p.live || p.newest_version !== p.live_version))
    bits.push(pill("neutral", `v${p.newest_version} draft, not live`));

  const upd = p.updated ? `${ago(p.updated.when)} · ${esc(p.updated.who)}` : "";
  const desc = String(p.description || "").trim();
  const descLine = desc
    ? `<div class="prow-desc">${esc(desc.length > 110 ? desc.slice(0, 110) + "…" : desc)}</div>` : "";
  // The whole row is the link (keyboard + middle-click); the "Details →" affordance is
  // decorative content inside it (a real control nested in <a> would be invalid markup).
  return `<a class="prow click" href="#/p/${enc(p.prompt_id)}/overview" data-pid="${esc(p.prompt_id)}" data-act="go" data-hash="#/p/${enc(p.prompt_id)}/overview">
    <div class="prow-main">
      <div class="prow-id">${esc(p.prompt_id)}</div>
      ${descLine}
      <div class="prow-status">${bits.join("") || '<span class="faint" style="font-size:12px">Not live yet</span>'}</div>
    </div>
    <span class="prow-meta">${upd}</span>
    <div class="prow-actions">
      <span class="btn primary sm" aria-hidden="true">Details →</span>
    </div></a>`;
}
function promptListHtml() {
  if (!_promptsCache) return "";
  const { data, rules } = _promptsCache;
  const { key, q } = _promptsFilter;
  let html = "", total = 0;
  for (const proj of data.projects) {
    const matched = proj.prompts.filter((p) =>
      promptMatchesFilter(p, key, rules) && promptMatchesSearch(p, q));
    if (!matched.length) continue;
    total += matched.length;
    html += `<div class="groupname">${esc(proj.project.toUpperCase())}</div>
      <div class="card" style="margin-bottom:18px">${matched.map((p) => promptRowHtml(p, rules)).join("")}</div>`;
  }
  if (!total) {
    const lbl = (PROMPT_FILTERS.find(([k]) => k === key) || [, "All"])[1];
    const fNote = key !== "all" ? ` under <b>${esc(lbl)}</b>` : "";
    const qNote = q ? ` matching “${esc(q)}”` : "";
    return `<div class="empty">No prompts${fNote}${qNote}.
      <div style="margin-top:8px;font-size:11.5px">Try a different filter or clear the search.</div></div>`;
  }
  return html;
}
function updatePromptList() { const host = el("promptList"); if (host) host.innerHTML = promptListHtml(); }

async function screenPrompts() {
  const main = el("main");   // capture before any await (a superseded screen writes a detached node)
  // The env-wide rule list needs env-wide viewer. A project-scoped viewer 403s here — we
  // distinguish that (rules === null → "access limited") from a genuinely empty rule set,
  // so an empty list is never presented as "nothing is being tested". `.then(ok, err)`
  // keeps the two fetches concurrent while mapping a 403 to null.
  const [data, rules] = await Promise.all([
    GET(`/mgmt/overview?environment=${enc(State.env)}`),
    GET(`/mgmt/envs/${enc(State.env)}/rules`).then(
      (rd) => rd.rules || [],
      (e) => (e && e.status === 403 ? null : [])),
  ]);
  // Testing status is unknowable without the rule list — don't leave the user stuck on a
  // "Being tested" filter that can't be evaluated.
  if (rules === null && _promptsFilter.key === "testing") _promptsFilter.key = "all";
  _promptsCache = { env: State.env, data, rules };
  const nPrompts = data.projects.reduce((s, p) => s + p.prompts.length, 0);
  const allPrompts = data.projects.flatMap((p) => p.prompts);
  const counts = {};
  for (const [k] of PROMPT_FILTERS) counts[k] = allPrompts.filter((p) => promptMatchesFilter(p, k, rules)).length;
  const chips = PROMPT_FILTERS.map(([k, lbl]) => {
    // Without the rule list the "Being tested" count is unknowable — render that one chip
    // disabled + explained rather than showing a false "(0)".
    const hideTesting = k === "testing" && rules === null;
    const on = k === _promptsFilter.key && !hideTesting;
    const count = hideTesting ? "" : ` (${counts[k]})`;
    const extra = hideTesting
      ? ' disabled aria-disabled="true" title="Testing status is hidden — your access is limited to specific projects"' : "";
    return `<button type="button" class="chip btn-bare ${on ? "active" : ""}"${extra} aria-pressed="${on}" data-act="promptFilter" data-key="${k}">${esc(lbl)}${count}</button>`;
  }).join("");
  const accessNote = rules === null
    ? `<div style="font-size:11.5px;color:var(--faint);margin:-10px 0 16px">Testing status hidden — your access is limited to specific projects.</div>` : "";

  main.innerHTML = `<div class="screen">
    <div class="h1row">
      <div><div class="page-h1">Prompts</div>
        <div class="page-sub">${data.projects.length} ${plural(data.projects.length, "project")} · ${nPrompts} ${plural(nPrompts, "prompt")} · showing what's live in ${esc(State.env)}</div></div>
      <div class="grow"></div>
      <input class="search" id="promptSearch" placeholder="Search id or description…" data-act="search" spellcheck="false" value="${esc(_promptsFilter.q)}">
      ${canRole("editor") ? `<button class="btn primary" data-act="newPrompt">New prompt</button>` : ""}</div>
    <div id="promptFilters" style="display:flex;gap:8px;flex-wrap:wrap;margin-bottom:18px">${chips}</div>
    ${accessNote}
    <div id="promptList">${promptListHtml()}</div>
    <div style="font-size:11.5px;color:var(--faint);margin-top:4px">Any prompt can be included by any other — shared fragments are just prompts.</div></div>`;
}
