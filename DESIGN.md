# Incant — Design

A prompt management platform for **non-devs to author, target, test, and develop
prompts** and for **devs and agents to consume them** — the LaunchDarkly parallel: PMs
define and ramp, services render. **Jinja2** for rendering, **flag-based targeting** for
which version serves to whom, and **git as a backup strategy — nothing else**.

Storage model in one breath:

- **Postgres is the system of record** — prompt content, versions, drafts, reviews,
  variable metadata, targeting rules, segments, environments, RBAC, audit. One database,
  one backup story, one source of truth.
- **Memory is the serving plane** — compiled templates and rule snapshots; the render
  path touches no git, no disk, no DB.
- **Git is the backup artifact** — Incant continuously writes a canonical git repository
  (one commit per publish, real history, human-readable) and pushes it to any configured
  ssh/https remote (GitHub, Bitbucket, GitLab, a bare server). The provider is never in
  the loop: no webhooks, no PR reviews, no ingestion, no provider APIs. If Incant
  disappears tomorrow, the repo is a complete, portable record of every prompt and every
  version.

The core loop: authors draft, test, and publish immutable, human-readable **versions**
(`support/system@v13`); rules evaluate request **flags** to pick which version serves;
Jinja renders it with request **variables**; the response reports exactly what was
served, so any render is reproducible.

---

## 1. Goals

- **Human-readable, immutable versions.** `support/system@v13`, not a SHA. Versions are
  the unit of targeting, review, promotion, and rollback.
- **Flags choose versions.** LaunchDarkly-style rules — segments, percentage rollouts,
  labels for multi-prompt experiments, kill switches — decide per-request which published
  version serves. Many versions of one prompt serve concurrently as a matter of course.
- **Never serve the wrong version.** A request that resolves to v13 gets exactly v13 or
  an error — no silent fallback to "the newest thing that worked". Agents built on
  different prompt versions can behave very differently; substitution is corruption.
- **In-product review, because PR review is the wrong tool.** A PR reviews a *text diff
  of a branch*; prompt review needs to judge *what will be served*: both sides rendered
  with real variables and flags, fragments expanded, saved test contexts executed,
  comments anchored to rendered output — and reviewers who never touch git. The unit of
  review is a version publish, not a merge.
- **Minimal metadata — none on disk, none hand-maintained.** Authors write templates;
  everything else is derived or operational. Variables are extracted from the Jinja AST;
  their types/optionality are refinable metadata in the DB, per version. No manifest, no
  sidecar file to forget.
- **Fast.** Target p50 ~1 ms, **p99 single-digit ms**. Serving is memory-only.
- **Production-grade access control.** First-class RBAC over rendering, authoring,
  publishing, flag operations, promotion, and administration.
- **Git as insurance.** The backup repo is complete enough to rebuild every prompt and
  version without a database restore, readable enough to `git log`/`grep`, and standard
  enough to take elsewhere.

### Non-goals (v1)

- Prompt *execution* (calling LLMs), evals/scoring, cost tracking.
- Provider integration of any kind: no webhooks, no PR/MR APIs, no ingestion of pushes.
  The remote is a passive backup target.
- Serving the git protocol to users. Anyone who wants a repo clones the backup remote.
- Response caching. Rendered output varies per-request; templates and rule state are
  cached aggressively instead (§8).

---

## 2. Concepts

