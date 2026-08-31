"""The async client — a line-for-line mirror of client.Incant over
httpx.AsyncClient. All request building, parsing, and error mapping is shared
(_common), so the two can never disagree about the wire."""

from __future__ import annotations

import asyncio
from typing import Any

import httpx

from . import _common as c
from ._errors import IncantUnavailable
from ._models import PromptInfo, PromptSpec, RenderResult, Resolution
from .client import _env_params


class AsyncIncant:
    """Async client for one Incant deployment.

    >>> async with AsyncIncant() as client:
    ...     r = await client.render("support/system", flags=..., variables=...)
    """

    def __init__(self, url: str | None = None, *, key: str | None = None,
                 environment: str | None = None, timeout: float = 5.0,
                 retries: int = 2,
                 transport: httpx.AsyncBaseTransport | None = None):
        base_url, api_key = c.resolve_config(url, key)
        self.environment = c.default_environment(environment)
        self._retries = max(0, retries)
        self._http = httpx.AsyncClient(base_url=base_url, headers=c.headers(api_key),
                                       timeout=timeout, transport=transport)

    async def render(self, prompt_id: str, *, flags: dict | None = None,
                     variables: dict | None = None, environment: str | None = None,
                     pin: dict | None = None) -> RenderResult:
        path, body = c.build_render(prompt_id, flags, variables,
                                    environment or self.environment, pin)
        return c.parse_render(await self._request("POST", path, json=body))

    async def evaluate(self, prompt_id: str, *, flags: dict | None = None,
                       environment: str | None = None) -> Resolution:
        path, body = c.build_evaluate(prompt_id, flags,
                                      environment or self.environment)
        return c.parse_resolution(await self._request("POST", path, json=body))

    async def evaluate_all(self, *, flags: dict | None = None,
                           environment: str | None = None) -> dict[str, Resolution]:
        path, body = c.build_evaluate(None, flags, environment or self.environment)
        data = await self._request("POST", path, json=body)
        return {pid: c.parse_resolution(res, pid)
                for pid, res in data.get("resolutions", {}).items()}

    async def prompts(self, *, environment: str | None = None) -> list[PromptInfo]:
        params = _env_params(environment or self.environment)
        return c.parse_prompts(await self._request("GET", "/prompts", params=params))

    async def prompt(self, prompt_id: str, *,
                     environment: str | None = None) -> PromptSpec:
        params = _env_params(environment or self.environment)
        return c.parse_spec(await self._request("GET", f"/prompt/{prompt_id}/spec",
                                                params=params))

    async def _request(self, method: str, path: str, **kwargs: Any) -> dict:
        last: Exception | None = None
        for attempt in range(self._retries + 1):
            if attempt:
                await asyncio.sleep(c.BACKOFFS[min(attempt - 1, len(c.BACKOFFS) - 1)])
            try:
                resp = await self._http.request(method, path, **kwargs)
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

    async def close(self) -> None:
        await self._http.aclose()

    async def __aenter__(self) -> "AsyncIncant":
        return self

    async def __aexit__(self, *exc: Any) -> None:
        await self.close()
