# Incant — Divergences from the design

Where the implementation (branch `implement-design`, reviewed 2026-07-08) differs from
its two sources of truth: **DESIGN.md** and the **UI design comp** (`Incant App.dc.html`,
claude.ai/design project `100e92fa-0a63-4f34-b7da-0a2aff3fd8b3`). Every high-severity
item was verified directly against the code; line numbers are as of the review commit
and will drift.

Severity: **P0** breaks a guarantee the design states explicitly · **P1** required
behavior missing or materially wrong · **P2** deviation worth a deliberate decision ·
**P3** minor/cosmetic.

---

## Fix log

- **Batch 1 — P0 one-liners (done)**: git commit dates now real wall-clock, pinned
  only under the `INCANT_FIXED_GIT_DATE` test hook (§1.3); `commit_version` writes
  `refs/heads/main` via compare-and-swap with retry + an in-process serialization
  lock, so concurrent publishers never strand a commit (§1.4); expired API keys are
  rejected (§2.8); `upsert_rule`/`set_rule_status` refuse to touch a rule from
  another environment (§1.7 cross-env capture); the DB-outage `stale` flag is a copy,
  so it clears on recovery (§3); unknown environment on `POST /evaluate` and
  `GET /prompts` now returns 404, not 500 (§3). Tests added in `test_gitstore.py`,
  `test_server.py`, `test_integration.py`.

- **Batch 2 — hot-path outage tolerance (done)** (§1.1, §8, §10, §15): the render
  path now does zero per-request DB round-trips. API keys are served from an
  in-memory `AuthCache` (TTL refresh + throttled on-miss reload) that keeps
  authenticating through a Postgres outage; the per-request `last_used_at` write is
  gone and serving uses a read-only, never-committing session (§15). Optional-variable
  refinement defaults are folded into the `EnvSnapshot`, so `serve()` reads them from
  memory. The `rules_version` poll was already outage-tolerant; together these make an
  actual `docker compose stop db` keep serving `HTTP 200 stale_rules:true` (verified in
  the container, including rule resolution), clearing on recovery. Key issuance and
  refinement edits invalidate the auth/snapshot caches. Tests: auth-survives-outage,
  serve-during-outage, created-key-immediately-valid, serving-path-does-not-write.

- **Batch 3 — content strictness (done)** (§1.2, §2.2): the render environment is
  `StrictUndefined` everywhere again — a missing *required* variable raises (→ 422)
  even inside a filter (`{{ history | length }}`) or an inline-if
  (`{{ 'yes' if x else 'no' }}`), closing the silent-wrong-content path. Guarded-
  optional variables still render: inference now computes the optional set across the
  whole include closure and the renderer injects Jinja's lenient base `Undefined` for
  exactly those names, so `{% if x %}`/`{% for m in x %}` (including inside fragments)
  render while everything else stays strict. Inference and render now agree on
  comparison tests — `{% if tier == 'pro' %}` makes `tier` **required** (a comparison
  against undefined raises), fixing the mirror bug and its wrong test assertion.
  Validation gained the missing test-context render (§2.2/§5): `validate_source` now
  strict-renders against a prompt's test contexts, so a template that compiles but
  fails at render is recorded *invalid*, never pointer-referenceable. Tests added in
  `test_render.py`, `test_variables.py`, `test_integration.py`.

- **Batch 4 — governance from the authenticated principal (done)** (§1.6, §1.7):
  identities are no longer client-supplied strings — commit author and review
  reviewer are the authenticated principal, so self-approval can't be spoofed (a
  principal's review of its own draft doesn't count toward policy). The protected-env
  flow is now real: an **operator proposes**, a **releaser (≠ proposer) approves** via
  new `GET/POST /mgmt/envs/{env}/approvals[/{id}/approve|reject]` endpoints; approving
  advances the live pointer. The body-supplied `approver` is gone; `force` is a
  break-glass direct release **gated to releaser** at the route. RBAC scope holes
  closed: `Identity.has` no longer lets a project-scoped binding satisfy an
  instance-wide (project=None) check, and global-rule create/patch require env-wide
  operator — so a project operator can't govern other projects or create global rules.
  UI: removed the hardcoded `force:true` on make-live/revert and the fake
  `author/reviewer` strings; added an **Approvals** screen (queue + approve/reject).
  Verified end-to-end in Docker (operator propose → force-denied → admin approve →
  pointer live) and in the browser. Tests in `test_server.py` (+ two-principal review).
  *Remaining:* env-**default** changes still apply directly on protected envs (releaser-
  gated) rather than via propose→approve (§3) — deferred to avoid seed churn.

