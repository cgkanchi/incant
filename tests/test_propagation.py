"""Content changes reach every node; the full node stays warm through its own writes.

``rules_version`` only moves on targeting changes, so a serve replica polling it alone
never learned about a new validated SHA (the tip and the servable index), a new version
row or a variable default — it served a stale tip forever and 409'd a pin naming a SHA
the full node accepted. ``content_version`` is the second freshness key that closes
that gap. On the full node the old "clear the whole cache after every commit" made
the next request a cold DB build (a 503 on a Postgres blip); it now rebuilds in place.

Also here: one refresh pass loads the environment-independent validated index once for
every snapshot it rebuilds, and a ``rules_version`` replay refuses to serve a fragment
that is killed NOW even though the recorded state had no kill.
"""

from __future__ import annotations

import pytest
from sqlalchemy import select, text

from incant import models
from incant.db import session_scope
from incant.service import AppContext, ServingError, get_app

from .test_hotpath import BoomSession
from .test_integration import _author_version
from .test_integration import app as _integration_app
from .test_server import auth, make_client

# The library-level AppContext fixture, re-bound under the name pytest looks up. A direct
# `import app` would be shadowed by every test's `app` parameter (pyflakes F811).
app = _integration_app

PID = "support/system"


def _content_versions() -> dict[str, int]:
    with session_scope() as s:
        return dict(s.execute(
            select(models.Environment.id, models.Environment.content_version)).all())


# ── Fix 2: replicas learn about content changes through content_version ────────

def test_replica_learns_new_tip_servable_sha_and_default_without_a_rules_bump(app):
    _author_version(app, PID, 1, "v1a {{ x }}")        # default + live in prod (no track_tip)
    replica = AppContext()                             # a second node on the same DB + repo
    with session_scope() as s:
        before = replica.get_snapshot(s, "prod")

    # The writer publishes a new SHA on v1 and sets a variable default. Neither is a
    # targeting change: prod's rules_version must NOT move.
    with session_scope() as s:
        reg = app.registry(s, "sam")
        d = reg.create_draft(PID, version_number=1, author="sam", content="v1b {{ x }}")
        out = reg.commit_draft(d.id, author="sam")
        reg.set_refinement(PID, 1, "x", type="string", required=False, default="dflt")
    assert out.validation["status"] == "valid"
    assert before.version_info(PID, 1).tip_sha != out.sha
    assert not before.servable(PID, 1, out.sha)

    with session_scope() as s:
        assert replica.get_snapshot(s, "prod") is before   # nothing moves until the poll
        replica.refresh_control_plane(s)
        after = replica.get_snapshot(s, "prod")
    assert after is not before
    assert after.rules_version == before.rules_version     # content moved, targeting did not
    assert after.version_info(PID, 1).tip_sha == out.sha
    assert after.servable(PID, 1, out.sha)
    assert after.refinement_defaults[(PID, 1)] == {"x": "dflt"}
    # The consequences the replica used to get wrong: a pin naming the fresh SHA is
    # accepted (it was 409 "not a validated commit"), and the default fills in from memory.
    resp = replica.serve(BoomSession(), "prod", PID, {}, {}, pin={PID: (1, out.sha)})
    assert resp["prompt"] == "v1b dflt"


def test_content_version_advances_on_every_content_input_and_never_on_targeting(app):
    with session_scope() as s:
        s.add(models.Environment(id="staging", name="staging"))
    base = _content_versions()
    assert set(base) == {"prod", "staging"}

    def expect(delta):
        # Content is global to the deployment: every environment's row moves together.
        assert _content_versions() == {e: v + delta for e, v in base.items()}

    with session_scope() as s:
        reg = app.registry(s, "sam")
        reg.create_prompt(PID)
        d = reg.create_draft(PID, version_number=1, author="sam", content="v1")
        reg.commit_draft(d.id, author="sam")                      # new version row + new SHA
    expect(1)
    with session_scope() as s:
        reg = app.registry(s, "sam")
        d = reg.create_draft(PID, version_number=1, author="sam", content="v1 again")
        reg.commit_draft(d.id, author="sam")                      # new SHA on an existing version
    expect(2)
    with session_scope() as s:
        app.registry(s, "sam").set_refinement(PID, 1, "x", type="string",
                                              required=False, default=1)
    expect(3)
    with session_scope() as s:
        app.targeting(s, "sam").set_default("prod", PID, 1)       # targeting: rules_version only
    expect(3)


@pytest.fixture()
def client(tmp_path):
    with make_client(tmp_path) as c:
        yield c


