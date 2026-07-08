"""ContentStore — the git-backed ContentProvider for the render hot path.

Blobs are extracted from git into a content-addressed in-memory cache when a SHA
becomes referenceable (validation time / eager warm). The cache is immutable and
LRU-bounded — a blob's bytes never change, so entries are never invalidated.
"""

from __future__ import annotations

from collections import OrderedDict

from ..core import ContentBlob
from .store import GitStore


class ContentStore:
    def __init__(self, git: GitStore, cache_max: int = 4096) -> None:
        self.git = git
        self._cache: OrderedDict[str, ContentBlob] = OrderedDict()
        self._cache_max = cache_max
        self.misses = 0

    @staticmethod
    def _path(prompt_id: str, version: int) -> str:
        return f"{prompt_id}/v{version}.j2"

    def get(self, prompt_id: str, version: int, commit_sha: str) -> ContentBlob:
        path = self._path(prompt_id, version)
        # Cache key is the (commit, path) pair; the blob within is content-addressed.
        key = f"{commit_sha}:{path}"
        hit = self._cache.get(key)
        if hit is not None:
            self._cache.move_to_end(key)
            return hit
        self.misses += 1
        source = self.git.read(path, ref=commit_sha)
        if source is None:
            raise KeyError(f"{path} not present at {commit_sha}")
        blob_sha = self.git.blob_sha(path, ref=commit_sha) or ""
        blob = ContentBlob(blob_sha=blob_sha, source=source)
        self._cache[key] = blob
        self._cache.move_to_end(key)
        if len(self._cache) > self._cache_max:
            self._cache.popitem(last=False)
        return blob

    def warm(self, prompt_id: str, version: int, commit_sha: str) -> ContentBlob:
        return self.get(prompt_id, version, commit_sha)
