"""The prompt-id grammar (incant.core.ids) at every door it guards, and the git-side
hygiene it pairs with.

Before the grammar existed, three concrete things went wrong: an id git refuses to
write (``acme/foo/``, ``acme/../x``) created the prompt row and then 500'd on the first
draft write (an orphan row, a permanent bare error); an empty id bound the deployment
to project ``""``; and ``list_files`` C-quoted any path with a quote/newline/non-ASCII
byte so a DR adoption silently dropped the prompt."""

from __future__ import annotations

import pytest
from sqlalchemy import func, select

from incant import models
from incant.core.ids import (
    PROMPT_ID_MAX,
    is_valid_prompt_id,
    validate_project_id,
    validate_prompt_id,
)
from incant.db import session_scope
from incant.registry import RegistryError

from .test_gitstore import make_store
from .test_server import auth, make_client


@pytest.fixture()
def client(tmp_path):
    with make_client(tmp_path) as c:
        yield c


# Every id the repo itself relies on — seed.py, the test suites, the SDK/MCP suites,
# skills and docs. The grammar must accept all of them, or a green suite lies.
_IDS_IN_USE = [
    "support/system", "support/style/language-rules", "support/growth/welcome",
    "support/growth/csrf-a", "support/growth/bearer-nocsrf", "support/mcp-flow",
    "support/scale/window", "support/escalation/triage", "support/refunds",
    "acme/system", "acme/refunds", "pm-review/system", "ghost/thing", "shared/leaf",
    "p/a", "a/b", "alpha/x", "qa_1-b/x", "acme/v1.2",
]

_REFUSED = [
    "",                     # bound the deployment to project ""
    "acme",                 # single segment: no project/name split
    "acme/",                # trailing slash — git refuses, used to orphan a row
    "/acme/x",              # leading slash
    "acme//x",              # double slash
    "acme/../x",            # git refuses `..`
    "acme/.",               # git refuses `.`
    "acme/.git",            # git refuses `.git`
    "Acme/x", "acme/Foo",   # uppercase: case-only twins in URL/git/PK
    "acme/x y",             # whitespace
    "acme/-x",              # option look-alike / leading separator
    "acme/x.",              # trailing separator in a segment
    "acme/x\n",             # control byte
    "a" * PROMPT_ID_MAX + "/x",  # too long
]


@pytest.mark.parametrize("pid", _IDS_IN_USE)
def test_grammar_accepts_every_id_the_repo_uses(pid):
    assert is_valid_prompt_id(pid)
    assert validate_prompt_id(pid) == pid


@pytest.mark.parametrize("pid", _REFUSED)
def test_grammar_refuses(pid):
    assert not is_valid_prompt_id(pid)
    with pytest.raises(ValueError, match="invalid prompt id"):
        validate_prompt_id(pid)


def _prompt_count() -> int:
    with session_scope() as s:
        return s.execute(select(func.count()).select_from(models.Prompt)).scalar()


@pytest.mark.parametrize("pid", ["", "support/foo/", "support/../x", "support/Upper",
                                 "support", "support//x"])
def test_create_prompt_422s_with_the_grammar_and_writes_no_row(client, pid):
    before = _prompt_count()
    r = client.post("/mgmt/prompts", json={"prompt_id": pid, "description": ""},
                    headers=auth())
    assert r.status_code == 422, r.text
    # The grammar itself is in the message, so the author knows what to type.
    assert "two or more segments" in r.text
    assert _prompt_count() == before  # no orphan row: nothing to 500 on later


def test_empty_id_no_longer_binds_the_deployment_to_a_blank_project(tmp_path):
    # Before any project is bound, `""` used to pass `_project_of` and `ensure_project`
    # and permanently claim project "" for the deployment. Boot WITHOUT seeding so no
    # project exists yet, then try.
    from incant import db
    from incant.config import Settings, set_settings
    from incant.service import reset_app
    from .conftest import db_url_for, reset_schema
    from .test_server import ADMIN
    from fastapi.testclient import TestClient

    set_settings(Settings(database_url=db_url_for(tmp_path), repo_path=str(tmp_path / "repo"),
                          bootstrap_admin_key=ADMIN))
    db.reset_engine(); reset_app(); reset_schema()
    from incant.server.app import create_app
    with TestClient(create_app()) as c:
        r = c.post("/mgmt/prompts", json={"prompt_id": ""}, headers=auth())
        assert r.status_code == 422
        with session_scope() as s:
            assert s.execute(select(models.Project)).scalars().all() == []


def test_valid_id_still_creates_and_writes_a_draft(client):
    # The positive path through the same door, including the first draft write that the
    # bad ids used to explode on.
    r = client.post("/mgmt/prompts", json={"prompt_id": "support/style/tone.v2"},
                    headers=auth())
    assert r.status_code == 200, r.text
    d = client.post("/mgmt/prompts/support/style/tone.v2/drafts",
                    json={"version_number": 1, "content": "hi {{ x }}"}, headers=auth())
    assert d.status_code == 200, d.text


def test_registry_refuses_bad_ids_for_seed_and_cli_paths(tmp_path):
    # The service layer is the second door (seed, CLI, tests) — same grammar, RegistryError.
    with make_client(tmp_path):
        from incant.service import get_app
        with session_scope() as s:
            reg = get_app().registry(s, "sam")
            with pytest.raises(RegistryError, match="invalid prompt id"):
                reg.create_prompt("Support/x")
            with pytest.raises(RegistryError, match="invalid prompt id"):
                reg.create_prompt("support/x/")
            assert reg.prompt_exists("support/system")  # untouched


def test_list_files_returns_raw_paths_for_unusual_bytes(tmp_path):
    # GitStore sits below the grammar (a restored legacy repo may hold anything git
    # accepts), so it must report every path faithfully. Without `-z`, git C-quotes a
    # path holding a quote or non-ASCII byte — the entry no longer ends in `.j2` and
    # adopt_content_tree silently drops it.
    g = make_store(tmp_path)
    for pid in ['support/we"ird', "support/ünïcode", "support/plain"]:
        g.commit_version(pid, 1, "x", author_name="A", author_email="a@x", message="c")
    assert g.list_files() == sorted(
        ['support/we"ird/v1.j2', "support/ünïcode/v1.j2", "support/plain/v1.j2"])


# ── project ids: one segment of the same grammar ─────────────────────


@pytest.mark.parametrize("pid", ["support", "acme", "a1", "x" * 64, "a.b_c-d"])
def test_project_grammar_accepts(pid):
    assert validate_project_id(pid) == pid


@pytest.mark.parametrize("pid", ["", "Support", "a/b", "-x", "x-", "x" * 65, "a b", ".."])
def test_project_grammar_refuses(pid):
    with pytest.raises(ValueError):
        validate_project_id(pid)


def test_projects_endpoint_refuses_a_wedging_id(client):
    from .test_server import auth

    # "Support" would make every valid (lowercase) prompt id conflict with the
    # one-project rule forever — the wedge the grammar exists to prevent.
    r = client.post("/mgmt/projects", json={"id": "Support"}, headers=auth())
    assert r.status_code == 422 and "project id" in r.text, r.text

