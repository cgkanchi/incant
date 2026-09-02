"""incant-mcp against a REAL server: every tool call goes over actual HTTP to a
seeded uvicorn subprocess, exactly as an agent's MCP session would. Covers the
full authoring loop (create → draft → review → commit), the release loop
(publish + make_default → serve), targeting with save-time warnings, kill/
rollback recovery, read-only mode, and the renderer-key discovery fallback."""

from __future__ import annotations

import asyncio
import json

import pytest

from incant_mcp.server import create_server

from .conftest import live_incant_server

ADMIN_KEY = "incant_sk_dev_admin"


@pytest.fixture(scope="module")
def server(tmp_path_factory):
    with live_incant_server(tmp_path_factory.mktemp("incant-mcp"), "mcp_test") as url:
        yield url


@pytest.fixture(scope="module")
def mcp(server):
    return create_server(server, ADMIN_KEY)


def call(mcp, tool: str, **args):
    """Drive a tool exactly as the protocol layer does; return the parsed JSON."""
    result = asyncio.run(mcp.call_tool(tool, args))
    assert not result.is_error, result
    return json.loads(result.content[0].text)


def call_err(mcp, tool: str, **args) -> str:
    with pytest.raises(Exception) as exc:
        asyncio.run(mcp.call_tool(tool, args))
    return str(exc.value)


# ── discovery ────────────────────────────────────────────────────────

def test_discovery_tools(mcp):
    lib = call(mcp, "list_prompts", environment="prod")
    ids = {p["prompt_id"] for proj in lib["projects"] for p in proj["prompts"]}
    assert "support/system" in ids

    p = call(mcp, "get_prompt", prompt_id="support/system", environment="prod")
    assert {v["version"] for v in p["versions"]} >= {1, 2, 3}
    assert any(d["status"] == "open" for d in p["drafts"])       # Sam's seeded draft
    assert any(t["name"] == "enterprise-us" for t in p["test_contexts"])

    rules = call(mcp, "list_rules", environment="prod")
    assert "beta-gets-v3" in {r["id"] for r in rules["rules"]}
    # diff without shas: each side defaults to what prod serves (live) else the tip.
    d = call(mcp, "diff_versions", prompt_id="support/system", a_version=2, b_version=3,
             environment="prod")
    assert d["mode"] == "source" and d["diff"] and d["left"] != d["right"]

    envs = call(mcp, "list_environments")
    assert {e["id"]: e["protected"] for e in envs["environments"]}["prod"] is True


# ── the full authoring loop, then release ────────────────────────────

def test_author_review_commit_publish_serve(mcp):
    call(mcp, "create_prompt", prompt_id="support/mcp-flow",
         description="authored over MCP")
    d = call(mcp, "edit_draft", action="create", prompt_id="support/mcp-flow",
             version_number=1, title="v1")
    d2 = call(mcp, "edit_draft", action="update", draft_id=d["id"],
              content="Hello {{ who }} from MCP", base_revision=d["draft_sha"])
    assert d2["lint"]["status"] == "valid"
    assert "who" in d2["variables"]["names"]

    # Render-test the draft through the real renderer before committing.
    r = call(mcp, "edit_draft", action="render", draft_id=d["id"],
             variables={"who": "world"}, environment="prod")
    assert "Hello world from MCP" in json.dumps(r)

    # The seeded project needs one approval: the 412 surfaces, review unblocks.
    msg = call_err(mcp, "commit_draft", draft_id=d["id"])
    assert "approval" in msg
    call(mcp, "review_draft", draft_id=d["id"], state="approved",
         comment="LGTM — verified against a render")
    out = call(mcp, "commit_draft", draft_id=d["id"], message="v1 via MCP")
    assert out["validation"]["status"] == "valid"

    # First publish: pointer + default in one atomic act (prod is protected —
    # the confirm echo is required, as the skill instructs).
    call(mcp, "publish_prompt", prompt_id="support/mcp-flow", version_number=1,
         to_sha=out["full_sha"], environment="prod", make_default=True,
         confirm="support/mcp-flow")
    served = call(mcp, "render_prompt", prompt_id="support/mcp-flow",
                  variables={"who": "users"}, environment="prod")
    assert "Hello users from MCP" in served["prompt"]

    hist = call(mcp, "get_publish_history", prompt_id="support/mcp-flow",
                version=1, environment="prod")
    assert hist["moves"][0]["full_sha"] == out["full_sha"]


# ── targeting: warnings, evaluation, recovery ────────────────────────