| Term | Meaning |
|---|---|
| **Project** | Top-level namespace and permission scope (`support`, `growth`, `shared`). |
| **Prompt** | A single Jinja template. Its id is a path: `support/system`. Fragments are not a separate type — any prompt can include any other (§4). |
| **Version** | An immutable, validated publish of a prompt: `support/system@v13`. Human-readable number, optional label, full content pinned. |
| **Label** | A name attached to versions across prompts (`voice-v2`) — the coordination handle for multi-prompt experiments. |
| **Draft** | Work-in-progress content based on some version; becomes a version through review + publish. |
| **Flags** | Evaluation context sent with a render request (`{"tier": "pro", "user_id": "u_42"}`). Rules match against these to choose a version. |
| **Variables** | Values interpolated into the chosen template by Jinja. |
| **Environment** | A named targeting scope (`prod`, `staging`): rules, segments, kill switches, and a default version per prompt. Pure DB state — environments hold no content pointer. |
| **Rule / segment** | Per-environment. Rules map flag conditions to versions/labels; segments are named, reusable conditions. |
| **Remote** | A configured git URL that receives backup pushes. Plural, optional, never read from during normal operation. |

Flags choose *which version of* the prompt; variables fill in *the blanks within* it.
Same request, never mixed: flags are not visible to templates unless explicitly passed as
variables too.

---

## 3. Storage model — why the DB owns content

Earlier iterations of this design made git the content store, with the provider handling
review, then with Incant as the committer. Each step removed a provider responsibility;
this design removes the last one. What git was still doing — durable storage and
history — a backup does better *as an output* than as a dependency:

1. **Review and versioning are product features.** Rendered diffs, test contexts,
   non-dev reviewers, per-version variable metadata, labels — none of it maps to
   branches and merges. Once review and version identity live in the DB, git-as-truth
   is a second copy of the same information that must be kept consistent.
2. **Line diffs are the wrong altitude for prompts.** What matters in prompt review is
   the rendered output under real contexts — including what fragments expand to. That is
   an execution, not a diff; a git host can't do it.
3. **Serving never wanted git.** The hot path reads compiled templates from memory;
   the warm path reads content rows from Postgres. A git working tree was plumbing.
4. **Operational simplicity.** No mirror sync loops, no fetch failures, no ingestion
   modes, no webhook verification, no provider adapters. Replicas need exactly one
   shared thing: Postgres.

Where everything lives:

| Concern | Home |
|---|---|
| Template content, per version | **DB** (`prompt_versions.content`, hash-addressed) |
| Version identity, status, labels, authorship, notes | **DB** |
| Drafts, reviews, comments, test contexts | **DB** |
| Variable metadata (extracted + refined) | **DB**, per version |
| Targeting: environments, rules, segments, defaults, kill switches | **DB** |
| Principals, roles, keys, approvals, audit | **DB** |
| Compiled templates, rule snapshots | **Memory** (+ disk spill, rebuildable) |
| Backup: full content history | **Git**, generated by Incant, pushed to remotes |

**The durability trade, stated plainly:** the provider is no longer an authoritative
copy. Durability = Postgres backups (the normal discipline) **plus** the continuously
pushed git backup, which independently preserves all content and version history (§6).
Losing the DB *and* every remote loses targeting/RBAC/audit state; content survives in
any clone of the backup repo.

---

## 4. Content model

### The content tree

Prompts form a tree — projects at the top, prompts beneath, exactly mirrored in the
backup repo's layout:

```
support/                        # project
├── system.j2                   # prompt: support/system
├── greeting.j2                 # prompt: support/greeting
└── escalation/
    └── triage.j2               # prompt: support/escalation/triage
growth/
└── onboarding.j2
shared/                         # a project of shared fragments — just prompts
└── style/
    └── language-rules.j2       # prompt: shared/style/language-rules
```

- **Projects** scope permissions (§11), group the UI, and namespace prompt ids. They are
  DB entities; the monorepo shape above is how they serialize to backup.
- **A prompt is one template. There is no manifest.** The tree contains `.j2` files and
  nothing else.

### No `prompt.yaml` — where every field went

Earlier drafts had a per-prompt manifest. Every job it did has a strictly better home,
so it no longer exists:

