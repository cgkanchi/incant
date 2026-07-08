# Incant — Design

A prompt management platform for **non-devs to author, target, test, and develop
prompts** and for **devs and agents to consume them** — the LaunchDarkly parallel: PMs
define and ramp, services render. **Git** for content (with an opinionated structure on
top), **Jinja2** for rendering, **flag-based targeting** for who sees what.

Three planes:

- **Git is the content store.** One canonical repository, owned by Incant, with an
  opinionated layout: one file per prompt version. Git does what it is made for —
  per-file history, immutable SHAs, diffs, durability, portability. External providers
  (GitHub, Bitbucket, GitLab, a bare server) receive backup pushes and are otherwise
  ignored: no webhooks, no PR review, no provider APIs.
- **Postgres is the control plane.** Targeting rules, segments, environments, **live
  pointers**, review state, variable metadata, RBAC, audit. Operational state at ops
  tempo — and no content, ever: the DB stores SHAs, never template text.
- **Memory is the serving plane.** Compiled templates and rule snapshots; the render
  path touches no git, no disk, no DB.

The core loop: authors commit changes to version files (cheap, gated by review); rules
and live pointers decide which version — at which exact commit — serves to whom (gated,
audited); Jinja renders with request variables; the response reports version + SHA, so
any render is reproducible.

---

## 1. What this design must solve

Stated as the four driving requirements:

1. **New versions**: create a new version of a prompt, review it, make it live.
2. **Rollbacks**: instant, to any previous state.
3. **Tweaks without version explosion**: iterate on a *live* version — and backport
   fixes to *old* versions — without minting a new version each time. Prompts need far
   more iteration than code releases; semver-style ceremony produces a mess. Collapsing
   tweaks into "just make a new version" fails differently: a patch to v12 published
   after v13 becomes v14, and now semantic order and edit order disagree.
4. **Gating via targeting**: neither a new version nor a tweak goes live by publishing
   it. The typical flow: make a tweak → target the tweaked content to a user/team →
   test → expand → make "live".

The resolution of 3 is structural: **a version is a file** (`v2.j2`), so semantic
identity lives in the filename while chronology lives in the commit log — a late
backport to v12 is just a newer commit touching `v12.j2`, and neither ordering lies.
The resolution of 4 is one concept: **live pointers** (§5) — commits never change
serving; pointer moves do.

### Supporting goals

- **Human-readable identity.** Rules target `support/system@v2`, humans discuss "v2".
  The commit SHA appears only as the machine-level pin in responses and audit — nobody
  types one.