- **Batch 5 (in progress) — §1.5 within-version fallback wired (done)**: the §10
  fallback can now actually fire. The evaluator's `servable` predicate only knew about
  validation, so a SHA that is validated but *unfetchable* (cache lost + store
  unreachable — the real §10 trigger, surfacing as `KeyError` from `ContentStore.get`)
  went straight to 409. Content fetching now applies the fallback at the fetch point:
  for a *live* resolution (root or include), if the resolved SHA's content can't be
  fetched, serve the newest previous-live SHA whose content IS available and flag
  `content_fallback`. Pinned-SHA and tip resolutions never degrade (still 409). Tests
  in `test_render.py`. **All P0 guarantee-breaking bugs (§1.1–§1.7) are now closed.**
  *Still open in §2 (larger, mostly design phase-4/5 additive):* pin replay (§1.8),
  targeting revisions/rollback (§2.4), track_tip consumption (§2.3), effective-schema
  closure in the mgmt API (§2.10 — closure walk already exists in the renderer), plus
  backup pusher, OIDC, Alembic, and the remaining metrics.

---

## 1. Guarantee-breaking bugs (P0)

### 1.1 A Postgres outage takes down serving
§10 promises "Postgres unreachable → serving continues on rule snapshots
(`stale_rules: true`)". The freeze exists (`incant/service.py:85-92`) but is
unreachable: every render performs three unguarded DB round-trips first —

- API-key auth SELECT **plus a `last_used_at` write** (`incant/server/auth.py:78-86`,
  session from `incant/server/deps.py:15-38`)
- per-request `rules_version` poll (`incant/service.py:76`)
- refinement-defaults SELECT (`incant/service.py:120-130`, called at `:184`)

DB down → `OperationalError` → 500 on every request. Also violates §8 ("no git, disk,
or DB per request; key check in-memory") and §15 (read-only-role `serve` replicas
write on every request). There is **no LISTEN/NOTIFY** and no 2 s poller anywhere;
propagation meets < 2 s only because the DB is polled per request.

### 1.2 Missing required variables can render silently
§8: missing required variable → 422 naming it, with `StrictUndefined`. The
implementation's `GuardUndefined` (`incant/core/render.py:26-47`) overrides
`__bool__`/`__iter__`/`__len__`, which apply in **all** positions, not just the guard
forms the inference recognizes. Verified:

- `{{ history | length }}` with `history` missing (inference: **required**) renders
  `"0"` instead of raising.
- `{{ 'yes' if x else 'no' }}` with `x` missing (inference: **required**) renders `"no"`.

The LLM silently receives wrong content — the exact failure class §1's "never serve
the wrong content" excludes. Mirror bug: `{% if tier == 'pro' %}` infers `tier`
*optional* (`incant/core/variables.py:60-67` never descends into if-tests) but render
raises on it; `tests/test_variables.py:39-42` asserts the wrong half.

### 1.3 Every git commit is dated 14 Nov 2023, forever
`_author_env` unconditionally pins `GIT_AUTHOR_DATE`/`GIT_COMMITTER_DATE` to
`1700000000 +0000` (`incant/gitstore/store.py:85-94`); the comment claims wall-clock
applies "if these are unset", but they are always set. §3's git-native history
(`git log` a version file, backport chronology) and every who/when field in the
API/UI report the frozen epoch. Fix: pin only under a test hook.

### 1.4 Lost-update race on `refs/heads/main`
`commit_version` reads `head()` (`store.py:215`) and `_commit_file` runs
`update-ref refs/heads/main <new>` with no expected-old-value CAS (`store.py:191`).
Two concurrent publishers (FastAPI threadpool) can strand a validated commit
unreachable from `main` — prunable by `git gc` while pointers/rules may still
reference it. Fix: `update-ref <ref> <new> <expected-old>` + retry.

### 1.5 The §10 within-version fallback can never fire
The snapshot's `servable` predicate is "has a valid `commit_validations` row"
(`incant/targeting/snapshot.py:131`) — always true for live SHAs since `make_live`
refuses unvalidated ones (`incant/targeting/service.py:162-163`). The actual §10
trigger (cache lost + store unreachable) surfaces as `KeyError` from
`ContentStore.get` and maps straight to **409** (`incant/service.py:194-195`) without
consulting `previous_live`. All fallback machinery (previous-live plumbing,
`content_fallback` flags, warming) is built and dead.

### 1.6 Governance is advisory end to end
§7/§11: protected envs gate pointer-class changes behind propose → approve,
approver ≠ proposer.

