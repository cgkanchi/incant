---
name: incant-release
description: "Release and target prompts in an Incant deployment: publish committed edits (move the live pointer), set environment defaults, run cohort tests with flag-based targeting rules, stop tests by promoting the winner, roll back pointers or whole targeting states, and use kill switches. Operates on committed content only — authoring is incant-authoring's job. Keywords: publish, release, live pointer, make live, default version, targeting rule, flag, cohort, stop test, promote, rollback, kill switch, protected environment."
license: MIT
compatibility: Requires the Incant MCP server (incant-mcp) with an API key holding operator/releaser on the target environment. Operates on committed content; does not edit templates.
metadata:
  author: incant
  version: "1.1.0"
---

# Release, Target, and Roll Back

You're changing **what users actually see**. In Incant nothing reaches users
until the **live pointer** moves — and each **environment** has its own
pointers, default versions, rules, and kill switches. Every action
here is live-traffic-affecting; the workflow is therefore plan → confirm →
act → verify, with hard confirmation gates.

| Step | Owned by |
|------|----------|
| Draft → test → commit | [`incant-authoring`](../incant-authoring/SKILL.md) |
| **Publish, target, roll out, roll back** | **this skill** |

**MCP tools this skill uses:**
- `list_environments`, `get_prompt`, `list_rules`, `get_publish_history`,
  `get_targeting_history`, `get_audit` — establish current state
- `evaluate_targeting`, `render_prompt`, `diff_versions` — prove who gets what,
  before and after
- `publish_prompt` — pointer move (+ `make_default`, + `archive_rule_ids`), atomic
- `set_default`, `upsert_rule`, `set_rule_status` — targeting
- `rollback_pointer`, `rollback_targeting`, `kill_switch` — recovery

## Plan Phase

**Change nothing in this phase.**

1. **Name the environment explicitly — never default to prod.** If the user
   didn't name one, ask. `list_environments` tells you which are `protected`
   (confirm ceremony), which `track_tip`, and which is the serving default.
2. **Establish what's true now.** `get_prompt` for the version's live sha vs
   tip (`tip_ahead` = unpublished edits); `list_rules` for the rules, defaults,
   and kills in play; `get_publish_history` for what was live before (your
   rollback target). For a cohort, `evaluate_targeting` with representative
   flags to show current resolution.
3. **Show the change as its rendered diff.** `diff_versions` in rendered mode
   against a real test context — "what people get now" vs "after". Source
   diffs justify a commit; rendered diffs justify a release.
4. **Present the plan and stop.** Environment, prompt, exact sha to publish,
   who's affected (everyone? a cohort? N%?), which rules are added/archived,
   whether the default moves, and the rollback path ("revert to `<sha>` via
   rollback_pointer"). **Wait for explicit confirmation.** For a protected
   environment, say so: the action will require echoing the prompt id (or env
   name) as `confirm` — relay that ceremony to the human; **never fabricate the
   confirm token from your own judgment.** Fail closed: unclear scope = don't.

## Implement Phase

Pick the pattern that matches the confirmed intent:

- **Ship committed edits to everyone (same version):** `publish_prompt` with
  the tip sha from `get_prompt`. If `@tip` test rules exist for
  this prompt, pass their ids in `archive_rule_ids` — published content makes
  them redundant, and the atomic form can't strand a moved pointer with live
  test rules.
- **First publish of a new prompt:** `publish_prompt` with `make_default=true`
  — a pointer alone serves nobody when the prompt has no default version.
- **Test with a cohort first:** `upsert_rule` serving the candidate
  (`{version: N, at: "tip"}` while iterating, or `@live` after a pointer
  exists) to a flag condition. Rules are prompt-scoped and read the flags the
  caller sends: WHO is in the cohort — a beta list, a percentage — is decided
  by the caller's flag system and arrives as a flag (e.g. `beta_voice: true`);
  Incant does not bucket users itself. **Read the response's warning field:**
  "can't serve yet" means the target has no publishable content here and the
  cohort will silently get the default — fix the pointer first. Then
  `evaluate_targeting` both ways: cohort flags resolve to the candidate,
  non-cohort flags to the default. Don't declare the test running until both
  are proven.
- **Stop a test and ship the winner:** one `publish_prompt` call with the
  winning version's sha, `make_default=true`, and the test rule's id in
  `archive_rule_ids`. **`make_default` is the promotion** — archiving the rule
  alone drops the cohort back to the OLD default, the opposite of shipping.
- **Roll back one prompt:** `rollback_pointer` to a sha from
  `get_publish_history`. History is append-only — the rollback is itself a
  recorded move.
- **Roll back targeting wholesale:** `rollback_targeting` to a
  `rules_version` from `get_targeting_history`.
- **Emergency stop:** `kill_switch` engaged=true.

After any action, **verify through the serving path**: `render_prompt` /
`evaluate_targeting` with the flags that matter, and confirm the response's
versions map shows what you intended. Report what changed, who sees it, and
the exact rollback command.

> **Three blast-radius truths you must not soften:**
> 1. **`rollback_targeting` restores the whole environment** — every prompt's
>    rules, defaults, kills, and pointers, not just the one you're fixing.
>    Changes made after the target revision stop applying. Per-prompt fixes
>    use `rollback_pointer`.
> 2. **A kill switch overrides deliberate pins.** Users kept on an older
>    version on purpose fall to the default too — for them the "safe" default
>    may be worse. Confirm intent; prefer archiving the offending rule when
>    the problem is one rule.
> 3. **The confirm token is a human ceremony, not a parameter to satisfy.**
>    Protected environments ask for it precisely so releases pause at a
>    human. Supply it only after relaying what will happen and receiving an
>    explicit yes.
