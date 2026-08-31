"""The synchronous client. See AsyncIncant (aio.py) for the async mirror —
both are thin transports over _common, so wire behavior is identical."""

from __future__ import annotations

import time
from typing import Any

import httpx

from . import _common as c
from ._errors import IncantUnavailable
from ._models import PromptInfo, PromptSpec, RenderResult, Resolution


class Incant:
    """Client for one Incant deployment.

    >>> client = Incant()                     # INCANT_URL / INCANT_API_KEY / INCANT_ENVIRONMENT
    >>> r = client.render("support/system",
    ...                   flags={"user_id": "u_42", "tier": "pro"},
    ...                   variables={"customer_name": "Acme", "history": []})
    >>> r.text, r.version, r.pin

    Renders are pure reads: connection failures and 502/503/504 are retried
    (``retries`` times, backing off) before raising IncantUnavailable.
    """

    def __init__(self, url: str | None = None, *, key: str | None = None,
                 environment: str | None = None, timeout: float = 5.0,
                 retries: int = 2, transport: httpx.BaseTransport | None = None):
        base_url, api_key = c.resolve_config(url, key)
        self.environment = c.default_environment(environment)
        self._retries = max(0, retries)
        self._http = httpx.Client(base_url=base_url, headers=c.headers(api_key),
                                  timeout=timeout, transport=transport)

    # ── rendering ────────────────────────────────────────────────────

    def render(self, prompt_id: str, *, flags: dict | None = None,
               variables: dict | None = None, environment: str | None = None,
               pin: dict | None = None) -> RenderResult:
        """Resolve through targeting for ``flags``, render with ``variables``.
        Pass a prior result's ``.pin`` to replay that render exactly."""
        path, body = c.build_render(prompt_id, flags, variables,
                                    environment or self.environment, pin)
        return c.parse_render(self._request("POST", path, json=body))

    # ── resolution (no render) ───────────────────────────────────────

    def evaluate(self, prompt_id: str, *, flags: dict | None = None,
                 environment: str | None = None) -> Resolution:
        """Which version (at which commit) these flags would get — no variables
        needed, nothing rendered. The debugging half of targeting."""
        path, body = c.build_evaluate(prompt_id, flags,
                                      environment or self.environment)
        return c.parse_resolution(self._request("POST", path, json=body))

    def evaluate_all(self, *, flags: dict | None = None,
                     environment: str | None = None) -> dict[str, Resolution]:
        """Every prompt's resolution for one user — 'what does this experiment
        change?' in a single call."""
        path, body = c.build_evaluate(None, flags, environment or self.environment)
        data = self._request("POST", path, json=body)
        return {pid: c.parse_resolution(res, pid)
                for pid, res in data.get("resolutions", {}).items()}

    # ── discovery ────────────────────────────────────────────────────

    def prompts(self, *, environment: str | None = None) -> list[PromptInfo]:
        """Every prompt this credential can render in the environment."""
        params = _env_params(environment or self.environment)
        return c.parse_prompts(self._request("GET", "/prompts", params=params))

    def prompt(self, prompt_id: str, *, environment: str | None = None) -> PromptSpec:
        """The prompt's spec: variables to pass (merged across every version
        targeting can serve) and the flags its rules consult."""
        params = _env_params(environment or self.environment)
        return c.parse_spec(self._request("GET", f"/prompt/{prompt_id}/spec",
                                          params=params))

    # ── plumbing ─────────────────────────────────────────────────────

    def _request(self, method: str, path: str, **kwargs: Any) -> dict:
        last: Exception | None = None
        for attempt in range(self._retries + 1):
            if attempt:
                time.sleep(c.BACKOFFS[min(attempt - 1, len(c.BACKOFFS) - 1)])
            try:
                resp = self._http.request(method, path, **kwargs)
            except httpx.TransportError as exc:
                last = exc
                continue
            if resp.status_code in c.RETRYABLE_STATUSES:
                last = c.response_error(resp)
                continue
            if resp.status_code >= 400:
                raise c.response_error(resp)
            return resp.json()
        if isinstance(last, IncantUnavailable):
            raise last
        raise IncantUnavailable(f"could not reach Incant after "
                                f"{self._retries + 1} attempts: {last}")

    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> "Incant":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()


def _env_params(environment: str | None) -> dict:
    return {"environment": environment} if environment else {}