- `force` and `approver` are client-controlled body fields
  (`incant/server/schemas.py:103-104`); `force` skips both the proposal and the
  approver≠proposer check, and `approver` is never authenticated nor checked for
  `releaser` (`incant/targeting/service.py:169-184`).
- The UI **hardcodes `force: true`** on make-live and revert
  (`incant/ui/app.js:708,717`) while displaying "proposes for approval" next to the
  button.
- Proposals are write-only: `Approval` rows are created but **no endpoint lists or
  approves them** — the flow cannot complete except via `force`.
- The pointers route requires `releaser` outright in protected envs
  (`incant/server/mgmt.py:660-662`), so an operator cannot even propose — inverting
  §7's operator-proposes / releaser-approves split.
- Review identities come from the request body: the UI sends `reviewer: "reviewer"`
  and commits as `author: "you"` (`app.js:671,681,688`) — self-approval works, and
  git/audit attribution is garbage. Root cause: mgmt bodies carry free-string
  identities instead of the authenticated principal.

### 1.7 RBAC scope holes
- **Cross-environment rule capture**: `upsert_rule`/`set_rule_status` fetch rules by
  bare primary key with no `environment_id` check
  (`incant/targeting/service.py:81,100`). An operator authorized only on `staging`
  can rewrite or archive a **prod** rule via the staging URL — and the *wrong env's*
  `rules_version` is bumped, so warm prod nodes keep old targeting while fresh nodes
  drop it.
- **Project-scoped operator → env-wide power**: `Identity.has` skips the project
  restriction when the check passes `project=None` (`incant/server/auth.py:51`), and
  global-rule creation checks project only when `prompt_id` is set
  (`mgmt.py:559-561`). A `(operator, project=support, env=prod)` binding can create
  global rules governing every project in prod.

### 1.8 `pin` replay is absent
§9's reproducibility contract — feed `versions` + `rules_version` back as `pin` to
replay exactly — cannot be exercised: `RenderRequest` has only
flags/variables/environment (`schemas.py:12-15`) and nothing renders at pinned
versions or targeting state. The `X-Incant-Content-Fallback` header is also never set.

---

## 2. Missing subsystems (P1)

| # | Requirement | State |
|---|---|---|
| 2.1 | **Backup pusher** (§6): async queued pushes to remotes, lag metrics, force-push-own-lineage, `/mgmt/remotes`, restore tooling (registry rebuild from tree + trailers) | Entirely absent; `models.Remote` referenced by nothing. §16 defers this to phase 5, but §3's durability story does not hold until then — the repo volume is a single point of content loss. |
| 2.2 | **Validation = compile + includes + cycles + strict render against test contexts** (§5) | Test-context render missing; `gitstore/validation.py:3-5` documents parameters that don't exist. A template that compiles but fails at render is recorded `valid` and pointer-referenceable. |
| 2.3 | **`track_tip`** (§7): live pointers auto-follow validated tips | Stored, surfaced, copied into the snapshot — consumed by no code. `seed.py:111` works around it manually. |
| 2.4 | **Targeting revisions/rollback** (§7, §12): `GET /mgmt/envs/{env}/revisions`, `POST /mgmt/envs/{env}/rollback` | `rule_revisions` rows are written (`targeting/service.py:52-55`) and read by nothing; neither endpoint exists. |
| 2.5 | **Write-time rule integrity** (§7): rules may only reference validated SHAs / existing versions / labels; global rules warned with participant list | `upsert_rule` only shape-parses (`targeting/service.py:79`). Only the eval-time skip backstop exists — and `incant_rule_skips_total` is defined but never incremented (`metrics.py:16`; skips are collected by core and dropped in `service.serve`). |
| 2.6 | **Archived-status enforcement** (§5, §7): no new commits, no new rules; archive requires acknowledging referencing rules | No status/label/notes mutation path exists at all; nothing checks `Version.status` on draft/commit/rule creation. |
| 2.7 | **OIDC users + DB sessions** (§11, §13 `sessions(...)`) | Absent; bearer API keys are the only authentication. |
| 2.8 | **Key lifecycle** (§11): expiry, revocation, listing | `expires_at` exists but is **never checked** — expired keys authenticate forever (`auth.py:78-84`); no revoke/list endpoints; creation can't set expiry. Hash is unsalted sha256 (`auth.py:65-66`) despite §11 and the model's own comment saying "salted". |
| 2.9 | **Audit for every mutation** (§11) | Only targeting mutations audited. Commits, drafts, refinements, project/env creation, **key issuance, and role grants** write no audit rows. |
| 2.10 | **Effective schema = union over the include closure** (§4) | No closure computation anywhere; `mgmt.py:96-124` reads only the prompt's own variables. A fragment's required variable is invisible until it 422s at render. |
| 2.11 | **Alembic migrations at boot** (§15) | `Base.metadata.create_all` (`db.py:71-74`); no upgrade path, Alembic not a dependency. |
| 2.12 | **Metrics** (§14) | Missing: `incant_template_cache_misses_total` (counters exist as plain ints, unexported), `incant_rules_snapshot_age_seconds`, `incant_flag_eval_fallthrough_total`, `incant_backup_lag_seconds`, `incant_backup_queue_depth`. `renders_total`/`content_fallbacks_total` lack the `version` label. |
| 2.13 | **Eager warm on commit/targeting change; precompile** (§8) | Warm at boot only; mutations just invalidate the snapshot. `precompile()` (`core/render.py:172`) has zero call sites — first render of each blob compiles on the hot path. Commit warms one SHA, not its include closure. |
| 2.14 | **Render-path structured logs with the version tuple** (§14); disk spill (§8/§10) | No serving-path logging; the compose `cache` volume is mounted but nothing writes to it. |
| 2.15 | **Review comments** (§5, §12) | No comments API server-side and no comments UI — missing end-to-end (`review_comments` table exists). |

