"""incant-sdk — Python client for Incant.

>>> from incant_sdk import Incant
>>> client = Incant()          # INCANT_URL, INCANT_API_KEY, INCANT_ENVIRONMENT
>>> r = client.render("support/system",
...                   flags={"user_id": "u_42", "tier": "pro"},
...                   variables={"customer_name": "Acme", "history": []})
>>> print(r.text)              # the rendered prompt
>>> r.pin                      # log beside the LLM call; pass back to replay exactly

Discovery: ``client.prompts()`` lists what this key can render;
``client.prompt(id)`` returns the spec — variables to pass and the targeting
flags the rules consult. ``client.evaluate(...)`` resolves without rendering.
Async: ``from incant_sdk import AsyncIncant`` (same surface, awaited).
"""

from ._common import VERSION as __version__  # noqa: N811
from ._errors import (
    IncantError,
    IncantUnavailable,
    MissingVariable,
    NotAuthorized,
    PromptNotFound,
    RenderError,
)
from ._models import (
    Flag,
    PromptInfo,
    PromptSpec,
    RenderResult,
    Resolution,
    RuleMatch,
    SkippedRule,
    Var,
    VersionPin,
)
from .aio import AsyncIncant
from .client import Incant

__all__ = [
    "Incant", "AsyncIncant",
    "RenderResult", "Resolution", "PromptInfo", "PromptSpec",
    "Var", "Flag", "RuleMatch", "VersionPin", "SkippedRule",
    "IncantError", "NotAuthorized", "PromptNotFound", "MissingVariable",
    "RenderError", "IncantUnavailable",
    "__version__",
]
