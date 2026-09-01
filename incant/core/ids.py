"""The prompt-id grammar — defined ONCE, applied at every door a prompt id enters.

A prompt id is a path: ``project/name`` or deeper (``project/style/tone``). It is at
once a URL path segment, a git tree path (``<prompt_id>/vN.j2``), and a database
primary key, so the grammar is the intersection of what all three accept cleanly:

* **two or more segments** — the first names the deployment's ONE project
  (``_project_of`` splits on the first ``/``); a single segment would make the prompt
  its own project and an empty id would bind the deployment to project ``""``;
* **single ``/`` separators, no leading/trailing slash** — git's ``verify_path``
  refuses ``//``, a leading ``/`` and a trailing ``/``; caught here they are a 422,
  caught in git they were an orphan DB row plus a bare 500 on the first draft write;
* **each segment starts and ends with a letter or digit** — this excludes the
  components git refuses outright (``.``, ``..``, ``.git``), option look-alikes
  (``-foo``) and dotfiles, while keeping ``.``/``_``/``-`` available inside a name;
* **lowercase** — ids are compared byte-for-byte in three places (URL, git path on a
  case-sensitive filesystem, PK), so admitting case would admit case-only twins;
* **bounded length** — a PK and a URL segment, not a document.

``validate_prompt_id`` is the single source of truth: the mgmt schema
(``CreatePromptRequest``) and ``RegistryService.create_prompt`` (seed/CLI paths) both
call it, so an id no code path can write is refused before any row exists.
"""

from __future__ import annotations

import re

PROMPT_ID_MAX = 200
_SEGMENT_RE = re.compile(r"^[a-z0-9]([a-z0-9._-]*[a-z0-9])?$")

PROMPT_ID_GRAMMAR = (
    "a prompt id is a path of two or more segments separated by single '/' "
    "(e.g. 'acme/refunds' or 'acme/style/tone'); each segment is lowercase letters, "
    "digits, '.', '_' or '-', starting and ending with a letter or digit; "
    f"no leading, trailing or double slashes; at most {PROMPT_ID_MAX} characters"
)


def is_valid_prompt_id(prompt_id: str) -> bool:
    if not isinstance(prompt_id, str) or not prompt_id or len(prompt_id) > PROMPT_ID_MAX:
        return False
    segments = prompt_id.split("/")
    # A leading/trailing/double slash yields an empty segment, which the segment
    # pattern rejects — so one rule covers all three. fullmatch, not match: `$` would
    # also accept a segment with a trailing newline.
    return len(segments) >= 2 and all(_SEGMENT_RE.fullmatch(seg) for seg in segments)


def validate_prompt_id(prompt_id: str) -> str:
    """Return ``prompt_id`` unchanged if it satisfies the grammar; raise ``ValueError``
    carrying the grammar otherwise (callers surface it as 422 / RegistryError)."""
    if not is_valid_prompt_id(prompt_id):
        raise ValueError(f"invalid prompt id {prompt_id!r}: {PROMPT_ID_GRAMMAR}")
    return prompt_id
