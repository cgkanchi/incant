"""Adoption logic for a non-Alembic Postgres schema (`ensure_schema`).

The dangerous case this guards is a populated Postgres database that has no
``alembic_version`` row: a dev/test DB built by ``create_all``, or a genuinely old
pre-Alembic install. The historical bug stamped every such DB at ``head``, which was
right only for the create_all-from-current-models case and permanently skipped the
migrations an older install still needed. ``_adoption_revision`` fixes that by probing
the live schema for the marker each migration adds; these tests pin that decision
table with a stub inspector (no real database required) and, when a Postgres URL is
available, exercise the full stamp-then-upgrade path end to end.
"""

from __future__ import annotations

import os

import pytest

from incant.db import _adoption_revision, _has_unique_columns


# --- Stub inspector ---------------------------------------------------------------
#
# `_adoption_revision` / `_has_unique_columns` depend only on the three SQLAlchemy
# Inspector methods below, so a plain stub lets us drive every schema state without a
# database. `unique_constraints` and `indexes` map table name -> list of reflection
# dicts, mirroring what SQLAlchemy's real inspector returns for each.


class FakeInspector:
    def __init__(self, tables, unique_constraints=None, indexes=None):
        self._tables = list(tables)
        self._unique_constraints = unique_constraints or {}
        self._indexes = indexes or {}

    def get_table_names(self):
        return list(self._tables)

    def get_unique_constraints(self, table):
        return list(self._unique_constraints.get(table, []))

    def get_indexes(self, table):
        return list(self._indexes.get(table, []))


# The set of tables a baseline-or-later schema always carries (abbreviated — only the
# ones the detection logic names need to be real; extra unrelated tables don't matter).
_CORE_TABLES = ["principals", "api_keys", "reviews", "drafts"]


def _uc(column_names, name="whatever"):
    """A unique-constraint reflection dict (name is deliberately arbitrary — detection
    keys on the column set, not the object name)."""
    return {"name": name, "column_names": list(column_names)}


def _prefix_unique_via_constraint():
    return {"api_keys": [_uc(["prefix"], name="uq_apikey_prefix")]}


def _review_unique_via_constraint():
    return {"reviews": [_uc(["draft_id", "reviewer"], name="uq_review")]}


# --- _adoption_revision: the four schema states ------------------------------------


def test_sessions_missing_adopts_at_baseline():
    """No ``sessions`` table → 67fb7465ee07 hasn't run; adopt at the baseline so the
    upgrade replays sessions + both later uniqueness migrations."""
    insp = FakeInspector(tables=_CORE_TABLES)  # note: no "sessions"
    assert _adoption_revision(insp) == "da3e34b2b8fe"


def test_sessions_present_but_prefix_not_unique_adopts_at_67fb():
    """``sessions`` exists but api_keys.prefix is still the OLD non-unique index →
    a3f1c8e29b41 hasn't run; adopt at 67fb7465ee07."""
    insp = FakeInspector(
        tables=_CORE_TABLES + ["sessions"],
        # The pre-a3f1 schema had a plain, NON-unique index on prefix — must not count.
        indexes={"api_keys": [{"name": "ix_api_keys_prefix",
                               "column_names": ["prefix"], "unique": False}]},
    )
    assert _adoption_revision(insp) == "67fb7465ee07"


def test_prefix_unique_but_review_not_unique_adopts_at_a3f1():
    """prefix uniqueness present, reviews(draft_id, reviewer) not yet unique →
    b7d2e6f4a1c9 hasn't run; adopt at a3f1c8e29b41."""
    insp = FakeInspector(
        tables=_CORE_TABLES + ["sessions"],
        unique_constraints=_prefix_unique_via_constraint(),
    )
    assert _adoption_revision(insp) == "a3f1c8e29b41"


def test_everything_present_returns_head():
    """A schema matching the current models (e.g. create_all) → stamp head, upgrade
    no-ops. This is the only state the old blanket stamp-head handled correctly."""
    ucs = {**_prefix_unique_via_constraint(), **_review_unique_via_constraint()}
    insp = FakeInspector(tables=_CORE_TABLES + ["sessions"], unique_constraints=ucs)
    assert _adoption_revision(insp) == "head"


# --- constraint-vs-index duality ---------------------------------------------------


