"""Shared test helpers: a dict-backed ContentProvider and snapshot builders."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass

from incant.core import ContentBlob, EnvSnapshot, VersionInfo


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