def test_full_node_stays_warm_through_a_commit_and_sees_the_new_tip_at_once(client):
    ctx = get_app()
    with session_scope() as s:
        prod_before = ctx.get_snapshot(s, "prod")
        staging_before = ctx.get_snapshot(s, "staging")

    d = client.post(f"/mgmt/prompts/{PID}/drafts",
                    json={"version_number": 2, "content": "tweak {{ customer_name }}"},
                    headers=auth()).json()
    client.post(f"/mgmt/drafts/{d['id']}/review", json={}, headers=auth())
    r = client.post(f"/mgmt/drafts/{d['id']}/commit", json={}, headers=auth())
    assert r.status_code == 200, r.text
    sha = r.json()["full_sha"]

    # No poll has run. Both entries are still there — rebuilt in place, never dropped —
    # so the next render is a memory hit (the BoomSession proves zero DB reads)…
    prod_after = ctx.get_snapshot(BoomSession(), "prod")
    staging_after = ctx.get_snapshot(BoomSession(), "staging")
    assert prod_after is not prod_before and staging_after is not staging_before
    # …and they already carry the new tip: same-node "commit then render sees it".
    assert prod_after.version_info(PID, 2).tip_sha == sha
    assert prod_after.servable(PID, 2, sha)
    assert prod_after.rules_version == prod_before.rules_version  # prod: no track_tip


# ── Fix 9: one validated index per refresh pass ──────────────────────────────

def _index_of(snap) -> set:
    """The validated set a snapshot's ``servable`` closes over (the object under test)."""
    return next(c.cell_contents for c in snap.servable.__closure__
                if isinstance(c.cell_contents, set))


def test_refresh_pass_loads_the_validated_index_once_and_shares_it(app, monkeypatch):
    import incant.service as service_mod
    import incant.targeting.snapshot as snapshot_mod

    with session_scope() as s:
        s.add(models.Environment(id="staging", name="staging"))
    _author_version(app, PID, 1, "v1")
    _author_version(app, PID, 1, "v1 staging", env="staging")
    with session_scope() as s:
        app.get_snapshot(s, "prod")
        app.get_snapshot(s, "staging")

    calls: list[int] = []
    real = snapshot_mod.load_validated_index

    def counting(session):
        calls.append(1)
        return real(session)

    # Both names: build_snapshot's own fallback and the pass-level loader in service.
    monkeypatch.setattr(snapshot_mod, "load_validated_index", counting)
    monkeypatch.setattr(service_mod, "load_validated_index", counting)

    with session_scope() as s:
        app.refresh_control_plane(s)
    assert calls == []                      # nothing to rebuild → no scan at all

    with session_scope() as s:              # both environments changed "elsewhere"
        s.execute(text("UPDATE environments SET rules_version = rules_version + 1"))
    with session_scope() as s:
        app.refresh_control_plane(s)
    assert len(calls) == 1                  # one load for two rebuilds…
    prod, staging = app._snapshots["prod"].snapshot, app._snapshots["staging"].snapshot
    assert _index_of(prod) is _index_of(staging)   # …and one shared set object


# ── Fix 5: kill beats replay for included fragments too ───────────────────────

def test_rules_version_replay_refuses_a_fragment_killed_now(app):
    _author_version(app, "support/frag", 1, "FRAG")
    _author_version(app, "support/parent", 1, 'P {% include "support/frag" %}')
    with session_scope() as s:
        first = app.serve(s, "prod", "support/parent", {}, {})
    assert first["prompt"] == "P FRAG"
    rv = first["rules_version"]

    # Kill the FRAGMENT now (not the root, not a pinned prompt). The recorded state at
    # `rv` has no kill, so the fragment resolves normally inside the historical snapshot.
    with session_scope() as s:
        app.targeting(s, "op").set_kill("prod", "support/frag", True)
    app.invalidate("prod")
    with session_scope() as s:
        with pytest.raises(ServingError) as ei:
            app.serve(s, "prod", "support/parent", {}, {}, pin_rules_version=rv)
    assert ei.value.status == 409 and ei.value.extra == {"error": "killed"}
    assert "support/frag" in ei.value.detail
    # The unpinned path still serves (a kill degrades the fragment to its default).
    with session_scope() as s:
        assert app.serve(s, "prod", "support/parent", {}, {})["prompt"] == "P FRAG"

    # Lift the kill: the same historical replay renders again.
    with session_scope() as s:
        app.targeting(s, "op").set_kill("prod", "support/frag", False)
    app.invalidate("prod")
    with session_scope() as s:
        replay = app.serve(s, "prod", "support/parent", {}, {}, pin_rules_version=rv)
    assert replay["rules_version"] == rv and replay["prompt"] == "P FRAG"
