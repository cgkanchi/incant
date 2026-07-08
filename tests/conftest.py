"""Shared test helpers: a dict-backed ContentProvider and snapshot builders."""

from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass

from incant.core import ContentBlob, EnvSnapshot, VersionInfo

# DB-touching tests run against whatever INCANT_TEST_DATABASE_URL points at (a real
# Postgres in CI/Docker), falling back to a throwaway SQLite file only for quick
# local unit runs. Serving always uses Postgres.
TEST_DATABASE_URL = os.environ.get("INCANT_TEST_DATABASE_URL")


def db_url_for(tmp_path) -> str:
    return TEST_DATABASE_URL or f"sqlite:///{tmp_path/'incant.db'}"


def reset_schema() -> None:
    """Drop + recreate all tables so a shared Postgres is isolated per test."""
    from incant import models  # noqa: F401 — register tables
    from incant.db import Base, engine

    Base.metadata.drop_all(engine())
    Base.metadata.create_all(engine())


def blob_sha(source: str) -> str:
    return "b" + hashlib.sha256(source.encode()).hexdigest()[:12]


@dataclass
class DictContent:
    """Maps (prompt_id, commit_sha) -> source. Commit SHAs are arbitrary labels."""

    sources: dict[tuple[str, str], str]

    def get(self, prompt_id: str, version: int, commit_sha: str) -> ContentBlob:
        source = self.sources[(prompt_id, commit_sha)]
        return ContentBlob(blob_sha=blob_sha(source), source=source)


def vinfo(version, live=None, tip=None, label=None, previous=(), status="active"):
    return VersionInfo(
        version=version, live_sha=live, tip_sha=tip, label=label,
        status=status, previous_live=tuple(previous),
    )


def snapshot(environment="prod", rules_version=1, **kw) -> EnvSnapshot:
    return EnvSnapshot(environment=environment, rules_version=rules_version, **kw)