- **Never serve the wrong content.** Serving resolves exclusively through explicit SHAs
  (live pointers, or a rule's deliberate tip/SHA target). Tips never serve implicitly;
  pointer moves are the only way served content changes, and they are audited and
  optionally approval-gated. One bounded exception, loudly flagged: if a version's
  current live SHA is unservable, its *previous live* SHA may serve (§10) — same
  version, same intent; never a different version, never silent.
- **In-product review.** PR review is the wrong tool: it reviews a text diff of a
  branch, prompt review must judge *what will be served* — both sides rendered with
  real flags and variables, fragments expanded, test contexts executed, by reviewers
  who never touch git. Incant owns review.
- **Minimal metadata.** The repo contains `.j2` files, nothing else. Variables are
  extracted from the Jinja AST; refinements live in the DB.
- **Fast.** p50 ~1 ms, **p99 single-digit ms**; memory-only hot path.
- **Production-grade RBAC** over rendering, authoring, targeting, pointer moves, admin.

### Non-goals (v1)

- Prompt *execution* (calling LLMs), evals/scoring, cost tracking.
- Provider integration: no webhooks, no PR/MR APIs, no ingestion. Remotes are passive
  backup targets.
- Serving the git protocol to users (clone the backup remote if you want a repo).
- Response caching (templates and rules are cached aggressively instead, §8).

---

## 2. Concepts

| Term | Meaning |
|---|---|
| **Project** | Top-level namespace and permission scope (`support`, `growth`, `shared`). |
| **Prompt** | A folder of version files. Its id is a path: `support/system`. Fragments are not a separate type — any prompt can include any other (§4). |
| **Version** | A file: `support/system/v2.j2`. A stable, targetable identity whose content iterates via commits — not a frozen point. Optional label, notes, status in the DB. |
| **Tip** | The newest validated commit touching a version file. Content that *exists* but serves only where explicitly targeted. |
| **Live pointer** | Per `(environment, prompt, version)`: the commit SHA that version serves in that environment. "Make live" = advance the pointer. Append-only history retained (rollback, audit, blame). |
| **Label** | A name attached to versions across prompts (`voice-v2`) — the handle for multi-prompt experiments. |
| **Draft** | Work-in-progress edit of a version file (or a proposed new file), held on an Incant-managed ref; becomes a commit through review. |
| **Flags** | Evaluation context sent with a render request (`{"tier": "pro", "user_id": "u_42"}`). Rules match these. |
| **Variables** | Values interpolated into the chosen template by Jinja. |
| **Environment** | A named targeting scope (`prod`, `staging`): rules, segments, kill switches, defaults, live pointers. Pure DB state. |
| **Rule / segment** | Per-environment. Rules map flag conditions to versions (at live, tip, or a pinned SHA) or labels; segments are named, reusable conditions. |
| **Remote** | A git URL receiving backup pushes. Plural, optional, never read from in normal operation. |

Flags choose *which version at which commit*; variables fill in *the blanks within it*.
Same request, never mixed: flags are invisible to templates unless explicitly passed as
variables too.

---

## 3. Storage model — git owns content, the DB owns state

An earlier iteration of this design moved content into Postgres, reasoning that if the
provider is out of the loop, git is just a backup format. That overshot: provider
independence requires ignoring the *provider* — not abandoning *git*. Rebuilding
per-version history in the DB meant synthetic revision numbers, a revisions table, and
a "generated" repo with regeneration machinery — a reimplementation of things git does
natively and better:

| Requirement | Git primitive |
|---|---|
| Within-version tweak history | `git log -- support/system/v2.j2` |
| Immutable, pinnable content states | commit SHAs |
| Diffs (source-level) | `git diff` |
| Backports to old versions | a commit touching an old file |
| Durable, portable, standard storage | the repository itself |
| Every version's current text, side by side | the working tree at HEAD |

The opinionated structure on top is deliberately thin: **one branch, one file per
version, all writes through Incant** (commits authored as the acting user), and the
serving-state question — which commit of which version serves where — pushed entirely
into the DB, where operational state belongs.

| Concern | Home |
|---|---|
| Template content + all content history | **Git** (canonical repo, Incant-owned) |
| Version registry: status, labels, notes | **DB** (references files; no content) |
| Live pointers, defaults, rules, segments, kill switches | **DB** |
| Validation results per commit, extracted variables, refinements | **DB** (keyed by SHA/blob — derived, rebuildable) |
| Drafts/review state, comments, test contexts | **DB** (draft content on git refs) |
| Principals, roles, keys, approvals, audit | **DB** |
| Compiled templates, rule snapshots | **Memory** (+ disk spill, rebuildable) |
| Off-site copy of content | **Remotes** (async backup pushes) |

**Durability, stated plainly:** the canonical repo volume is now durable state — the
content system of record. Protection = continuous backup pushes to remotes (content)
plus normal Postgres backups (control plane). Losing the volume *and* every remote
loses content; losing the DB loses targeting/RBAC/audit but never content.

---

## 4. Content model

### The content tree

```
support/                        # project
├── system/                     # prompt: support/system
│   ├── v1.j2                   # a version: one file, iterated by commits
│   ├── v2.j2
│   └── v3.j2
├── greeting/
│   └── v1.j2
└── escalation/
    └── triage/                 # prompt: support/escalation/triage
        └── v1.j2
shared/                         # a project of shared fragments — just prompts
└── style/
    └── language-rules/         # prompt: shared/style/language-rules
        ├── v1.j2
        └── v2.j2
```

- **Projects** are top-level directories, registered in the DB; they scope permissions
  (§11), group the UI, and namespace prompt ids.
- **A prompt is a folder of versions; a version is one file; there is no manifest.**
  New version = new file (seeded from any existing version). Tweak or backport = a
  commit to an existing file. HEAD holds every version's current text side by side —
  greppable, cross-version diffable (`diff v2.j2 v3.j2`), no history archaeology. (A
  version serializes as a file rather than a folder because it is exactly one template;
  trivially revisited if versions ever carry more artifacts.)
- Authors never see this layout — the UI/API is the surface, git is the store.

### Fragments are prompts

Any prompt includes any other by id:

```jinja
{% include "shared/style/language-rules" %}
```

The include resolves **through the targeting engine, with the same flag context, in the
same environment**: rules targeting the fragment pick its version, the live pointer
picks the commit; no rule → the environment's default. "Roll out the new style rules to
10% of enterprise" is targeting on the fragment prompt — every consumer follows,
coherently, with no consumer edited.

- Includes may cross projects (RBAC governs who can *edit*, not who can include).
- **Cycles:** validation-time static check over the include graph at current defaults,
  plus a render-time depth limit (32) as backstop — resolution is flag-dependent.
- Every response reports the resolved version *and SHA* of the prompt and every
  included prompt (§9) — fragment indirection never costs reproducibility.

### Variables — extracted, not declared

On every draft save and commit, Incant parses the template (`jinja2.meta` over the AST):

- the **variable set** — every undeclared name referenced;
- **optionality inference** — used only inside guards (`{% if var %}`,
  `{{ var | default(...) }}`) → optional; otherwise required;
- the **effective schema** — the union over the include closure (against defaults for
  display; against actually-resolved content at render time for validation).

Extraction is cached per blob; human refinements (types, descriptions, defaults) attach
per `(prompt, version)` in the DB and carry forward across tweaks — with a lint warning
when a commit changes the variable set out from under them. The template stays the
single source of *which* variables exist; the DB holds what they *mean*. Render-time
validation: missing required variable → `422` naming it; defaults applied pre-render so
`StrictUndefined` stays on.

---

## 5. The lifecycle: commits are cheap, pointers are governed

One principle covers all four driving requirements: **content changes land as commits
and change nothing; serving changes are pointer moves and are governed.**

### Validation first

Every commit is validated on landing (Jinja compiles, includes resolve, cycle check,
strict render against the prompt's test contexts); results are recorded per SHA. Only
validated SHAs can ever be referenced by a pointer or rule — a broken template can
exist in history but can never serve.

### The flows

**New version** (req 1): draft `v3.j2` seeded from v2 → in-product review → commit.
v3 now *exists* — and serves nowhere. Target it (a rule for the beta segment, a
rollout), or promote it to an environment's default. Making it prod's default is a
pointer-class change: audited, optionally approval-gated (§7).

**Tweak a live version** (req 3+4, the canonical scenario): edit `v2.j2` → review →
commit. Prod's live pointer for v2 still pins the old SHA — nothing changed in serving.
Now: rule "team X → `v2@tip`" → test → widen (`v2@tip` at 10%, 50%) → **make live**
(advance prod's v2 pointer to the new SHA) → delete the rule. The tip/live gap *is* the
testing window.

**Backport** (req 3): identical flow against `v1.j2`. Semantic identity is the
filename; the commit log records that the fix landed last Tuesday. No v14-that-is-
really-v12 confusion.

**Rollback** (req 2): three levers, all instant pointer moves — move a version's live
pointer back to any entry in its **pointer history** (§7); retarget default/rules at an
older version; or the kill switch (§7). Nothing is rebuilt, nothing re-reviewed; every
state ever served is a validated SHA away, and the history records who served what,
when.

### Review

Review gates what enters the repo — targeting gates who sees it. Per-project policy: N
approvals to commit (0 for scratch projects). The draft view renders saved **test
contexts** (named `{flags, variables}` sets) live — fragments expanded — and diffs
*rendered output* side-by-side against any version at any SHA (source diff one tab
over); comments anchor to source or rendered output. Reviewers judge what will be
served. The unit of review is a change to a version file — v12 takes a patch under
review while v13 experiments, in parallel; a shape PRs against a lineage tip can't
express.

Optimistic concurrency: if the file moved since the draft's base commit, the publisher
sees the intervening diff and confirms — git-level merge only when edits don't overlap,
never a silent merge of prompt text.

Devs and agents get the same flow via the mgmt API — create draft, put content, commit —
same validation, review policy, audit. No side door.

### Version registry

Versions carry DB metadata: notes, optional **label**, status `active → archived`
(archived: no new commits, no new rules; existing pointers keep serving forever).
`incant_versions` mirrors the tree; the tree is authoritative for existence, the DB for
status.

---

## 6. The canonical repo and its backups

- Incant owns one canonical repository (per instance, monorepo of projects) on its
  volume. Single `main` branch; drafts live at `refs/incant/drafts/<id>`; commits are
  authored as the acting user, committed by Incant, with structured trailers
  (`Incant-Prompt`, `Incant-Version`, `Incant-Draft`) for machine-readable history.
- **Backup pushes are asynchronous and queued** (queue state in the DB): every commit
  propagates to all configured remotes; remote down → queue grows, alerts fire
  (`incant_backup_lag_seconds`), nothing else happens.
- Remotes are write-only from Incant's perspective. Pushes to them by anything else are
  ignored — Incant force-pushes its own lineage on conflict.
- **Restore**: content = clone from any remote (it's the real repo — full history, no
  reconstruction step); control plane = Postgres backup. Losing only the DB never loses
  content; the version registry rebuilds from the tree + trailers, targeting/RBAC/audit
  need the DB backup.
- **Escape hatch**: any clone is the complete content history in standard git — grep
  HEAD across all versions, `git log` a version file, or leave the platform with it.

---

## 7. Targeting

All targeting is per-environment DB state, edited in the UI/API, propagated to serving
nodes in seconds.

### Rules serve versions

```jsonc
{
  "id": "voice-v2-beta",
  "environment": "prod",
  "scope": "global",                 // or {"scope": "prompt", "prompt_id": "support/system"}
  "priority": 10,
  "when": {"all": [
    {"segment": "beta-us"},
    {"flag": "tier", "op": "in", "values": ["enterprise", "pro"]}
  ]},
  "serve": {"rollout": {"bucket_by": "user_id",
                        "weights": [{"label": "voice-v2", "weight": 20},
                                    {"default": true,     "weight": 80}]}},
  "status": "active",                // active | paused | archived
  "comment": "Voice v2 ramp — EXP-142",
  "version": 7
}
```

- **Prompt-scoped rules** serve a version at its live pointer (`{"version": 2}`), at
  its tip (`{"version": 2, "at": "tip"}` — the testing flow), or at an explicit SHA.
- **Global rules** serve a **label**: prompts with a labeled version participate at
  that version's live pointer; others skip the rule and continue. One experiment = one
  rule; killing it = archiving one rule.
- Evaluation order per prompt: global rules → prompt rules → **environment default**
  (a version, at its live pointer). First match wins.
- Environments have `track_tip: true|false` — dev/staging convenience where live
  pointers auto-follow validated tips; off for prod, where making content live is
  always explicit.

### Clause & rollout semantics

- Operators: `eq`, `neq`, `in`, `not_in`, `contains`, `starts_with`, `ends_with`,
  `matches`, `gt/gte/lt/lte`, `semver_gt/semver_lt`, `exists`; `all`/`any` composition.
  A clause referencing an absent flag does not match (no error).
- **Segments**: named clause groups per environment, referencable from any rule.
- **Rollout bucketing is coherent across prompts for global rules**:
  `sha256(f"{rule.id}:{bucket_value}")` — no prompt id — so an experiment user sees it
  in every participating prompt. Prompt-scoped rollouts hash
  `sha256(f"{prompt_id}:{rule.id}:{bucket_value}")`. Rule ids are immutable → ramps are
  monotonic and reordering never reshuffles cohorts. Missing `bucket_by` flag → rule
  falls through.

### Governance

- **Pointer-class changes** — advancing/reverting a live pointer, changing an
  environment default — are the governed acts: `operator` role, audited, and in
  `protected` environments optionally behind propose → approve (approver ≠ proposer).
- **Rule edits** (create, ramp, archive) need `operator` on the environment but no
  approval ceremony — requirement 4's test-target-expand loop must be low-friction, and
  a rule pointing at a validated SHA can only expose reviewed content.
- **Kill switches**: per environment, per prompt — one click forces the environment
  default, bypassing all rules; never waits for approval. Loud in UI and audit.

### Integrity

- Rules and pointers may only reference validated SHAs and existing versions/labels;
  violations are rejected at write time (global rules: warned with participant list).
- Archiving a version still referenced by active rules requires acknowledging them.
- Eval-time backstop: a rule resolving to something unservable is skipped, counted
  (`incant_rule_skips_total`), surfaced in the UI.

### Pointer history

Live pointers are **append-only**: every move writes
`(from_sha, to_sha, actor, at, comment)`, and the current pointer is simply the newest
entry. That one decision buys three things — **rollback** (make any prior entry live
again, one click), **audit** (who made what live, when, why), and **blame** (join the
history against render logs to answer "which make-live changed behavior at 3 pm?" —
the UI renders this as a per-version timeline per environment). Recent history also
rides along in the serving snapshot: it is what makes the §10 within-version fallback
work even during a DB outage.

### Lifecycle, audit, propagation

Every targeting mutation (rules, segments, pointers, defaults) snapshots to
`rule_revisions` (actor, at, comment) and bumps the environment's monotonic
**`rules_version`** — full history, one-click rollback of a rule or the whole
environment's targeting state. Nodes hold rule snapshots in memory; Postgres
`LISTEN/NOTIFY` (2s poll fallback) propagates bumps. Target: any targeting change —
including "make live" — serves everywhere in **< 2 s**.

---

## 8. Rendering & performance

- **Jinja2 `SandboxedEnvironment`**, **`StrictUndefined`** (missing variable → `422`
  naming it; DB-held defaults applied pre-render), autoescape off (plain text for
  LLMs). The loader serves only registered content at pinned blobs — never a
  filesystem.
- **Memory-only hot path**: key check (in-memory) → rule eval (pure function over the
  snapshot, µs) → compiled-template render (sub-ms typical). **p50 ~1 ms, p99
  single-digit ms.** No git, disk, or DB per request.
- **Content cache**: blobs are extracted from git into a content-addressed cache when a
  SHA becomes referenceable (validation time), compiled templates cached by blob hash —
  immutable, LRU-evicted, never invalidated.
- **Eager warm**: everything reachable from any environment's targeting (live pointers
  *and each version's previous live* — the §10 fallback must be warm to be useful —
  defaults, rule targets incl. tips, their include closures) precompiles at boot and on
  commit/targeting change. `incant_template_cache_misses_total` should sit at zero.

---

## 9. Serving API

### Render

```
POST /prompt/{prompt_id}
```

```jsonc
{
  "flags":       {"tier": "enterprise", "region": "us", "user_id": "u_42"},
  "variables":   {"customer_name": "Acme", "history": []},
  "environment": "prod",             // optional; defaults to the instance default env
  "pin": {                           // optional; exact replay of a historical render
    "versions": {"support/system": "v2@8c1f2ab", "shared/style/language-rules": "v1@2fe9c1a"},
    "rules_version": 4172            // alternative: pin targeting state instead
  }
}
```

```jsonc
// 200 OK
{
  "prompt": "You are a friendly support agent for Acme...",
  "prompt_id": "support/system",
  "matched_rule": {"scope": "global", "id": "voice-v2-beta"},   // or {"scope":"prompt",...} or "default"
  "versions": {                      // every prompt that contributed content, resolved
    "support/system": {"version": 2, "commit": "8c1f2ab", "label": "voice-v2"},
    "shared/style/language-rules": {"version": 1, "commit": "2fe9c1a"}
  },
  "environment": "prod",
  "rules_version": 4172,
  "stale_rules": false,              // true iff targeting is frozen at last-known-good (§10)
  "content_fallback": false          // true iff any entry served a previous live SHA (§10)
}
```

The `versions` map + `rules_version` is the reproducibility tuple — log it beside LLM
calls; feed it back as `pin` to replay exactly. It is SHA-exact, so later tweaks to v2
never blur an old trace. Humans read `v2`; machines pin `8c1f2ab`. On the rare §10
fallback, the map reports the SHA *actually served* with `"fallback": true` on that
entry (plus the top-level flag and an `X-Incant-Content-Fallback` header) — the
reproducibility contract holds even in degraded states.

Errors: `401/403` bad or under-scoped credential · `404` unknown prompt/environment ·
`409` resolved content unservable (compound cache+DB failure — §10) · `422`
variable/render failure (naming the variable or line) · `503` node not ready.

### Supporting endpoints

```
POST /prompt/{prompt_id}/evaluate   # flags only → resolved version+SHA (no render)
POST /evaluate                      # flags → resolution for EVERY prompt in the env;
                                    #   "what does this experiment change?" in one call
GET  /prompts?environment=…         # prompt list: descriptions, effective variable schemas,
                                    #   defaults, labels, tip-vs-live status
GET  /prompt/{prompt_id}/versions   # versions; per version: label, notes, status,
                                    #   commit history, live pointers per environment
GET  /healthz  /readyz  /metrics
```

---

## 10. Availability & failure modes

Node state: the canonical repo (durable — content system of record), content + compiled
caches (rebuildable), rule snapshots (memory + disk spill). Postgres is authoritative
for the control plane and sits on the refresh/write paths only, never per-request.

| Failure | Behavior |
|---|---|
| Remote unreachable | Nothing user-visible. Backup queue grows; alerts on lag; drains on recovery. |
| Postgres unreachable | Serving continues on rule snapshots + caches (`stale_rules: true`). All writes (drafts, commits, targeting, pointer moves) return `503`. Frozen deterministically. |
| Commit fails validation | Recorded against the SHA; that SHA can never be referenced by a pointer or rule. The draft shows the error; serving never sees invalid content. |
| Rule resolves to something unservable | Skipped, evaluation continues; counted + surfaced. |
| Version's current live SHA unservable (cache lost + store unreachable — compound failure) | **Within-version fallback**: serve the most recent *previous live* SHA of the same version that is still servable. Loud: `content_fallback` in the response, header, error-level logs, `incant_content_fallbacks_total` alerts. Never a different version. |
| Nothing in the version's pointer history is servable | `409` for that request — never substitute another version. |
| Repo volume lost | Restore by cloning a remote (full history, no reconstruction). Between backup pushes, the queue metric bounds the exposure window. |
| Node restart | Caches/spills reload; re-warm before `readyz` goes green. |
| Missing required variable / render error | `422` — caller-input problem, never a fallback trigger. |

Availability posture: **rules freeze** (last-known-good targeting keeps evaluating
through DB outages) and **content never lies** (what served is exactly reported; the
only permitted degradation is stepping back within a version's own pointer history —
loudly — before erroring).

---

## 11. AuthN & RBAC

Incant is the only door; remotes are write-only backup targets with push-only deploy
keys.

### Principals

- **Users** — OIDC (Okta, Entra, Google); server-side DB sessions, revocable
  immediately.
- **Service keys** — bearer API keys: created/revoked in the UI, salted hashes with a
  lookup prefix (`incant_sk_…`), expiry, last-used tracking.

### Roles × scopes

Bindings are `(principal, role, scope)`; scope = instance, project, or
(project, environment).

| Role | Grants |
|---|---|
| `renderer` | Render/evaluate via the serving API (what service keys hold) |
| `viewer` | Read prompts, versions, rules, history; run previews and test contexts |
| `editor` | `viewer` + drafts, test contexts, commits (subject to project review policy); targeting in unprotected environments |
| `operator` | `viewer` + targeting in granted environments: rules, segments, ramps, kill switches, pointer moves, defaults |
| `releaser` | `operator` + approvals for pointer-class changes in protected environments |
| `admin` | Everything: projects, environments, principals, keys, bindings, remotes |

- **Serving:** key holds `renderer` on `(project, environment)` → else `403`. Scoping
  no finer than project — wanting per-prompt render ACLs is the signal to split
  projects.
- **Authoring:** `editor` on the project; commits gated by project review policy
  (N approvals, approver ≠ author).
- **Targeting:** `operator`; protected environments add two-person approval for
  pointer-class changes only (§7). Kill switches: any `operator`, instantly.

Append-only **`audit_log`** for every mutation — commits, rule changes, ramps, kills,
pointer moves, key issuance, role grants: actor, action, object, before/after, at.
Render traffic goes to structured logs/metrics (with the version tuple), not the DB.
Sandboxed rendering, loader confined to registered content; Incant stores no variable
values.

---

## 12. UI

One app, three centers of gravity:

**Author & review.** Project browser → prompt page: version files with tip-vs-live
badges per environment, labels, effective variable schema (auto-extracted, refinable
inline), include graph. Draft editor (Monaco, lint-as-you-type) with the test panel:
saved contexts rendering live, rendered diffs against any version at any SHA. Review
queue with comments on source or rendered output. A "tweak flow" affordance walks the
canonical loop: edit → commit → target to cohort → expand → make live.

**Target & operate.** Per environment: rule list (global + per-prompt,
drag-to-reprioritize), segment editor, ramp sliders writing live, kill switches, live
pointers per prompt/version with advance/revert controls, the per-version pointer
timeline (who made what live, when, why — the blame view), rendered old→new diffs,
approval queue (protected envs), targeting history with one-click rollback. Controls
reflect RBAC.

**Understand.** Experiment view: pick a rule or label → every affected prompt with
rendered before/after (`POST /evaluate` underneath). Per-prompt timeline: versions,
commits, who/when/notes, where each is live. Audit explorer. Backup health (queue
depth, last push per remote).

### Mgmt API (selected)

```
# authoring
GET/POST /mgmt/projects/{p}/prompts            GET /mgmt/prompts/{id}/versions
POST /mgmt/prompts/{id}/versions               # create vN+1 (seeded from a version/SHA)
POST /mgmt/prompts/{id}/drafts                 PUT /mgmt/drafts/{id}/content
POST /mgmt/drafts/{id}/render                  # test-context render, rendered diff
POST /mgmt/drafts/{id}/review  /approve  /commit
GET/PUT /mgmt/prompts/{id}/variables           # refine extracted metadata
GET/PUT /mgmt/prompts/{id}/test-contexts

# targeting & pointers
GET/POST /mgmt/envs/{env}/rules                PATCH /mgmt/envs/{env}/rules/{id}
POST /mgmt/envs/{env}/prompts/{id}:kill        GET/POST /mgmt/envs/{env}/segments
POST /mgmt/envs/{env}/pointers                 # make-live / revert (bulk-capable)
POST /mgmt/envs/{env}/defaults                 # set default versions
GET  /mgmt/envs/{env}/revisions                POST /mgmt/envs/{env}/rollback

# admin
GET/POST /mgmt/projects /mgmt/envs /mgmt/principals /mgmt/keys /mgmt/bindings /mgmt/remotes
GET /mgmt/audit?…
```

---

## 13. Architecture

```
                ┌────────────────────────── Incant node ─────────────────────────┐
                │                                                                 │
                │ GitStore: canonical repo (authoritative content)                │
                │   │   ├─► validation on commit ─► per-SHA results (DB)          │
 git remotes ◄──│   │   └─► backup pusher (queued) ─► remotes                     │
 (backup only)  │   └─► content cache ─► compiled templates (memory, eager-warm)  │
                │                                       │                         │
 Postgres ◄────►│ Control plane: pointers, rules, review, RBAC, audit             │
 (control       │   └─ RulesSync (LISTEN/NOTIFY) ─► snapshots (memory + spill)    │
  plane)        │                                       │                         │
  clients ─────►│ Serving API ─► RBAC ─► Evaluator ─► Renderer ─► response        │
 (services)     │ /prompt/*        (all in-memory on the hot path)                │
                │                                                                 │
  browser ─────►│ Mgmt API + UI ─► RBAC ─► commits (git) + state writes (DB)      │
                └─────────────────────────────────────────────────────────────────┘
```

- **Replicas**: `serve` replicas are stateless + Postgres + a read replica of the
  content cache (rebuildable from the `full` node's repo or a remote); one `full` node
  owns the canonical repo, commits, and backup pushing. Targeting changes propagate via
  NOTIFY in seconds; the DB is never on the per-request path.
- The evaluation/render core is a **pure library** — `(content, rules-as-data, flags,
  variables) → (version, sha, text)`, no I/O — embeddable as `incant.core`,
  exhaustively unit-testable.

### Python stack

| Concern | Choice | Why |
|---|---|---|
| HTTP | FastAPI + uvicorn | pydantic-native models, async |
| Models/validation | pydantic v2 | one definition → API docs + validation + UI forms |
| Templates | jinja2 (`SandboxedEnvironment`, `jinja2.meta`) | the requirement; AST access for variable inference |
| Content | `git` CLI via subprocess | the content store; battle-tested ssh/https transport for backup pushes |
| Database | Postgres + SQLAlchemy core + Alembic | control plane; LISTEN/NOTIFY. SQLite for dev/single-node (poll fallback) |
| Config | pydantic-settings | bootstrap only: DB URL, OIDC, bind, repo path |
| Metrics | prometheus-client | |

```
incant/
├── core/          # pure: evaluation, rendering, variable extraction — no I/O
├── gitstore/      # canonical repo, commits, validation pipeline, backup pusher
├── registry/      # version registry, drafts, reviews, variable refinements
├── targeting/     # rules, segments, pointers, snapshots, propagation
├── server/        # FastAPI: serving API, mgmt API, RBAC middleware
└── ui/            # frontend (built assets served by the server)
```

### Schema sketch (control plane — note: no content anywhere)

```
projects(id, name, review_policy)
prompts(id, project_id, path)                        -- registry of the tree
versions(id, prompt_id, number, label?, status: active|archived, notes, created_by, created_at)
commit_validations(sha, path, status, error?, extracted_variables, validated_at)
variable_refinements(prompt_id, version_number, name, type?, required?, default?, description?)
test_contexts(id, prompt_id, name, flags, variables)
drafts(id, prompt_id, version_number?, base_sha, git_ref, author, status)
reviews(id, draft_id, reviewer, state)  review_comments(...)
environments(id, name, protected, track_tip)
pointer_moves(environment_id, prompt_id, version_number, from_sha?, to_sha,
              moved_by, moved_at, comment)      -- append-only; current live = newest row
env_defaults(environment_id, prompt_id, version_number)
segments(id, environment_id, name, clauses, version)
rules(id, environment_id, scope, prompt_id?, priority, clauses, serve, status, comment)
rule_revisions(rule_id, version, snapshot, actor, at, comment)
env_versions(environment_id, rules_version)
remotes(id, url, auth_ref, enabled, last_pushed_sha, last_push_at)
principals(id, kind, subject, name)  sessions(...)  api_keys(...)
role_bindings(principal_id, role, project_id?, environment_id?)
approvals(id, environment_id, change, proposed_by, approved_by?, status)
audit_log(actor, action, object_type, object_id, before, after, at)
```

---

## 14. Observability

- Latency: `incant_render_seconds` histogram (p99 SLO alert),
  `incant_template_cache_misses_total` (~0 expected).
- Targeting: `incant_renders_total{prompt,version,environment,stale_rules}` ·
  `incant_rules_snapshot_age_seconds{environment}` · `incant_rule_skips_total` ·
  `incant_flag_eval_fallthrough_total` (dead rules) ·
  `incant_content_fallbacks_total{prompt,version,environment}` (page on nonzero) ·
  pointer history is queryable state (§7), not just audit entries.
- Authoring: `incant_commits_total{project}` · `incant_validation_failures_total`.
- Backup: `incant_backup_lag_seconds{remote}` · `incant_backup_queue_depth` — bounds
  the content-durability exposure window.
- Render-path structured logs carry the full version tuple, joinable to LLM traffic.

---

## 15. Deployment

Docker-first: the app image plus Postgres.

```dockerfile
FROM python:3.13-slim
RUN apt-get update && apt-get install -y --no-install-recommends git openssh-client curl \
    && rm -rf /var/lib/apt/lists/*
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv
WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project   # deps layer, cached across code changes
COPY . .
RUN uv sync --frozen --no-dev
EXPOSE 8080
CMD ["uv", "run", "uvicorn", "incant.server:app", "--host", "0.0.0.0", "--port", "8080"]
```

```yaml
# docker-compose.yaml
services:
  incant:
    build: .
    ports: ["8080:8080"]
    depends_on: [db]
    volumes:
      - content:/var/lib/incant/repo               # canonical repo — DURABLE, back up via remotes
      - cache:/var/lib/incant/cache                # caches + spills — rebuildable
      - ./incant.server.yaml:/etc/incant/config.yaml:ro
      - ./secrets/backup_key:/secrets/backup_key:ro # push-only deploy key for backup remotes
      - ./secrets/known_hosts:/etc/incant/known_hosts:ro
    environment:
      INCANT_CONFIG: /etc/incant/config.yaml
      INCANT_DATABASE_URL: postgresql://incant:…@db/incant
      INCANT_MODE: full                            # full | serve (no mgmt/UI; read-only)
    healthcheck:
      test: ["CMD", "curl", "-f", "http://localhost:8080/readyz"]
      interval: 10s
  db:
    image: postgres:17
    volumes: [pgdata:/var/lib/postgresql/data]
    environment: {POSTGRES_DB: incant, POSTGRES_USER: incant, POSTGRES_PASSWORD_FILE: /run/secrets/…}
volumes:
  content:
  cache:
  pgdata:
```

- Migrations (Alembic) run at boot before `readyz`.
- **Two durable things**: the `content` volume (canonical repo; off-site copy =
  backup pushes to remotes) and `pgdata` (control plane; normal Postgres backups). The
  `cache` volume is fully rebuildable.
- `INCANT_MODE=serve` replicas scale horizontally (read-only DB role, no mgmt surface,
  content cache hydrated from the full node or a remote); one `full` instance owns the
  repo, commits, and backup pushing.
- Kubernetes: StatefulSet (or Deployment + PVC) for the full node, Deployment for serve
  replicas, Secret mounts, managed Postgres; `readyz` as the readiness probe.

---

## 16. Build order

1. **`incant.core`** — evaluator (rules as data), sandboxed renderer, variable
   extraction/inference, include resolution; exhaustive unit tests (rule semantics,
   rollout bucketing, AST inference are the fiddly parts).
2. **GitStore + registry** — canonical repo, commit pipeline with validation,
   version-file conventions, drafts/review, version registry.
3. **Serving + targeting** — render/evaluate APIs over eager-warm caches, rules,
   segments, live pointers, propagation, API keys + RBAC + audit. Hit the latency SLO
   here, benchmarks in CI. With 1–3, shippable for API-driven teams.
4. **UI** — author/draft/test/review flow, the tweak-flow loop, rules console, pointer
   controls, admin screens.
5. **Hardening** — approvals for protected environments, backup pusher + restore
   tooling, promote-rules-between-envs, label UX, dashboards.
6. **Later** — SDK clients (Python/TS) with client-side caching and stale-on-fail,
   releases (named bundles of prompt versions promoted together), experiment analytics,
   scheduled ramps, eval hooks.