---

## 3. Behavioral deviations (P2 unless noted)

- **Optimistic concurrency** (§5): conflict is detected and 409s, but the response
  carries no intervening diff, and `force=true` silently **overwrites** the
  intervening edit (`registry/service.py:220-238`). Design: show the diff, git-level
  merge only when edits don't overlap, never silent. Silent overwrite is worse.
- **Re-commit of a committed/abandoned draft**: `commit_draft` never checks
  `draft.status` (`registry/service.py:211-214`); after commit the ref is deleted and
  content reads `""`, so a retried `force` commit lands an **empty template** (which
  validates as `valid`).
- **DB is authoritative for version existence**, not the tree (§5 says the reverse):
  `next_version_number` queries only `models.Version` (`registry/service.py:80-82`).
  Crash between git write and DB commit (not atomic) → number reallocated → a new
  "v3" **silently overwrites** the existing `v3.j2` (the concurrency check passes
  because base and main blobs match).
- **Rollout `{"default": true}` band falls through to lower-priority rules**
  (`core/evaluate.py:84-85`) rather than serving the environment default and
  stopping. The design's ramp example reads the other way; no test pins the choice.
  Needs a deliberate decision.
- **Pointer ordering is wall-clock-first**: `moved_at DESC, id DESC`
  (`targeting/service.py:144,154`; `snapshot.py:43`). Clock skew or a stalled
  transaction can make a later revert sort as non-current. Order by `id` first.
  Related: `make_live` reads `current_live` without locking, so concurrent moves
  record forked `from_sha` chains.
- **`stale_rules` sticks after DB recovery**: the cached snapshot is mutated in place
  (`service.py:90-91`) and keeps reporting stale until a targeting change bumps
  `rules_version`.
- **Env-default changes skip propose→approve** (§7 classes them pointer-class):
  `set_default` (`targeting/service.py:203-224`) has no protected-env branch.
- **Editor role lacks §11's "targeting in unprotected environments"** grant
  (`auth.py:28`; all targeting routes demand `operator`).
- **`GET /prompts`** filters on `viewer`, which `renderer` does not imply — a plain
  service key gets an empty list from a §9 serving endpoint — and omits the promised
  descriptions/variable schemas/tip-vs-live (`serving.py:100-121`).
- **Serving-plane `GET /prompt/{id}/versions` doesn't exist** — mgmt-only, and gone
  entirely in `INCANT_MODE=serve`.
