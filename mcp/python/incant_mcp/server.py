"""Incant MCP server — curated, task-shaped tools over the Incant HTTP API.

Everything the console can do (minus deployment administration), within the
API key's roles: the server enforces RBAC and this process adds nothing and
hides nothing — a 403 surfaces the server's role explanation verbatim.

Config: INCANT_URL + INCANT_API_KEY (+ optional INCANT_ENVIRONMENT).
`--read-only` registers only the read/test tools.
"""

from __future__ import annotations

import argparse
import os
from typing import Any

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations

from incant_sdk import IncantError

from ._client import Mgmt

# Safety classes for tool annotations. WRITE is for purely additive tools —
# nothing existing can be archived, discarded, deleted, or overwritten by any of
# the tool's documented actions. A combined tool takes the annotation of its most
# destructive action (a host may gate on destructiveHint, so understating one
# action would let it run ungated).
READ = ToolAnnotations(readOnlyHint=True)
WRITE = ToolAnnotations(readOnlyHint=False, destructiveHint=False)
DESTRUCTIVE = ToolAnnotations(readOnlyHint=False, destructiveHint=True)


def create_server(url: str, key: str, *, read_only: bool = False) -> MCPServer:
    api = Mgmt(url, key)
    mcp = MCPServer(
        "incant",
        instructions=(
            "Incant manages versioned prompts: git-backed content, flag-based "
            "targeting, memory-first serving. Core model: a PROMPT has numbered "
            "VERSIONS; edits accumulate on a version's TIP but users only see what "
            "the LIVE POINTER serves — publishing is the explicit act that moves it. "
            "Each ENVIRONMENT has its own targeting (rules/defaults/kills) "
            "and default versions; protected environments demand a `confirm` echo on "
            "mutations — never supply it without the human's explicit go-ahead. "
            "All calls run under the configured API key's roles; a 403 names the "
            "missing role."),
    )

    def _err(exc: Exception) -> ToolError:
        # ToolError survives to the caller verbatim; anything else is masked as
        # a generic "error executing tool" — and the server's messages are the
        # best part of its error UX, so keep them.
        if isinstance(exc, ToolError):
            return exc
        if isinstance(exc, IncantError):
            return ToolError(f"incant: {exc.detail} (HTTP {exc.status})")
        return ToolError(f"incant: {exc}")

    # ── discover & read ──────────────────────────────────────────────

    @mcp.tool(annotations=READ)
    def list_prompts(environment: str | None = None) -> dict:
        """The prompt library with live status per prompt: live version, unpublished
        edits waiting (tip_ahead), open drafts, drafts needing review. Start here."""
        env = api.env(environment)
        try:
            return api.get("/mgmt/overview", environment=env)
        except IncantError as exc:
            if exc.status != 403:
                raise _err(exc)
            # Renderer-scoped keys can't read the console overview but CAN list
            # what they can render — fall back to the serving listing.
            try:
                return api.get("/prompts", environment=env)
            except Exception as exc2:
                raise _err(exc2)

    @mcp.tool(annotations=READ)
    def get_prompt(prompt_id: str, environment: str | None = None) -> dict:
        """One prompt in full: every version (live sha, tip sha, tip_ahead, status,
        commit history), effective variables, includes, open drafts, and saved test
        contexts."""
        env = api.env(environment)
        try:
            out = api.get(f"/mgmt/prompts/{prompt_id}/versions", environment=env)
            out["drafts"] = api.get(f"/mgmt/prompts/{prompt_id}/drafts").get("drafts", [])
            out["test_contexts"] = api.get(
                f"/mgmt/prompts/{prompt_id}/test-contexts").get("test_contexts", [])
            return out
        except Exception as exc:
            raise _err(exc)

    @mcp.tool(annotations=READ)
    def list_rules(environment: str | None = None) -> dict:
        """The environment's whole targeting state: rules (with priority order and
        any unservable warnings), per-prompt default versions, and kill switches.
        Rules are checked top to bottom; first match wins."""
        try:
            return api.get(f"/mgmt/envs/{api.env(environment)}/rules")
        except Exception as exc:
            raise _err(exc)

    @mcp.tool(annotations=READ)
    def get_publish_history(prompt_id: str, version: int,
                            environment: str | None = None) -> dict:
        """Every publish of one prompt version in this environment, newest first —
        who moved the pointer, when, to which sha. The shas here are what
        rollback_pointer accepts."""
        try:
            return api.get(f"/mgmt/envs/{api.env(environment)}/pointers",
                           prompt_id=prompt_id, version=version)
        except Exception as exc:
            raise _err(exc)

    @mcp.tool(annotations=READ)
    def get_targeting_history(environment: str | None = None, limit: int = 50) -> dict:
        """The environment's targeting change log (rules_version timeline): every
        rule/default/kill/pointer/version-status change (plus baseline and rollback
        anchors) with `actor` and `comment`. Feed a
        listed rules_version to rollback_targeting to restore that exact state."""
        try:
            return api.get(f"/mgmt/envs/{api.env(environment)}/revisions", limit=limit)
        except Exception as exc:
            raise _err(exc)

    @mcp.tool(annotations=READ)
    def get_audit(actor: str | None = None, action: str | None = None,
                  object: str | None = None, limit: int = 100) -> dict:
        """The deployment-wide audit log — every mutation with before/after.
        Filter by actor, action (e.g. 'pointer.move', 'rule.upsert'), or object
        substring."""
        try:
            return api.get("/mgmt/audit", actor=actor, action=action,
                           object=object, limit=limit)
        except Exception as exc:
            raise _err(exc)

    @mcp.tool(annotations=READ)
    def list_environments() -> dict:
        """Every environment with its posture: protected (mutations need a confirm
        echo), track_tip (valid saves auto-publish), and which is the serving
        default."""
        try:
            return api.get("/mgmt/envs")
        except Exception as exc:
            raise _err(exc)

    # ── test ─────────────────────────────────────────────────────────

    @mcp.tool(annotations=READ)
    def render_prompt(prompt_id: str, flags: dict | None = None,
                      variables: dict | None = None,
                      environment: str | None = None,
                      pin: dict | None = None) -> dict:
        """Render through the REAL serving path: resolve targeting for `flags`
        (who's asking), render with `variables` (template inputs). Returns the
        text plus exactly what was served (versions map + rules_version — feed
        back as `pin` to replay byte-identically)."""
        body: dict[str, Any] = {"flags": flags or {}, "variables": variables or {},
                                "environment": api.env(environment)}
        if pin:
            body["pin"] = pin
        try:
            return api.post(f"/prompt/{prompt_id}", body)
        except Exception as exc:
            raise _err(exc)

    @mcp.tool(annotations=READ)
    def evaluate_targeting(flags: dict, prompt_id: str | None = None,
                           environment: str | None = None) -> dict:
        """Which version these flags would get — nothing rendered, no variables
        needed. With prompt_id: that prompt's resolution and the matched rule.
        Without: every prompt at once ('what does this experiment change?')."""
        body = {"flags": flags, "environment": api.env(environment)}
        try:
            if prompt_id:
                return api.post(f"/prompt/{prompt_id}/evaluate", body)
            return api.post("/evaluate", body)
        except Exception as exc:
            raise _err(exc)

    @mcp.tool(annotations=READ)
    def diff_versions(prompt_id: str, a_version: int, b_version: int,
                      a_sha: str | None = None, b_sha: str | None = None,
                      mode: str = "source", test_context: str | None = None,
                      environment: str | None = None) -> dict:
        """Compare two versions. Without a_sha/b_sha each side is what the environment
        serves for that version (its live pointer), else its newest validated commit;
        pass shas to compare exact commits (e.g. tip vs live of one version).
        mode='source' diffs the templates; mode='rendered' diffs what a test context
        actually produces — the diff that matters before publishing."""
        try:
            return api.get(f"/mgmt/prompts/{prompt_id}/diff",
                           a_version=a_version, b_version=b_version,
                           a_sha=a_sha, b_sha=b_sha, mode=mode,
                           test_context=test_context,
                           environment=api.env(environment))
        except Exception as exc:
            raise _err(exc)

    if read_only:
        return mcp

    # ── author ───────────────────────────────────────────────────────

    @mcp.tool(annotations=WRITE)
    def create_prompt(prompt_id: str, description: str = "") -> dict:
        """Create a prompt. The id is a path under the deployment's ONE project
        (e.g. 'support/refunds'); nested paths are fine. Fragments are just
        prompts — any prompt can be {% include %}d by another."""
        try:
            return api.post("/mgmt/prompts",
                            {"prompt_id": prompt_id, "description": description})
        except Exception as exc:
            raise _err(exc)

    # DESTRUCTIVE: action='discard' abandons a draft and 'update' replaces its
    # content (unconditionally when base_revision is omitted).
    @mcp.tool(annotations=DESTRUCTIVE)
    def edit_draft(action: str, prompt_id: str | None = None,
                   version_number: int | None = None, draft_id: str | None = None,
                   content: str | None = None, base_revision: str | None = None,
                   title: str = "", seed_from_version: int | None = None,
                   seed_from_sha: str | None = None,
                   against_version: int | None = None, test_context: str | None = None,
                   flags: dict | None = None, variables: dict | None = None,
                   environment: str | None = None) -> dict:
        """The draft lifecycle. action=
        'create' (prompt_id [+ version_number to edit an existing version; omit to
          mint a new one; seed_from_version/seed_from_sha choose the starting
          content — pass the LIVE sha so rolled-back tip edits don't resurrect]),
        'update' (draft_id + content [+ base_revision from the last response to
          chain safely — a 409 means the draft moved; re-read and rebase]),
        'get' (draft_id — content, lint verdict, variables, review state),
        'render' (draft_id + test_context or flags/variables — test the draft
          through the real renderer BEFORE committing),
        'diff' (draft_id [+ against_version, test_context] — what changed, as
          source or rendered output),
        'discard' (draft_id — abandon it)."""
        try:
            if action == "create":
                if not prompt_id:
                    raise ValueError("create needs prompt_id")
                body: dict[str, Any] = {"title": title}
                if version_number is not None:
                    body["version_number"] = version_number
                if seed_from_version is not None:
                    body["seed_from_version"] = seed_from_version
                if seed_from_sha:
                    body["seed_from_sha"] = seed_from_sha
                if content is not None:
                    body["content"] = content
                return api.post(f"/mgmt/prompts/{prompt_id}/drafts", body)
            if not draft_id:
                raise ValueError(f"{action} needs draft_id")
            if action == "update":
                return api.request("PUT", f"/mgmt/drafts/{draft_id}/content",
                                   json={"content": content or "",
                                         "base_revision": base_revision})
            if action == "get":
                return api.get(f"/mgmt/drafts/{draft_id}")
            if action == "render":
                return api.post(f"/mgmt/drafts/{draft_id}/render",
                                {"environment": api.env(environment),
                                 "test_context": test_context,
                                 "flags": flags or {}, "variables": variables or {}})
            if action == "diff":
                return api.get(f"/mgmt/drafts/{draft_id}/diff",
                               against_version=against_version,
                               test_context=test_context,
                               mode="rendered" if test_context else "source",
                               environment=api.env(environment))
            if action == "discard":
                return api.post(f"/mgmt/drafts/{draft_id}/discard")
            raise ValueError(f"unknown action {action!r}")
        except Exception as exc:
            raise _err(exc)

    @mcp.tool(annotations=WRITE)
    def commit_draft(draft_id: str, message: str = "") -> dict:
        """Save a draft's edits as a commit on its version's tip. Honors the
        project's review policy — a 412 means it still needs approvals (use
        review_draft, or ask a colleague). Committing does NOT publish: users
        keep seeing the live pointer until publish_prompt moves it."""
        try:
            return api.post(f"/mgmt/drafts/{draft_id}/commit", {"message": message})
        except Exception as exc:
            raise _err(exc)

    @mcp.tool(annotations=WRITE)
    def review_draft(draft_id: str, state: str = "approved",
                     comment: str | None = None, anchor: str = "") -> dict:
        """Record a review verdict on a draft: state='approved' or
        'changes_requested', with an optional comment (anchor like 'source:4'
        pins it to a line). Verdicts bind to the draft's current content — an
        edit after approval drops it back to needing review."""
        try:
            out = api.post(f"/mgmt/drafts/{draft_id}/review", {"state": state})
            if comment:
                out["comment"] = api.post(f"/mgmt/drafts/{draft_id}/comments",
                                          {"anchor": anchor, "body": comment})
            return out
        except Exception as exc:
            raise _err(exc)

    # DESTRUCTIVE: action='version' can archive a version (status='archived'
    # retires it from serving/drafts) or move its label; 'refine'/'test_context'
    # overwrite an existing refinement/test context of the same name.
    @mcp.tool(annotations=DESTRUCTIVE)
    def set_prompt_metadata(action: str, prompt_id: str, version: int | None = None,
                            notes: str | None = None,
                            status: str | None = None, name: str | None = None,
                            type: str | None = None, required: bool | None = None,
                            default: Any = None, description: str = "",
                            flags: dict | None = None,
                            variables: dict | None = None) -> dict:
        """Prompt/version metadata. action=
        'version' (version + notes/status — status 'archived' retires a version:
          it stops serving, and new drafts/rules for it are refused; 'active'
          brings it back; archiving an environment's DEFAULT version is refused
          with 409 — point the default elsewhere first),
        'refine' (version + name [+ type/required/default/description] — record
          what a template variable means),
        'test_context' (name + flags/variables — save a named who-and-what for
          repeatable render tests)."""
        try:
            if action == "version":
                if version is None:
                    raise ValueError("version action needs version")
                return api.request(
                    "PATCH", f"/mgmt/prompts/{prompt_id}/versions/{version}",
                    json={"notes": notes, "status": status})
            if action == "refine":
                if version is None or not name:
                    raise ValueError("refine needs version and name")
                return api.request(
                    "PUT", f"/mgmt/prompts/{prompt_id}/variables",
                    params={"version": version},
                    json={"name": name, "type": type, "required": required,
                          "default": default, "description": description})
            if action == "test_context":
                if not name:
                    raise ValueError("test_context needs name")
                return api.request(
                    "PUT", f"/mgmt/prompts/{prompt_id}/test-contexts",
                    json={"name": name, "flags": flags or {},
                          "variables": variables or {}})
            raise ValueError(f"unknown action {action!r}")
        except Exception as exc:
            raise _err(exc)

    # ── release & target ─────────────────────────────────────────────

    @mcp.tool(annotations=DESTRUCTIVE)
    def publish_prompt(prompt_id: str, version_number: int, to_sha: str,
                       environment: str | None = None, comment: str = "",
                       make_default: bool = False,
                       archive_rule_ids: list[str] | None = None,
                       confirm: str | None = None) -> dict:
        """Move the live pointer to `to_sha` (a full 40-char commit from
        get_prompt/get_publish_history) — THE act that changes what users see.
        make_default also promotes the version to the environment default (use
        for a first publish, or to end a cohort test so everyone converges).
        archive_rule_ids retires now-redundant test rules atomically. Protected
        environments require confirm=<prompt_id> — relay the confirmation to the
        human and only echo it after their explicit yes."""
        try:
            return api.post(f"/mgmt/envs/{api.env(environment)}/publish",
                            {"prompt_id": prompt_id, "version_number": version_number,
                             "to_sha": to_sha, "comment": comment,
                             "make_default": make_default,
                             "archive_rule_ids": archive_rule_ids or [],
                             "confirm": confirm})
        except Exception as exc:
            raise _err(exc)

    @mcp.tool(annotations=DESTRUCTIVE)
    def rollback_pointer(prompt_id: str, version_number: int, to_sha: str,
                         environment: str | None = None, comment: str = "",
                         confirm: str | None = None) -> dict:
        """Point the live pointer BACK to an earlier sha from
        get_publish_history — the one-click 'go back to this'. History is
        append-only: the rollback is itself a new pointer move. Protected
        environments require confirm=<prompt_id> after explicit human sign-off."""
        try:
            return api.post(f"/mgmt/envs/{api.env(environment)}/pointers",
                            {"prompt_id": prompt_id, "version_number": version_number,
                             "to_sha": to_sha, "comment": comment, "confirm": confirm})
        except Exception as exc:
            raise _err(exc)

    # DESTRUCTIVE: upserting an existing rule id overwrites that rule's live
    # targeting (condition/serve/priority) in place.
    @mcp.tool(annotations=DESTRUCTIVE)
    def upsert_rule(rule: dict, environment: str | None = None) -> dict:
        """Create or update a targeting rule. Shape: {id, prompt_id, priority,
        when: <condition>, serve: {version, at: 'live'|'tip'|'sha' [, sha]},
        status: 'active'|'paused'|'archived', comment}. Conditions:
        {flag, op, value|values} or {all|any: [...]} / {not: ...}. Rules are
        prompt-scoped and serve one version of that prompt; who is in a cohort (a
        beta, a percentage) is a flag the caller's system sets and sends. The
        response may carry a warning that the rule CAN'T SERVE YET (e.g. the
        version was never published here) — surface it and fix the pointer before
        relying on the rule."""
        try:
            return api.post(f"/mgmt/envs/{api.env(environment)}/rules", rule)
        except Exception as exc:
            raise _err(exc)

    # DESTRUCTIVE: 'paused'/'archived' turn a live rule off — its cohort drops to
    # the environment default immediately.
    @mcp.tool(annotations=DESTRUCTIVE)
    def set_rule_status(rule_id: str, status: str,
                        environment: str | None = None) -> dict:
        """Flip a rule's status: 'active', 'paused', or 'archived'. Archiving a
        test rule without publishing its version drops that cohort back to the
        default — usually you want publish_prompt with archive_rule_ids instead."""
        try:
            return api.request(
                "PATCH", f"/mgmt/envs/{api.env(environment)}/rules/{rule_id}",
                json={"status": status})
        except Exception as exc:
            raise _err(exc)

    @mcp.tool(annotations=DESTRUCTIVE)
    def set_default(prompt_id: str, version_number: int,
                    environment: str | None = None,
                    confirm: str | None = None) -> dict:
        """Set which version this environment serves by default (what everyone
        gets when no rule matches). Protected environments require
        confirm=<prompt_id> after explicit human sign-off."""
        try:
            return api.post(f"/mgmt/envs/{api.env(environment)}/defaults",
                            {"prompt_id": prompt_id, "version_number": version_number,
                             "confirm": confirm})
        except Exception as exc:
            raise _err(exc)

    @mcp.tool(annotations=DESTRUCTIVE)
    def kill_switch(prompt_id: str, engaged: bool,
                    environment: str | None = None) -> dict:
        """Targeting on/off for ONE prompt. engaged=true forces every request to
        the environment default, ignoring ALL rules — including versions pinned
        on purpose, for whom the default may be worse, not safer. Reversible
        with engaged=false. Confirm intent with the human before engaging."""
        try:
            return api.post(f"/mgmt/envs/{api.env(environment)}/kill",
                            {"engaged": engaged}, prompt_id=prompt_id)
        except Exception as exc:
            raise _err(exc)

    @mcp.tool(annotations=DESTRUCTIVE)
    def rollback_targeting(to_rules_version: int,
                           environment: str | None = None,
                           confirm: str | None = None) -> dict:
        """Restore the ENTIRE environment's targeting — rules, defaults, kills,
        and live pointers — to an earlier rules_version from
        get_targeting_history. This is environment-wide, not per-prompt: every
        targeting change made after that point stops applying. It is itself a
        new revision (history is never rewritten). Protected environments
        require confirm=<environment name> after explicit human sign-off."""
        try:
            return api.post(f"/mgmt/envs/{api.env(environment)}/rollback",
                            {"to_rules_version": to_rules_version,
                             "confirm": confirm})
        except Exception as exc:
            raise _err(exc)

    return mcp


def main() -> None:
    parser = argparse.ArgumentParser(prog="incant-mcp")
    parser.add_argument("--url", default=os.environ.get("INCANT_URL", ""))
    parser.add_argument("--api-key", default=os.environ.get("INCANT_API_KEY", ""))
    parser.add_argument("--read-only", action="store_true",
                        help="register only the read/test tools")
    args = parser.parse_args()
    if not args.url or not args.api_key:
        raise SystemExit("incant-mcp: set INCANT_URL and INCANT_API_KEY "
                         "(or pass --url / --api-key)")
    create_server(args.url, args.api_key, read_only=args.read_only).run()


if __name__ == "__main__":
    main()
