"""Thin HTTP client for the management surface, reusing the SDK's auth headers
and error mapping so MCP tools speak the exact same wire dialect. Permissions
are entirely the server's: this client adds nothing and hides nothing — a 403
carries the server's role explanation verbatim."""

from __future__ import annotations

from typing import Any

import httpx
from incant_sdk import _common as c


class Mgmt:
    def __init__(self, url: str, key: str, timeout: float = 15.0):
        self._http = httpx.Client(base_url=url.rstrip("/"), headers=c.headers(key),
                                  timeout=timeout)
        self._default_env: str | None = None

    def request(self, method: str, path: str, **kwargs: Any) -> Any:
        resp = self._http.request(method, path, **kwargs)
        if resp.status_code >= 400:
            raise c.response_error(resp)
        return resp.json()

    def get(self, path: str, **params: Any) -> Any:
        return self.request("GET", path, params={k: v for k, v in params.items()
                                                 if v is not None})

    def post(self, path: str, body: dict | None = None, **params: Any) -> Any:
        return self.request("POST", path, json=body or {},
                            params={k: v for k, v in params.items() if v is not None})

    def env(self, environment: str | None) -> str:
        """Resolve the working environment: explicit > INCANT_ENVIRONMENT >
        the deployment's marked default (fetched once)."""
        import os
        if environment:
            return environment
        if os.environ.get("INCANT_ENVIRONMENT"):
            return os.environ["INCANT_ENVIRONMENT"]
        if self._default_env is None:
            envs = self.get("/mgmt/envs").get("environments", [])
            self._default_env = next((e["id"] for e in envs if e.get("default")),
                                     envs[0]["id"] if envs else "prod")
        return self._default_env
