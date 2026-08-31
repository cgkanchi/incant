/* suggest — typeahead for the condition builder: flag names known to the environment
   and values real traffic has sent (DESIGN.md §7 "Observed flags").

   Inputs opt in with data-sugg="flag" or data-sugg="value" (the latter naming its
   flag input via data-flag-input and its operator select via data-op-input). One
   popover at a time, positioned fixed under the focused input so it survives flex
   layouts and the modal's overflow; keyboard: up/down move, Enter picks, Esc closes
   (only the popover — the modal stays). Free text always works: this never blocks a
   value the user wants to type. */
"use strict";

const Suggest = (() => {
  const TTL_MS = 30000, LIMIT = 12, DEBOUNCE_MS = 140;
  const index = { env: null, at: 0, names: null, values: new Map() };
  let pop = null, anchor = null, items = [], active = -1, seq = 0, timer = null, blurTimer = null;

  // ── data (cached briefly; the composer is short-lived) ──────────────
  async function flagNames(env) {
    if (index.env === env && index.names && Date.now() - index.at < TTL_MS) return index.names;
    try {
      const d = await GET(`/mgmt/envs/${enc(env)}/flags`);
      index.env = env; index.names = d.flags || []; index.at = Date.now(); index.values.clear();
    } catch (_) { if (index.env !== env) { index.env = env; index.names = []; index.values.clear(); } }
    return index.names || [];
  }
  async function flagValues(env, flag, q) {
    const key = `${env} ${flag} ${q}`;
    const hit = index.values.get(key);
    if (hit && Date.now() - hit.at < TTL_MS) return hit.data;
    try {
      const data = await GET(`/mgmt/envs/${enc(env)}/flags/${enc(flag)}/values?q=${enc(q)}&limit=${LIMIT}`);
      index.values.set(key, { at: Date.now(), data });
      return data;
    } catch (_) { return null; }
  }
  const valueText = (v) => (typeof v === "boolean" ? (v ? "true" : "false") : String(v));

  // ── the comma-separated token model for `in` / `not in` ──────────────
  function tokenMode(input) {
    const op = input.dataset.opInput ? el(input.dataset.opInput) : null;
    return !!op && (op.value === "in" || op.value === "not_in");
  }
  function currentToken(input) {
    if (!tokenMode(input)) return { prefix: "", q: input.value.trim() };
    const parts = input.value.split(",");
    const last = parts.pop();
    return { prefix: parts.length ? parts.map((s) => s.trim()).filter(Boolean).join(", ") + ", " : "", q: last.trim() };
  }

  // ── popover ──────────────────────────────────────────────────────────
  function close() {
    if (pop) pop.remove();
    pop = null; items = []; active = -1;
    if (anchor) { anchor.removeAttribute("aria-expanded"); anchor.removeAttribute("aria-activedescendant"); }
    anchor = null;
  }
  function position() {
    if (!pop || !anchor) return;
    if (!document.body.contains(anchor)) { close(); return; }   // the builder re-rendered under us
    const r = anchor.getBoundingClientRect();
    pop.style.left = `${Math.max(8, Math.min(r.left, window.innerWidth - pop.offsetWidth - 8))}px`;
    const below = r.bottom + 4, room = window.innerHeight - below;
    if (room < 140 && r.top > 180) { pop.style.top = ""; pop.style.bottom = `${window.innerHeight - r.top + 4}px`; }
    else { pop.style.bottom = ""; pop.style.top = `${below}px`; }
    pop.style.minWidth = `${Math.max(220, r.width)}px`;
  }
  function render(input, list, hint) {
    if (!list.length && !hint) { close(); return; }
    if (!pop || anchor !== input) {
      close(); anchor = input;
      pop = document.createElement("div"); pop.className = "sugg"; pop.setAttribute("role", "listbox"); pop.id = "suggPop";
      document.body.appendChild(pop);
    }
    items = list; active = -1;
    pop.innerHTML = (hint ? `<div class="sugg-hint">${esc(hint)}</div>` : "") + list.map((it, i) =>
      `<div class="sugg-item" role="option" id="sugg-${i}" data-i="${i}" aria-selected="false"><span class="v">${esc(it.text)}</span>${
        it.tags && it.tags.length ? `<span class="t">${it.tags.map(esc).join(" · ")}</span>` : ""}</div>`).join("");
    input.setAttribute("aria-expanded", "true");
    position();
  }
  function setActive(i) {
    if (!pop) return;
    active = i;
    pop.querySelectorAll(".sugg-item").forEach((e, j) => { e.classList.toggle("active", j === i); e.setAttribute("aria-selected", j === i ? "true" : "false"); });
    if (anchor) { if (i >= 0) anchor.setAttribute("aria-activedescendant", `sugg-${i}`); else anchor.removeAttribute("aria-activedescendant"); }
    const e = i >= 0 ? pop.querySelector(`#sugg-${i}`) : null; if (e && e.scrollIntoView) e.scrollIntoView({ block: "nearest" });
  }
  function pick(i) {
    const it = items[i]; if (!it || !anchor) return;
    const input = anchor;
    const { prefix } = currentToken(input);
    input.value = tokenMode(input) ? `${prefix}${it.text}` : it.text;
    // The builder reads inputs back at save time; still notify listeners like a real edit.
    input.dispatchEvent(new Event("input", { bubbles: true }));
    input.dispatchEvent(new Event("change", { bubbles: true }));
    close();
    input.focus();
  }

  // ── building the lists ───────────────────────────────────────────────
  async function open(input) {
    const kind = input.dataset.sugg; if (!kind) return;
    const env = State.env; if (!env) return;
    const mySeq = ++seq;
    const { q } = currentToken(input);
    const ql = q.toLowerCase();
    if (kind === "flag") {
      const names = await flagNames(env);
      if (mySeq !== seq || document.activeElement !== input) return;
      const list = names
        .filter((f) => !ql || f.name.toLowerCase().includes(ql))
        .filter((f) => f.name !== q)
        .sort((a, b) => (b.name.toLowerCase().startsWith(ql) - a.name.toLowerCase().startsWith(ql)) || a.name.localeCompare(b.name))
        .slice(0, LIMIT)
        .map((f) => {
          const tags = [];
          if (f.in_rules) tags.push("in rules");
          if (f.suppressed) tags.push("not suggested");
          else if (f.values_seen) tags.push(`${f.values_seen} value${f.values_seen === 1 ? "" : "s"} seen`);
          return { text: f.name, tags };
        });
      render(input, list, list.length || names.length ? "" : "No flags seen in this environment yet — type a name.");
      return;
    }
    const flagInput = input.dataset.flagInput ? el(input.dataset.flagInput) : null;
    const flag = flagInput ? flagInput.value.trim() : "";
    if (!flag) { close(); return; }
    const d = await flagValues(env, flag, q);
    if (mySeq !== seq || document.activeElement !== input) return;
    if (!d) { close(); return; }
    if (d.suppressed) {
      render(input, [], `${flag} isn't suggested — ${Number(d.values_seen).toLocaleString()}+ distinct values seen (high-cardinality). Type a value.`);
      return;
    }
    const taken = tokenMode(input) ? new Set(input.value.split(",").map((s) => s.trim()).filter(Boolean)) : new Set();
    const list = (d.values || [])
      .map((v) => ({ text: valueText(v.value), tags: (v.sources || []).map((s) => (s === "traffic" ? (v.last_seen ? `seen ${ago(v.last_seen)}` : "seen") : "in a rule")) }))
      .filter((it) => it.text !== q && !taken.has(it.text))
      .slice(0, LIMIT);
    render(input, list, "");
  }
  function schedule(input) { clearTimeout(timer); timer = setTimeout(() => open(input), DEBOUNCE_MS); }

  // ── wiring (delegated; the builder re-renders its inputs freely) ─────
  document.addEventListener("focusin", (ev) => {
    const t = ev.target;
    clearTimeout(blurTimer);
    if (t && t.matches && t.matches("input[data-sugg]")) { open(t); return; }
    if (pop && !(pop.contains(t))) close();
  });
  document.addEventListener("focusout", (ev) => {
    if (ev.target === anchor) blurTimer = setTimeout(() => { if (document.activeElement !== anchor) close(); }, 150);
  });
  document.addEventListener("input", (ev) => {
    const t = ev.target;
    if (t && t.matches && t.matches("input[data-sugg]") && ev.isTrusted) schedule(t);
  }, true);
  // Capture phase: runs before main.js's Escape (which closes the modal) and Enter handlers.
  document.addEventListener("keydown", (ev) => {
    if (!pop || !anchor || ev.target !== anchor) return;
    if (ev.key === "ArrowDown") { ev.preventDefault(); setActive(items.length ? (active + 1) % items.length : -1); }
    else if (ev.key === "ArrowUp") { ev.preventDefault(); setActive(items.length ? (active - 1 + items.length) % items.length : -1); }
    else if (ev.key === "Enter") { if (active >= 0) { ev.preventDefault(); ev.stopPropagation(); pick(active); } }
    else if (ev.key === "Escape") { ev.preventDefault(); ev.stopPropagation(); close(); }
    else if (ev.key === "Tab") { close(); }
  }, true);
  // mousedown (not click): picking must beat the input's blur.
  document.addEventListener("mousedown", (ev) => {
    if (!pop) return;
    if (pop.contains(ev.target)) {
      ev.preventDefault();
      const it = ev.target.closest(".sugg-item"); if (it) pick(+it.dataset.i);
    } else if (ev.target !== anchor) close();
  }, true);
  window.addEventListener("scroll", () => position(), true);
  window.addEventListener("resize", () => position());

  return { open, close, invalidate() { index.names = null; index.values.clear(); } };
})();