| Old manifest field | Now |
|---|---|
| `variables` (declared schema) | **Extracted from the template AST** on every save/publish; refined (types, descriptions, defaults) in the UI; stored in the DB per version. Can't drift — it's derived, not declared. |
| `variants` | **Gone as a concept.** Versions replaced variants: parallel alternatives are labeled published versions, targeted by rules. |
| `default` | **Operational state**: per-environment default version (`env_defaults`), promoted explicitly (or tracking latest where `auto_serve_latest` is on). |
| `description` | Prompt metadata in the DB, edited in the UI. |

A sidecar file has to be *remembered*; everything above is either computed from the
template or belongs to an environment, not to the content. Removing the manifest also
removes its failure mode — editing the prompt and forgetting the sidecar.

### Fragments are prompts

There is no fragment type. Any prompt includes any other by id:

```jinja
{% include "shared/style/language-rules" %}
```

The include resolves **through the targeting engine, with the same flag context, in the
same environment**: rules targeting `shared/style/language-rules` pick its version; no
matching rule → the environment's default version for it. "Roll out the new style rules
to 10% of enterprise" is targeting on the fragment prompt — every consumer follows,
coherently, with no consumer edited.

- Include references are by prompt id. Includes may cross projects (RBAC governs who can
  *edit*, not who can include; a render needs `renderer` on the entry prompt's project).
- **Cycles:** publish-time static check over the include graph at current default
  versions, plus a render-time depth limit (32) as backstop — resolution is
  flag-dependent, so the static check alone can't be exhaustive.
- Every render response reports the resolved version of the prompt *and every included
  prompt* (§9) — fragment indirection never costs reproducibility.

### Variables — extracted, not declared

On every draft save and publish, Incant parses the template (`jinja2.meta` over the AST)
and derives:

- the **variable set** — every undeclared name the template references;
- **optionality inference** — a variable used only inside guards (`{% if var %}`,
  `{{ var | default(...) }}`) is inferred *optional*; otherwise *required*;
- the **effective schema** — the union over the include closure (resolved against
  default versions for display; against the actually resolved versions at render time
  for validation).

Extracted schemas are stored **per version** and surfaced in the UI and `GET /prompts`,
where humans refine what inference can't know: types, descriptions, default values,
optionality overrides. The template is the single source of *which* variables exist; the
DB holds what they *mean*.

Render-time validation runs against the resolved versions' metadata: missing required
variable → `422` naming it; defaults applied before render, so `StrictUndefined` stays
on and conditionals just work.

---

## 5. Versions, drafts, and review

### Versions

- Publishing creates the next version in the prompt's sequence: `v1, v2, …` — per
  prompt, monotonic. Optional **label** (`voice-v2`) attachable at publish or later.
- A version is **immutable**: exact content (hash-addressed), extracted variable schema,
  include references, author, notes. Validated once, at publish: Jinja compiles,
  includes resolve, cycle check, strict render against the prompt's test contexts.
- Status: `published → archived`. Archived versions stop being targetable by *new* rules
  but keep serving existing pins — immutability is forever.

### Drafts → test → review → publish

All in-product; git is nowhere in this flow:

1. **Draft**: branch from any version — a DB row, autosaved. Monaco editor, Jinja
   highlighting, lint-as-you-type (the same validation code as publish).
2. **Test**: every prompt carries saved **test contexts** (named `{flags, variables}`
   sets). The draft view renders them live — fragments expanded, targeting evaluated —
   and diffs *rendered output* side-by-side against any published version. Reviewers
   judge what will actually be served, not a line diff of template source (that view
   exists too, one tab over).
3. **Review**: assigned reviewers, comments anchored to template source *or rendered
   output*, approve / request changes. Per-project policy: publishing requires N
   approvals (0 for scratch projects, ≥1 for anything real).
4. **Publish**: assigns `vN+1` atomically with validation; the version is immediately
   targetable (and immediately serving wherever `auto_serve_latest` is on — §7). If the
   base version is no longer the latest, the publisher sees the intervening diff and
   confirms (optimistic concurrency; the older version stays targetable regardless, so
   no outcome loses work).

Devs and agents get the same flow via the mgmt API — create draft, put content, publish —
with the same validation, review policy, and audit trail. There is deliberately no
side door through git.

---

## 6. The git backup

Incant maintains one canonical bare repository (regenerable, on the cache volume) and
pushes it to every configured remote.

- **One commit per publish**, written by a single background worker (Postgres advisory
  lock elects it) consuming the publish log in order. The tree mirrors the content tree
  (§4); the commit is authored as the publishing user, committed by Incant, and carries
  structured trailers:

  ```
  Publish support/system v13

  Voice v2 rollout candidate — EXP-142

  Incant-Prompt: support/system
  Incant-Version: 13
  Incant-Label: voice-v2
  Incant-Draft: 4821
  ```

- **Push is asynchronous and queued** (queue state in the DB). Remote down → queue depth
  grows (`incant_backup_lag_seconds` alerts), nothing else happens. Remote back → drain.
- **Regenerable**: the repo is derived from the DB, so losing the volume just means
  rebuilding it (commit hashes change on regeneration; remotes are force-pushed with a
  lineage note — acceptable for a backup artifact, and the trailers keep identity
  stable).
- **Restore paths**: normal DR is a Postgres restore. If the DB is unrecoverable,
  `incant restore --from <remote>` rebuilds projects, prompts, all versions, labels,
  authorship, and notes from any clone — that's what the trailers are for. Targeting,
  RBAC, and audit exist only in the DB; only a DB backup brings those back. Stated
  trade, see §3.
- **Escape hatch**: the backup is an ordinary repo — clone it, `git log` a prompt's
  history, grep it, or leave the platform with it. Read-only by design; pushes to it by
  anything other than Incant are ignored (Incant force-pushes its own lineage).

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

- **Prompt-scoped rules** serve a specific version (`{"version": 13}`) or the
  environment default.
- **Global rules** serve a **label**. A prompt participates iff some published version
  of it carries that label; non-participants skip the rule and continue. A multi-prompt
  experiment = publish labeled versions of the affected prompts + one global rule.
  Killing it = archive one rule; every participant reverts in one propagation tick.
- Evaluation order per prompt: global rules → prompt rules → **environment default
  version** (explicitly promoted, or newest publish where `auto_serve_latest: true` —
  the dev/staging convenience; off for prod). First match wins.

### Clause & rollout semantics

- Operators: `eq`, `neq`, `in`, `not_in`, `contains`, `starts_with`, `ends_with`,
  `matches`, `gt/gte/lt/lte`, `semver_gt/semver_lt`, `exists`; `all`/`any` composition.
  A clause referencing a flag absent from the request does not match (no error).
- **Segments**: named clause groups per environment, referencable from any rule.
- **Rollout bucketing is coherent across prompts for global rules**:
  `sha256(f"{rule.id}:{bucket_value}")` — no prompt id — so a user in the experiment
  sees it in every participating prompt. Prompt-scoped rules hash
  `sha256(f"{prompt_id}:{rule.id}:{bucket_value}")`. Rule ids are immutable, so ramps
  are monotonic (the 20% cohort stays inside the 50%) and reordering never reshuffles
  assignments. Missing `bucket_by` flag → rule falls through (sticky assignment needs a
  key; we don't randomize).

### Kill switches

Per environment, per prompt: one click forces the environment default version, bypassing
all rules. The fail-safe direction never waits for approval, even in protected
environments; re-enabling follows normal rules. Loud in the UI and audit log.

### Integrity

- A rule serving a version/label that doesn't exist (or that some prompts lack, for
  global rules) is rejected/warned at write time with the participant list.
- Archiving a version still referenced by active rules requires acknowledging them.
- Eval-time backstop: a rule resolving to something unservable for a prompt is skipped,
  counted (`incant_rule_skips_total`), surfaced in the UI.

### Lifecycle, audit, rollback, approvals

Every targeting mutation snapshots to `rule_revisions` (actor, at, comment) and bumps the
environment's monotonic **`rules_version`** — full history, one-click rollback of a rule
or the whole environment, and reproducible targeting state (the `rules_version` returns
in every render). `protected` environments (prod) gate targeting changes and
default-version promotion behind propose → approve (approver ≠ proposer); kill switches
exempt.

### Propagation

Nodes hold per-environment rule snapshots in memory; Postgres `LISTEN/NOTIFY` (2s poll
fallback) signals `rules_version` bumps. Target: a flag change serves everywhere in
**< 2 s**. Snapshots spill to disk; a DB outage freezes targeting at last-known-good
(§10).

---

## 8. Rendering & performance

### Semantics

- **Jinja2 `SandboxedEnvironment`** — no unsafe attribute access, no code execution.
- **`StrictUndefined`** — a missing variable is a `422` naming it, never a silent empty
  string. DB-held defaults apply before render, so optional variables and
  `{% if var %}` conditionals work under strictness.
- **Autoescape off** — output is plain text for LLMs.
- Includes resolve through targeting (§4) to exact versions; the loader serves only
  registered version content — never a filesystem.

### The hot path is memory-only

Budget: API-key check (in-memory verified-key cache) → rule evaluation (in-memory
snapshot, pure function, µs) → template render (compiled `jinja2.Template`, sub-ms
typical) → response. **p50 ~1 ms, p99 single-digit ms.** No git, no disk, no DB per
request.

- **Compiled-template cache** keyed by content hash — immutable, LRU-evicted, never
  invalidated. Warm-miss fallback reads the content row from Postgres (ms, rare).
- **Eager warm**: every version reachable from any environment's targeting (defaults,
  rule targets, their include closures) precompiles at boot and on publish/rule-change.
  `incant_template_cache_misses_total` should sit at zero.
