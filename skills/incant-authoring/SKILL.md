---
name: incant-authoring
description: "Author and evolve prompts in an Incant deployment: create prompts, draft edits or new versions, test-render against real contexts, record variable metadata, and take drafts through review to a committed version. Ends at the commit — publishing what users see is incant-release's job. Keywords: prompt authoring, draft, edit prompt, new version, Jinja template, test render, variables, review, approve, commit, fragment, include."
license: MIT
compatibility: Requires the Incant MCP server (incant-mcp) configured with an API key holding at least editor on the target project. Never publishes — pointer moves belong to incant-release.
metadata:
  author: incant
  version: "1.0.0"
---

# Author a Prompt Change

You're editing content in Incant, where **a commit is not a release**. Edits
accumulate on a version's **tip**; users keep seeing whatever the **live
pointer** serves until someone explicitly publishes. That split is the whole
point — it's what makes editing safe. This skill takes a change from intent to
a committed, review-clean version and **stops there**:

| Step | Owned by |
|------|----------|
| Draft → test → review → **commit** | **this skill** |
| Publish, target a cohort, roll out | [`incant-release`](../incant-release/SKILL.md) |

**MCP tools this skill uses:**
- `list_prompts`, `get_prompt` — discover what exists and its current state
- `create_prompt` — new prompt (or fragment — fragments are just prompts)
- `edit_draft` — create / update / get / **render** / diff / discard a draft
- `set_prompt_metadata` — labels, variable refinements, saved test contexts
- `review_draft` — approve / request changes, with comments
- `commit_draft` — the deliverable
- `diff_versions`, `render_prompt` — verify the committed result

## Plan Phase

**Write nothing in this phase.**

1. **Discover before assuming.** `list_prompts` for the library; `get_prompt`
   for the target's versions, variables, includes, open drafts, and test
   contexts. If another author has an open draft on the same version, say so
   and ask before creating a parallel one — two drafts on one version is a
   merge waiting to happen.
2. **Pick the right container for the change.** Editing the current version's
   meaning-preserving wording → a draft **on that version**. A change in what
   the prompt *does* (different task, different variables) → a **new version**
   (`edit_draft` action=create with no version_number). When seeding a new
   version, pass `seed_from_sha` = the current **live** sha from `get_prompt` —
   seeding from the tip can silently resurrect edits someone deliberately
   rolled back.
3. **Know the review gate.** The project's review policy (approvals required,
   self-review allowed) travels with every draft payload — `edit_draft`
   action=create/get returns `review_policy` and `allow_self_review` — not with
   `get_prompt`. Read it the moment the draft exists and plan the approval step
   then; don't discover the 412 at commit time and improvise.
4. **State the plan and stop.** Prompt, version (existing or new), the edit in
   one sentence, which test contexts will prove it, and whether review is
   needed. Wait for confirmation.

## Implement Phase

1. **Draft.** `edit_draft` action=create, then action=update with the content.
   Chain each update's `base_revision` from the previous response — a 409 means
   the draft moved underneath you (another autosave, another session): re-read
   with action=get and rebase onto the current content; never blind-overwrite.
2. **Lint is not optional.** Every update returns the lint verdict and the live
   variable set. A `template error` is an ordinary mid-edit state — but never
   present a draft as done while lint is failing, and never commit one.
3. **Render-test before review — through the real renderer.** `edit_draft`
   action=render against each saved test context (and any context the change
   specifically targets). Read the output. A template that compiles but says
   the wrong thing passes every lint check. Use action=diff (rendered mode)
   to show what actually changes for each context — that diff, not the source
   diff, is what a reviewer should judge.
4. **Record what you learned.** New variable? `set_prompt_metadata`
   action=refine with type/required/description — the next author and every
   SDK user inherits it. A scenario worth keeping? action=test_context.
5. **Review, honestly.** If policy requires approvals, request them and wait —
   the approval must come from a principal other than the draft's author unless
   the project allows self-review; **do not approve your own work to unblock
   yourself** even if your key technically can. Verdicts bind to content: any
   edit after an approval voids it, so land review last.
6. **Commit** with a message that says *why*. Then verify: `get_prompt` shows
   the new tip, and `render_prompt` (which serves the LIVE pointer) still shows
   the old content — that's correct, not a bug. Report exactly that: committed
   at `<sha>`, live unchanged, publishing is a separate decision
   (`incant-release`).

> **The one thing you must get right: track-tip environments auto-publish.**
> In an environment marked `track_tip` (check `list_environments`), a VALID
> commit publishes itself — there the commit IS the release. Before committing,
> know which environments track tip and say so in the plan. If the change isn't
> ready for those users, it isn't ready to commit.
