"""§7 pointer integrity: `make_live` and the snapshot's servability check must
enforce the full (prompt, version, SHA) tuple, not just "this SHA is validated for
*some* prompt".

The bug: `make_live` used to guard with `is_validated(to_sha)`, which only asked
"does any valid CommitValidation row carry this SHA?". A releaser could therefore
aim (prompt A, v2)'s live pointer at a commit validated only for prompt B, or for a
different version of A, or at a version that never existed. Rule pins already did
this correctly via `_version_exists` + `_is_validated_for`; these tests pin the same
guarantee onto the pointer, plus the read-side backstop in `build_snapshot`.
"""

from __future__ import annotations

import pytest

from incant import models
from incant.db import session_scope
from incant.service import ServingError
from incant.targeting import build_snapshot
from incant.targeting.service import TargetingError

# Reuse the end-to-end `app` fixture and author helper rather than re-deriving the
# whole authoring→validation→commit dance here.
from .test_integration import _author_version, app  # noqa: F401 (app used as fixture)


def test_make_live_rejects_sha_validated_for_another_prompt(app):
    # Two independent prompts → two distinct validated commits.
    a = _author_version(app, "support/alpha", 1, "alpha v1", make_live=False)
    b = _author_version(app, "support/beta", 1, "beta v1", make_live=False)
    assert a.sha != b.sha
    with session_scope() as s:
        with pytest.raises(TargetingError):
            # beta's SHA is validated, but not for support/alpha v1.
            app.targeting(s, "rel").make_live("prod", "support/alpha", 1, b.sha)
    # Pointer never moved.
    with session_scope() as s:
        assert app.targeting(s, "rel").current_live("prod", "support/alpha", 1) is None


def test_make_live_rejects_sha_validated_for_another_version(app):
    # Same prompt, two versions, two distinct validated commits.
    v1 = _author_version(app, "support/system", 1, "v1 body", make_live=False)
    v2 = _author_version(app, "support/system", 2, "v2 body", make_live=False)
    assert v1.sha != v2.sha
    with session_scope() as s:
        with pytest.raises(TargetingError):
            # v2's SHA is validated for v2, but the pointer targets v1.
            app.targeting(s, "rel").make_live("prod", "support/system", 1, v2.sha)


def test_make_live_rejects_nonexistent_version(app):
    v1 = _author_version(app, "support/system", 1, "v1 body", make_live=False)
    with session_scope() as s:
        with pytest.raises(TargetingError):
            # v7 was never authored, even though the SHA is a real validated commit.
            app.targeting(s, "rel").make_live("prod", "support/system", 7, v1.sha)


def test_make_live_accepts_correct_tuple_and_moves_pointer(app):
    v1 = _author_version(app, "support/system", 1, "v1 body", make_live=False)
    with session_scope() as s:
        assert app.targeting(s, "rel").current_live("prod", "support/system", 1) is None
        outcome = app.targeting(s, "rel").make_live(
            "prod", "support/system", 1, v1.sha, comment="go live")
        assert outcome.status == "live" and outcome.move_id is not None
    with session_scope() as s:
        assert app.targeting(s, "rel").current_live("prod", "support/system", 1) == v1.sha


def test_snapshot_servable_is_prompt_aware(app):
    # Two prompts, each with its own validated SHA (no live pointers needed — the
    # CommitValidation rows are what `servable` keys off).
    a = _author_version(app, "support/alpha", 1, "alpha v1", make_live=False)
    b = _author_version(app, "support/beta", 1, "beta v1", make_live=False)
    with session_scope() as s:
        snap = build_snapshot(s, "prod")
    # beta's SHA is servable for beta, but NOT for alpha (cross-prompt is rejected).
    assert snap.servable("support/beta", 1, b.sha) is True
    assert snap.servable("support/alpha", 1, b.sha) is False
    # ...and symmetrically for alpha's SHA.
    assert snap.servable("support/alpha", 1, a.sha) is True
    assert snap.servable("support/beta", 1, a.sha) is False


def test_snapshot_servable_is_version_aware_for_same_prompt(app):
    v1 = _author_version(app, "support/system", 1, "v1", make_live=False)
    v2 = _author_version(app, "support/system", 2, "v2", make_live=False)
    with session_scope() as s:
        snap = build_snapshot(s, "prod")
    assert snap.servable("support/system", 1, v1.sha) is True
    assert snap.servable("support/system", 2, v2.sha) is True
    assert snap.servable("support/system", 1, v2.sha) is False
    assert snap.servable("support/system", 2, v1.sha) is False


def test_corrupt_pointer_to_wrong_version_fails_closed(app):
    v1 = _author_version(app, "support/system", 1, "v1", make_live=False)
    v2 = _author_version(app, "support/system", 2, "v2", make_live=False)
    assert v1.sha != v2.sha
    # Simulate corruption/import bypassing TargetingService's write guard.
    with session_scope() as s:
        s.add(models.PointerMove(
            environment_id="prod", prompt_id="support/system", version_number=1,
            to_sha=v2.sha, moved_by="corrupt-import",
        ))
    app.invalidate("prod")
    with session_scope() as s:
        with pytest.raises(ServingError) as exc:
            app.serve(s, "prod", "support/system", {}, {})
    assert exc.value.status == 409