- Fragment includes hit the same cache: a render touching five fragments is five hash
  lookups, not five compilations.

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
    "versions": {"support/system": 13, "shared/style/language-rules": 3},
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
    "support/system": {"version": 13, "label": "voice-v2"},
    "shared/style/language-rules": {"version": 3}
  },
  "environment": "prod",
  "rules_version": 4172,
  "stale_rules": false               // true iff targeting is frozen at last-known-good (§10)
}
```

The `versions` map + `rules_version` is the reproducibility tuple — log it beside LLM
calls; feed it back as `pin` to replay exactly. There is no content staleness field:
versions are immutable, and a version that can't be served errors rather than degrades.

Errors: `401/403` bad or under-scoped credential · `404` unknown prompt/environment ·
`409` resolved version unservable (requires DB loss mid-request — see §10) · `422`
variable/render failure (naming the variable or line) · `503` node not ready.

### Supporting endpoints

```
POST /prompt/{prompt_id}/evaluate   # flags only → resolved version (no render)
POST /evaluate                      # flags → resolved version for EVERY prompt in the env;
                                    #   "what does this experiment change?" in one call
GET  /prompts?environment=…         # prompt list: descriptions, effective variable schemas,
                                    #   current defaults + labels
GET  /prompt/{prompt_id}/versions   # version history: number, label, author, notes, dates
GET  /healthz  /readyz  /metrics
```

---

## 10. Availability & failure modes

Node state: rule snapshots (memory + disk spill), compiled-template cache + content
cache (memory + disk, rebuildable from DB), and — on the backup-writer node — the
canonical repo and push queue. Postgres is authoritative for everything and sits on the
*refresh* and *write* paths only, never per-request.

| Failure | Behavior |
|---|---|
| Git remote unreachable | Nothing user-visible. Backup queue depth grows; alerts fire on lag; drains on recovery. |
| Postgres unreachable | Serving continues on rule snapshots + template caches (`stale_rules: true`). All writes (drafts, publishes, targeting) return `503`. Frozen deterministically: no rule can change, so none can misfire. |
| Publish fails validation | No version created; the draft shows the error. Serving never sees invalid content — validation is at the door, not on the serving path. |
| Rule targets something unservable for a prompt | Rule skipped for that prompt, evaluation continues; counted + surfaced. |
| Resolved version's content not in any cache and DB down | `409` for that request — **never substitute another version**. Requires losing warm caches during a DB outage (eager warm makes this a compound failure). |
| Cache volume lost | Caches rebuild from the DB; the canonical backup repo regenerates from the DB. Cold boot gated behind `readyz`. Nothing durable lived there. |
| Node restart | Spills + caches reload; re-warm before `readyz` goes green. |
| Missing required variable / render error | `422` — caller-input problem, never a version-fallback trigger. |

The availability posture in two properties: **rules freeze** (last-known-good targeting
keeps evaluating through DB outages) and **content never lies** (immutable versions serve
exactly or error exactly).

---

## 11. AuthN & RBAC

Incant is the only door. There is no provider surface to reconcile with — the backup
remote is write-only from Incant's side, with a deploy key that exists solely to push.

### Principals

- **Users** — OIDC (Okta, Entra, Google). Server-side sessions in the DB, revocable
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
| `editor` | `viewer` + drafts, test contexts, publish (subject to project review policy); targeting in unprotected environments |
| `operator` | `viewer` + full targeting control in granted environments: rules, segments, ramps, kill switches, default promotion |
| `releaser` | `operator` + approvals in protected environments (targeting, promotion, and publishes where policy requires) |
| `admin` | Everything: projects, environments, principals, keys, bindings, remotes |

In practice:

- **Serving:** key holds `renderer` on `(project, environment)` → else `403`. Scoping is
  deliberately no finer than project — wanting prompt-level render ACLs is the signal to
  split projects.
- **Authoring:** `editor` on the project; publish additionally gated by the project's
  review policy (N approvals, approver ≠ author).
- **Targeting:** `operator` per environment; protected environments add the two-person
  rule via `releaser`. Kill switches: any `operator`, instantly.
- **Promotion** (default-version changes in protected envs): `releaser`, with the
  rendered old→new diff attached to the approval.

Append-only **`audit_log`** for every mutation: drafts published, rules changed, ramps,
kills, promotions, key issuance, role grants — actor, action, object, before/after, at.
Render traffic goes to structured logs/metrics (with the version tuple), not the DB.

Templates render in the sandbox with the loader confined to registered content (§8).
Incant stores no variable values — nothing sensitive at rest beyond the credentials it
holds by design (OIDC client, deploy keys, key hashes).

---

## 12. UI

One app, three centers of gravity:

**Author & review.** Project browser → prompt page: versions and labels, live serving
status per environment, effective variable schema (auto-extracted, refinable inline),
include graph. Draft editor (Monaco, lint-as-you-type) with the **test panel**: saved
test contexts rendering live, fragments expanded, side-by-side *rendered* diffs against
any version (source diff one tab over). Review queue: comments on source and rendered
output, approvals, publish — RBAC- and policy-aware.

**Target & operate.** Per environment: rule list (global + per-prompt,
drag-to-reprioritize), segment editor, ramp sliders writing live, kill switches, default
promotion with rendered diff, approval queue, revision history with one-click rollback.
Controls reflect RBAC — an `editor` sees prod read-only; an `operator` gets sliders.

**Understand.** The experiment view: pick a rule or label, see every affected prompt
with rendered before/after (`POST /evaluate` underneath). Version history per prompt —
author, notes, label, where it's serving. Audit log explorer. Backup health (queue
depth, last push per remote).

### Mgmt API (selected)

```
# authoring
GET/POST /mgmt/projects/{p}/prompts            GET /mgmt/prompts/{id}/versions
POST /mgmt/prompts/{id}/drafts                 PUT /mgmt/drafts/{id}/content
POST /mgmt/drafts/{id}/render                  # test-context render, rendered diff
POST /mgmt/drafts/{id}/review  /approve  /publish
GET/PUT /mgmt/versions/{id}/variables          # refine extracted metadata
GET/PUT /mgmt/prompts/{id}/test-contexts

