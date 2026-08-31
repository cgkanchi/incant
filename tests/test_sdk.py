"""incant-sdk against a REAL server — a uvicorn subprocess over a dedicated
Postgres database with the seeded example dataset. No mocked HTTP anywhere:
these tests exercise the true wire format, auth, and error shapes the SDK's
users will hit, including the renderer-scoped discovery the SDK exists for."""

from __future__ import annotations

import asyncio
import json
import urllib.request

import pytest

from incant_sdk import (
    AsyncIncant,
    Incant,
    IncantUnavailable,
    MissingVariable,
    NotAuthorized,
    PromptNotFound,
    RuleMatch,
)

from .conftest import live_incant_server

ADMIN_KEY = "incant_sk_dev_admin"


@pytest.fixture(scope="module")
def server(tmp_path_factory):
    """Seeded real server on a free port (module scope — tests are read-mostly)."""
    with live_incant_server(tmp_path_factory.mktemp("incant-sdk"), "sdk_test") as url:
        yield url


@pytest.fixture(scope="module")
def client(server):
    with Incant(server, key=ADMIN_KEY, environment="prod") as c:
        yield c


@pytest.fixture(scope="module")
def renderer_client(server):
    """A key with ONLY the renderer role, scoped to prod — the production
    credential the SDK's discovery story is designed around."""
    req = urllib.request.Request(
        server + "/mgmt/keys", method="POST",
        data=json.dumps({"principal_name": "sdk-app", "role": "renderer",
                         "environment_id": "prod"}).encode(),
        headers={"Authorization": f"Bearer {ADMIN_KEY}",
                 "Content-Type": "application/json"})
    key = json.load(urllib.request.urlopen(req))["key"]
    with Incant(server, key=key, environment="prod") as c:
        yield c


# ── discovery ────────────────────────────────────────────────────────

def test_prompts_lists_the_library(client):
    ids = {p.id for p in client.prompts()}
    assert {"support/system", "support/greeting",
            "support/style/language-rules"} <= ids
    system = next(p for p in client.prompts() if p.id == "support/system")
    assert system.default_version == 2 and set(system.versions) >= {1, 2, 3}
    assert system.labels.get(3) == "voice-v2"


def test_renderer_key_can_discover(renderer_client):
    # The point of the /prompts relaxation: a pure renderer key sees its library.
    ids = {p.id for p in renderer_client.prompts()}
    assert "support/system" in ids
    spec = renderer_client.prompt("support/system")
    assert spec.default_version == 2


def test_spec_names_variables_and_flags(client):
    spec = client.prompt("support/system")
    assert set(spec.resolvable_versions) == {2, 3}
    v = {x.name: x for x in spec.variables}
    assert v["customer_name"].required
    assert "company or name" in v["customer_name"].description  # refinement metadata
    assert not v["history"].required
    flags = {f.name: set(f.values) for f in spec.flags}
    assert {"enterprise", "pro"} <= flags["tier"]          # from the beta rule
    assert {"us", "us-gov"} <= flags["region"]             # via the beta-us segment
    assert True in flags["beta_opt_in"]
    assert "u_12" in flags["user_id"]                      # from team-x-tip
    assert "support/style/language-rules" in spec.includes


# ── rendering ────────────────────────────────────────────────────────

def test_render_default_and_targeted(renderer_client):
    r = renderer_client.render("support/system",
                               variables={"customer_name": "Acme", "history": []})
    assert "support agent for Acme" in r.text and str(r) == r.text
    assert r.version == 2 and r.matched_rule == "default"
    assert len(r.sha) == 40

    beta = renderer_client.render(
        "support/system",
        flags={"beta_opt_in": True, "region": "us", "tier": "enterprise"},
        variables={"customer_name": "Acme", "history": []})
    assert beta.version == 3
    assert isinstance(beta.matched_rule, RuleMatch)
    assert beta.matched_rule.id == "beta-gets-v3"
    # v3 includes the style fragment — the versions map carries it for the pin.
    assert "support/style/language-rules" in beta.versions


def test_pin_replays_exactly(renderer_client):
    args = dict(flags={"beta_opt_in": True, "region": "us", "tier": "pro"},
                variables={"customer_name": "Lumen", "history": []})
    first = renderer_client.render("support/system", **args)
    replay = renderer_client.render("support/system", **args, pin=first.pin)
    assert replay.text == first.text
    assert replay.versions == first.versions


def test_evaluate_and_evaluate_all(renderer_client):
    res = renderer_client.evaluate(
        "support/system",
        flags={"beta_opt_in": True, "region": "us", "tier": "pro"})
    assert res.version == 3 and isinstance(res.matched_rule, RuleMatch)
    everything = renderer_client.evaluate_all(flags={"user_id": "u_12"})
    assert everything["support/system"].version == 2   # team-x-tip serves v2@tip
    assert "support/greeting" in everything


# ── errors ───────────────────────────────────────────────────────────

def test_missing_variable_is_typed(renderer_client):
    with pytest.raises(MissingVariable) as exc:
        renderer_client.render("support/system", variables={"history": []})
    assert exc.value.variable == "customer_name"
    assert exc.value.status == 422


def test_unknown_prompt_and_bad_key(server, renderer_client):
    with pytest.raises(PromptNotFound):
        renderer_client.render("support/nope", variables={})
    with Incant(server, key="incant_sk_" + "0" * 32) as bad:
        with pytest.raises(NotAuthorized):
            bad.render("support/system", variables={})


def test_unreachable_server_is_unavailable():
    with Incant("http://127.0.0.1:9", key="incant_sk_x",
                retries=0, timeout=0.5) as c:
        with pytest.raises(IncantUnavailable):
            c.prompts()


# ── async mirror ─────────────────────────────────────────────────────

def test_async_client_mirror(server):
    async def go():
        async with AsyncIncant(server, key=ADMIN_KEY, environment="prod") as c:
            r = await c.render("support/system",
                               variables={"customer_name": "Async", "history": []})
            spec = await c.prompt("support/system")
            return r, spec
    r, spec = asyncio.run(go())
    assert "support agent for Async" in r.text and r.version == 2
    assert any(v.name == "customer_name" for v in spec.variables)