- **Unknown environment → 500** (not §9's 404) on `POST /evaluate` and `GET /prompts`
  (`serving.py:87,108` don't catch `ServingError`; no global handler).
- **Mgmt writes during a DB outage → 500** (not §10's 503): handlers catch only
  domain errors.
- **`serve` mode still serves the UI** (§15: "no mgmt/UI") — only the mgmt router is
  gated (`server/app.py:49-50,66-76`). Boot also does writes (`create_all`,
  bootstrap admin) in serve mode.
- **Boot warm failures are swallowed** (`app.py:37-38`), so `readyz` can go green
  with cold caches (§10: re-warm before ready).
- **Compose/config drift** (§15): no config-file support (`INCANT_CONFIG` YAML), no
  deploy-key/known_hosts mounts, DB password inline rather than
  `POSTGRES_PASSWORD_FILE`.
- P3 assorted: endpoint shapes differ (`:kill` → `?prompt_id=`, PATCH rules is
  status-only, pointers not bulk-capable, flat `POST /mgmt/prompts`); cycle check
  runs against newest versions instead of "current defaults"
  (`registry/service.py:193-198`); compile cache is FIFO not LRU
  (`core/render.py:135-139`); semver ops ignore prerelease semantics; 422s for
  attribute failures name `'dict object'` instead of the variable
  (`render.py:265-270`); rollout band `version` not int-coerced (`parse.py:53`) so a
  string `"2"` silently never matches; content cache keyed by commit not blob;
  tip selection has no id tiebreak on equal `validated_at`; draft commits always
  use `draft@localhost` as author email; CRLF content is silently LF-translated on
  read (`store.py:42-47` uses `text=True`).

---

## 4. UI vs the design comp

**Faithful**: visual system (CSS vars/themes/fonts/animations match the comp), all
nine screens, sidebar structure, live data via real mgmt-API fetches (no mocks),
make-live re-fetch behavior, new-prompt modal.

Gaps and bugs (beyond the hardcoded `force`/`reviewer`/`author` already in §1.6):

- **Segment editor is read-only prose** — comp specifies clause rows (flag / op
  dropdown / values, ✕ remove, + Add clause, + New segment); detail pane hardcodes
  `segments[0]` (`app.js:536-545`). *P1*
- **No rule-creation UI**; ⠿ drag-to-reprioritize handles render but have no drag
  wiring (dead affordance; §12 names it explicitly). *P1*
- **Editor is a plain `<textarea>`** — comp shows line numbers + Jinja coloring;
  §12 says Monaco, lint-as-you-type (lint runs only on save). *P2*
- **Review pane shows raw draft source, not the rendered diff** the comp specifies —
  while printing the "reviewers judge what will be served" tagline (`app.js:448,451`). *P2*
- **Kill switch skips the comp's confirm step** and doesn't dim/disable the rule
  list when engaged (`app.js:461-465,497`). *P2*
- **Rendered test output is plain text** — no variable/fragment highlighting, no
  flags line, no resolved-fragment footnote (`app.js:365`). *P2*
- **Tweak-flow step progression is inert** — the comp's done✓/next-step state is
  never computed; `.tstep.next`/`.tnum.done` CSS exists unused (`app.js:190-194`). *P2*
- **Missing screens/affordances**: overview tabs (Versions / Test contexts /
  Includes / History — the versions API already returns commit history the UI never
  shows), diff compare-against selector, test-context management ("+ context"),
  approval queue, ramp sliders, and §12's whole **Understand** center (experiment
  view, per-prompt timeline, backup health); audit explorer exists but has no
  filtering and hides before/after payloads. *P1–P2*
- **Bugs**: variable required/optional toggle 422s on prompts with no env default
  (`PUT ?version=` empty — exactly where the new-prompt flow lands, `app.js:259`);
  `ensureDraft` falls back to `drafts[0]` and can open the wrong version's draft
  (`app.js:289`); duplicate `style` attribute kills the lint-line color
  (`app.js:339`); "tip = live" shown for prompts never made live (`app.js:222-223`);
  post-save footer drops optional variables (`app.js:663`); dev admin key
  hardcoded in the client (`app.js:5`). *P2–P3*

---

## 5. Test gaps on the §16 "fiddly parts"

- `test_global_bucketing_coherent_across_prompts` is tautological (compares a call
  to an identical call); no end-to-end prompt-scoped rollout through `resolve()`.
- No tests for: the kill switch, `at: "sha"` serving, archived rules, the include
  depth limit, skipped-global-rule → prompt-rule fallthrough, the rollout
  default-band question (§3 above), inference-vs-render agreement (would have caught
  §1.2), pointer `from_sha` chain integrity under concurrency, gitstore conflict/
  force/review-policy paths.

---

## Suggested order of attack

1. **One-liners**: unpin git dates outside tests; CAS on `update-ref`; check
   `expires_at`; env check on rule fetch; reset `stale` on recovery; catch
   `ServingError` in `evaluate_all`/`list_prompts`.
2. **Hot path**: identities + refinement defaults into memory/snapshot; make the
   `rules_version` check outage-tolerant; move `last_used_at` off the request path.
   Restores §8 and §10 together.
3. **Content strictness**: strict `__len__` (or compile-time guard tracking);
   reconcile if-comparison inference; add test-context render to validation.
4. **Governance**: identities from the authenticated principal, never the body;
   approvals list/approve endpoints; restrict or remove `force`; fix UI callers.
5. **Bigger absences** in design order: `pin` replay, backup pusher, revisions/
   rollback, `track_tip`, effective-schema closure.