def test_prefix_uniqueness_detected_via_unique_index():
    """Postgres may surface uniqueness as a unique INDEX rather than a constraint
    (create_all, or a legacy hand-built schema). Detection must accept it, so a DB
    whose prefix uniqueness is index-shaped still advances past 67fb."""
    insp = FakeInspector(
        tables=_CORE_TABLES + ["sessions"],
        indexes={"api_keys": [{"name": "some_unique_ix",
                               "column_names": ["prefix"], "unique": True}]},
    )
    # prefix uniqueness satisfied, review uniqueness absent → next revision is a3f1.
    assert _adoption_revision(insp) == "a3f1c8e29b41"


def test_review_uniqueness_detected_via_unique_index():
    ucs = _prefix_unique_via_constraint()
    insp = FakeInspector(
        tables=_CORE_TABLES + ["sessions"],
        unique_constraints=ucs,
        indexes={"reviews": [{"name": "some_unique_ix",
                              "column_names": ["draft_id", "reviewer"], "unique": True}]},
    )
    assert _adoption_revision(insp) == "head"


# --- _has_unique_columns direct coverage -------------------------------------------


def test_has_unique_columns_ignores_wrong_column_set_and_non_unique():
    insp = FakeInspector(
        tables=["api_keys"],
        unique_constraints={"api_keys": [_uc(["id"])]},         # wrong columns
        indexes={"api_keys": [{"name": "ix", "column_names": ["prefix"],
                               "unique": False}]},               # right cols, not unique
    )
    assert _has_unique_columns(insp, "api_keys", ["prefix"]) is False
    assert _has_unique_columns(insp, "api_keys", ["id"]) is True


# --- Integration: real Postgres round-trip (skipped without a DB) -------------------
#
# Only runs when INCANT_TEST_DATABASE_URL points at a reachable Postgres. Almost never
# set in a plain unit run, so this skips cleanly there. It upgrades a scratch schema to
# the *middle* of the chain (67fb7465ee07), drops alembic_version to simulate a
# non-Alembic install stuck at that state, then proves ensure_schema adopts + migrates
# it to head with the later uniqueness objects materialised.

_PG_URL = os.environ.get("INCANT_TEST_DATABASE_URL")


@pytest.mark.skipif(not _PG_URL, reason="INCANT_TEST_DATABASE_URL not set (no Postgres)")
def test_ensure_schema_adopts_and_upgrades_partial_postgres_schema():
    from sqlalchemy import create_engine, inspect, text
    from sqlalchemy.engine import make_url

    import incant.db as db
    from incant.config import Settings, get_settings, set_settings

    # Isolate onto a dedicated '<db>_scratch' database so we never touch anything real.
    base = make_url(_PG_URL)
    scratch_name = (base.database or "incant") + "_ensure_schema_scratch"
    admin = base.set(database="postgres")
    admin_eng = create_engine(admin, isolation_level="AUTOCOMMIT", future=True)
    try:
        with admin_eng.connect() as c:
            c.execute(text(f'DROP DATABASE IF EXISTS "{scratch_name}" WITH (FORCE)'))
            c.execute(text(f'CREATE DATABASE "{scratch_name}"'))
    finally:
        admin_eng.dispose()

    scratch_url = base.set(database=scratch_name).render_as_string(hide_password=False)

    saved = get_settings()
    try:
        set_settings(Settings(database_url=scratch_url))
        db.reset_engine()

        # Build the schema only up to the middle of the chain, then erase the Alembic
        # bookmark so it looks like a non-Alembic install frozen at 67fb7465ee07.
        from alembic import command
        command.upgrade(db._alembic_config(), "67fb7465ee07")
        with db.engine().begin() as conn:
            conn.exec_driver_sql("DROP TABLE IF EXISTS alembic_version")

        insp = inspect(db.engine())
        assert "sessions" in insp.get_table_names()
        assert not _has_unique_columns(insp, "api_keys", ["prefix"])  # a3f1 not yet run

        db.ensure_schema()

        # Landed at head, with the two post-67fb uniqueness objects now present.
        db.reset_engine()
        insp = inspect(db.engine())
        with db.engine().connect() as conn:
            version = conn.execute(text("SELECT version_num FROM alembic_version")).scalar()
        assert version == "b7d2e6f4a1c9"
        assert _has_unique_columns(insp, "api_keys", ["prefix"])
        assert _has_unique_columns(insp, "reviews", ["draft_id", "reviewer"])
    finally:
        set_settings(saved)
        db.reset_engine()
        admin_eng = create_engine(admin, isolation_level="AUTOCOMMIT", future=True)
        try:
            with admin_eng.connect() as c:
                c.execute(text(f'DROP DATABASE IF EXISTS "{scratch_name}" WITH (FORCE)'))
        finally:
            admin_eng.dispose()