# targeting
GET/POST /mgmt/envs/{env}/rules                PATCH /mgmt/envs/{env}/rules/{id}
POST /mgmt/envs/{env}/prompts/{id}:kill        GET/POST /mgmt/envs/{env}/segments
POST /mgmt/envs/{env}/defaults                 # promote default versions (bulk-capable)
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
 Postgres ◄────►│ Registry: content, versions, drafts, reviews, variables, RBAC   │
 (system of     │     │                                                           │
  record)       │     ├─► compiled-template + content caches (memory, eager-warm) │
                │     │                                                           │
                │ RulesSync (LISTEN/NOTIFY) ─► rule snapshots (memory + spill)    │
                │     │                                                           │
  clients ─────►│ Serving API ─► RBAC ─► Evaluator ─► Renderer ─► response        │
 (services)     │ /prompt/*        (all in-memory on the hot path)                │
                │                                                                 │
  browser ─────►│ Mgmt API + UI ─► RBAC ─► registry/targeting writes ─► NOTIFY    │
  (humans)      │                                                                 │
                │ BackupWriter (one node, via advisory lock):                     │
 git remotes ◄──│   publish log ─► canonical repo ─► queued pushes                │
 (backup only)  └─────────────────────────────────────────────────────────────────┘
```

- **Replicas are stateless; Postgres is the one shared component.** Caches and spills
  are per-node and rebuildable. The DB is never on the per-request path, so standard
  Postgres HA suffices; its outage means frozen targeting and blocked writes, not
  downtime.
- The evaluation/render core is a **pure library** — `(content, rules-as-data, flags,
  variables) → (version, text)`, no I/O — embeddable as `incant.core`, exhaustively
  unit-testable.

### Python stack

| Concern | Choice | Why |
|---|---|---|
| HTTP | FastAPI + uvicorn | pydantic-native models, async |
| Models/validation | pydantic v2 | one definition → API docs + validation + UI forms |
| Templates | jinja2 (`SandboxedEnvironment`, `jinja2.meta` for extraction) | the requirement; AST access for variable inference |
| Database | Postgres + SQLAlchemy core + Alembic | system of record; LISTEN/NOTIFY for propagation. SQLite for dev/single-node (poll fallback) |
| Backup | `git` CLI via subprocess | commit + push only; the most battle-tested ssh/https transport |
| Config | pydantic-settings | bootstrap only: DB URL, OIDC, bind — everything else lives in the DB |
| Metrics | prometheus-client | |

```
incant/
├── core/          # pure: evaluation, rendering, variable extraction — no I/O
├── registry/      # content, versions, drafts, reviews, variable metadata
├── targeting/     # rules, segments, snapshots, propagation
├── backup/        # canonical repo writer, push queue, restore
├── server/        # FastAPI: serving API, mgmt API, RBAC middleware
└── ui/            # frontend (built assets served by the server)
```

### Schema sketch

```
projects(id, name, review_policy)
prompts(id, project_id, path)
prompt_versions(id, prompt_id, number, label?, content, content_hash,
                status: published|archived, created_by, notes, published_at)
version_variables(version_id, name, type?, required, default?, inferred, description?)
test_contexts(id, prompt_id, name, flags, variables)
drafts(id, prompt_id, base_version, content, author, status, updated_at)
reviews(id, draft_id, reviewer, state)  review_comments(...)
environments(id, name, protected, auto_serve_latest)
env_defaults(environment_id, prompt_id, version_id)
segments(id, environment_id, name, clauses, version)
rules(id, environment_id, scope, prompt_id?, priority, clauses, serve, status, comment)
rule_revisions(rule_id, version, snapshot, actor, at, comment)
env_versions(environment_id, rules_version)
remotes(id, url, auth_ref, enabled, last_pushed_publish, last_push_at)
principals(id, kind, subject, name)  sessions(...)  api_keys(...)
role_bindings(principal_id, role, project_id?, environment_id?)
approvals(id, environment_id, change, proposed_by, approved_by?, status)
audit_log(actor, action, object_type, object_id, before, after, at)
```

---

## 14. Observability

- Latency: `incant_render_seconds` histogram (alert on the p99 SLO),
  `incant_template_cache_misses_total` (should be ~0).
- Targeting: `incant_renders_total{prompt,version,environment,stale_rules}` ·
  `incant_rules_snapshot_age_seconds{environment}` · `incant_rule_skips_total` ·
  `incant_flag_eval_fallthrough_total` (dead rules).
- Authoring: `incant_publishes_total{project}` · `incant_publish_validation_failures_total`.
- Backup: `incant_backup_lag_seconds{remote}` · `incant_backup_queue_depth` — the only
  signals the git side needs.
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
      - cache:/var/lib/incant                       # caches + canonical backup repo; rebuildable
      - ./incant.server.yaml:/etc/incant/config.yaml:ro
      - ./secrets/backup_key:/secrets/backup_key:ro # push-only deploy key; needed only if remotes configured
      - ./secrets/known_hosts:/etc/incant/known_hosts:ro
    environment:
      INCANT_CONFIG: /etc/incant/config.yaml
      INCANT_DATABASE_URL: postgresql://incant:…@db/incant
      INCANT_MODE: full                             # full | serve (no mgmt/UI; read-only DB role)
    healthcheck:
      test: ["CMD", "curl", "-f", "http://localhost:8080/readyz"]
      interval: 10s
  db:
    image: postgres:17
    volumes: [pgdata:/var/lib/postgresql/data]
    environment: {POSTGRES_DB: incant, POSTGRES_USER: incant, POSTGRES_PASSWORD_FILE: /run/secrets/…}
volumes:
  cache:
  pgdata:
```

- Migrations (Alembic) run at boot before `readyz`.
- **`pgdata` is the system of record — back it up**; the git remotes are the independent
  second copy of content. The `cache` volume is fully rebuildable; losing it costs a
  cold start behind `readyz`, never data.
- `INCANT_MODE=serve` replicas scale horizontally (read-only DB role, no mgmt surface,
  no backup writer); one `full` instance carries the UI, writes, and backup pushing.
- Kubernetes: Deployment + PVC (or emptyDir, accepting cold starts) + Secret mounts +
  managed Postgres; `readyz` as the readiness probe.

---

## 16. Build order

1. **`incant.core`** — evaluator (rules as data), sandboxed renderer, variable
   extraction/inference, include resolution; exhaustive unit tests (rule semantics,
   rollout bucketing, AST inference are the fiddly parts).
2. **Registry + serving** — Postgres schema, versions/drafts/publish pipeline with
   validation, render/evaluate APIs over eager-warm caches, rule snapshots +
   propagation, API keys + RBAC + audit. Hit the latency SLO here, benchmarks in CI.
   With 1–2, Incant is shippable for API-driven teams.
3. **UI** — author/draft/test/review/publish flow; rules console (ramps, kills,
   promotion); admin screens.
4. **Backup** — canonical repo writer, push queue, `incant restore`, backup health in
   UI. (Late deliberately: until here the DB backup alone carries DR.)
5. **Hardening** — approvals for protected environments, promote-rules-between-envs,
   rules export/import, label management UX, dashboards.
6. **Later** — SDK clients (Python/TS) with client-side caching and stale-on-fail,
   releases (named bundles of prompt versions promoted together), experiment analytics,
   scheduled ramps, eval hooks.