def test_targeting_warning_evaluate_kill_and_rollback(mcp):
    before = call(mcp, "get_targeting_history", environment="prod")["revisions"][0][
        "rules_version"]

    # An unservable serve target warns at save time (greeting v2 never published).
    out = call(mcp, "upsert_rule", environment="prod",
               rule={"id": "mcp-unservable", "scope": "prompt",
                     "prompt_id": "support/greeting", "priority": 80,
                     "when": {"flag": "beta_opt_in", "op": "eq", "value": True},
                     "serve": {"version": 2}, "comment": "warn me"})
    assert "can't serve yet" in out.get("warning", "")

    ev = call(mcp, "evaluate_targeting", environment="prod",
              flags={"beta_opt_in": True, "region": "us", "tier": "pro"},
              prompt_id="support/system")
    assert ev["version"] == 3 and ev["matched_rule"]["id"] == "beta-gets-v3"

    # Kill: everyone falls to the default (v2), rules ignored; then restore.
    call(mcp, "kill_switch", prompt_id="support/system", engaged=True,
         environment="prod")
    ev2 = call(mcp, "evaluate_targeting", environment="prod",
               flags={"beta_opt_in": True, "region": "us", "tier": "pro"},
               prompt_id="support/system")
    assert ev2["version"] == 2 and ev2["matched_rule"] == "default"
    call(mcp, "kill_switch", prompt_id="support/system", engaged=False,
         environment="prod")

    # Whole-environment rollback erases the experiment rule (prod is protected:
    # confirm echoes the ENV name here).
    call(mcp, "rollback_targeting", to_rules_version=before,
         environment="prod", confirm="prod")
    rules = call(mcp, "list_rules", environment="prod")
    by_id = {r["id"]: r for r in rules["rules"]}
    assert "mcp-unservable" not in by_id or by_id["mcp-unservable"]["status"] != "active"


# ── permission surfacing + modes ─────────────────────────────────────

def test_errors_carry_server_messages(mcp):
    msg = call_err(mcp, "publish_prompt", prompt_id="support/system",
                   version_number=2, to_sha="0" * 40, environment="prod")
    assert "confirm" in msg.lower()   # the protected-env ceremony, verbatim


def test_read_only_mode_has_no_write_tools(server):
    ro = create_server(server, ADMIN_KEY, read_only=True)
    names = {t.name for t in asyncio.run(ro.list_tools())}
    assert "render_prompt" in names and "list_prompts" in names
    assert "publish_prompt" not in names and "upsert_rule" not in names


def test_stdio_transport_end_to_end(server):
    """The real thing an agent does: spawn `incant-mcp` as a subprocess, speak
    MCP over stdio, list tools, call one over actual HTTP."""
    import sys

    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    async def go():
        params = StdioServerParameters(
            command=sys.executable, args=["-m", "incant_mcp.server"],
            env={"INCANT_URL": server, "INCANT_API_KEY": ADMIN_KEY,
                 "PATH": __import__("os").environ.get("PATH", "")})
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                tools = await session.list_tools()
                names = {t.name for t in tools.tools}
                result = await session.call_tool(
                    "evaluate_targeting",
                    {"flags": {"user_id": "u_12"}, "prompt_id": "support/system",
                     "environment": "prod"})
                return names, json.loads(result.content[0].text)

    names, ev = asyncio.run(go())
    assert {"list_prompts", "publish_prompt", "kill_switch"} <= names
    assert ev["version"] == 2 and ev["matched_rule"]["id"] == "team-x-tip"


# ── safety annotations (Finding F11) ─────────────────────────────────────────

def test_tool_safety_annotations_do_not_understate_destructive_actions():
    """A host may gate on destructiveHint, so a combined tool must carry its most
    destructive action's class: archive/discard/delete/overwrite ⇒ destructive;
    only purely additive writes stay non-destructive. No live server needed —
    annotations are declared at build time."""
    m = create_server("http://127.0.0.1:9", "unused-key")  # no HTTP at build time
    tools = {t.name: t for t in asyncio.run(m.list_tools())}

    read_only = {"list_prompts", "get_prompt", "list_rules", "get_publish_history",
                 "get_targeting_history", "get_audit", "list_environments",
                 "render_prompt", "evaluate_targeting", "diff_versions"}
    additive = {"create_prompt", "commit_draft", "review_draft"}
    destructive = {
        "edit_draft",           # action='discard'; 'update' can replace content
        "set_prompt_metadata",  # 'version' can archive; refine/test_context overwrite
        "upsert_rule",          # overwrites an existing rule's live targeting
        "set_rule_status",      # 'paused'/'archived' drop a cohort to the default
        "publish_prompt", "rollback_pointer", "set_default", "kill_switch",
        "rollback_targeting",
    }
    assert set(tools) == read_only | additive | destructive  # nothing unclassified

    for name in sorted(read_only):
        assert tools[name].annotations.read_only_hint is True, name
    for name in sorted(additive):
        a = tools[name].annotations
        assert a.read_only_hint is False and a.destructive_hint is False, name
    for name in sorted(destructive):
        a = tools[name].annotations
        assert a.read_only_hint is False and a.destructive_hint is True, name


def test_renderer_key_falls_back_to_serving_listing(server):
    import urllib.request
    req = urllib.request.Request(
        server + "/mgmt/keys", method="POST",
        data=json.dumps({"principal_name": "mcp-render", "role": "renderer",
                         "environment_id": "prod"}).encode(),
        headers={"Authorization": f"Bearer {ADMIN_KEY}",
                 "Content-Type": "application/json"})
    key = json.load(urllib.request.urlopen(req))["key"]
    rmcp = create_server(server, key)
    lib = call(rmcp, "list_prompts", environment="prod")
    assert "prompts" in lib   # the serving-shape fallback
    assert any(p["prompt_id"] == "support/system" for p in lib["prompts"])
