"""Request/response budgets (§8): the request-body cap (a pure-ASGI middleware, both
the Content-Length door and the streamed-bytes door) and the rendered-output cap as
seen through the app (a 4xx render error, never a 500). The middleware's own 413
path — for a body read outside FastAPI's exception handling — is driven ASGI-direct
with a fake receive so the mid-stream cut-off is asserted chunk by chunk."""

from __future__ import annotations

import asyncio
import json

import pytest

from incant.server.app import _BodyTooLarge, _RequestBodyLimit

from .test_server import auth, make_client

RENDER = {"environment": "prod", "flags": {},
          "variables": {"customer_name": "Acme", "history": []}}


@pytest.fixture()
def client(tmp_path):
    # The floors of both settings, so the tests stay small and fast.
    with make_client(tmp_path, max_request_bytes=4096, max_render_bytes=1024) as c:
        yield c


def _padded(n: int) -> bytes:
    return json.dumps({**RENDER, "variables": {**RENDER["variables"], "pad": "x" * n}}).encode()


def test_under_cap_requests_are_untouched(client):
    r = client.post("/prompt/support/system", headers=auth(client.renderer_key), json=RENDER)
    assert r.status_code == 200, r.text
    assert "connection" not in r.headers


def test_oversized_content_length_is_refused_before_the_body_is_read(client):
    body = _padded(5000)
    r = client.post("/prompt/support/system", content=body,
                    headers={**auth(client.renderer_key), "content-type": "application/json"})
    assert r.status_code == 413
    assert r.json()["detail"] == "request body exceeds the 4096-byte limit (INCANT_MAX_REQUEST_BYTES)"
    assert r.headers["connection"] == "close"            # the server drops the unread body
    assert r.headers["x-content-type-options"] == "nosniff"  # security headers wrap the 413
    # Every surface, not just serving: a mgmt draft body over the cap is refused too.
    r = client.post("/mgmt/prompts/support/system/drafts", content=body,
                    headers={**auth(), "content-type": "application/json"})
    assert r.status_code == 413


def test_chunked_body_without_content_length_is_cut_off_at_the_cap(client):
    body = _padded(5000)

    def chunks():
        yield body[:2500]
        yield body[2500:]
    r = client.post("/prompt/support/system", content=chunks(),
                    headers={**auth(client.renderer_key), "content-type": "application/json"})
    assert "content-length" not in r.request.headers      # streamed: the second door
    assert r.status_code == 413
    assert "4096-byte" in r.json()["detail"]
    assert r.headers["connection"] == "close"


def _run(limit, inner, chunks, scope_headers=()):
    it = iter(chunks)
    sent = []

    async def receive():
        try:
            return {"type": "http.request", "body": next(it), "more_body": True}
        except StopIteration:
            return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message):
        sent.append(message)

    scope = {"type": "http", "headers": list(scope_headers)}
    asyncio.run(_RequestBodyLimit(inner, max_bytes=limit)(scope, receive, send))
    return sent, list(it)


def _draining_app(consumed):
    """A raw ASGI app that reads its whole body — no FastAPI exception handling between
    it and the middleware, so the middleware must answer the 413 itself."""
    async def app(scope, receive, send):
        while True:
            m = await receive()
            consumed.append(len(m.get("body", b"")))
            if not m.get("more_body"):
                break
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})
    return app


def test_streamed_body_is_cut_off_mid_stream():
    consumed = []
    sent, left = _run(1000, _draining_app(consumed), [b"x" * 300] * 5)
    assert consumed == [300, 300, 300]          # the 4th chunk crosses 1000 and never lands
    assert left == [b"x" * 300]                 # the 5th was never even pulled
    assert sent[0]["type"] == "http.response.start" and sent[0]["status"] == 413
    assert (b"connection", b"close") in sent[0]["headers"]
    assert b"1000-byte limit" in sent[1]["body"]


def test_exactly_the_cap_passes():
    consumed = []
    sent, _ = _run(1000, _draining_app(consumed), [b"x" * 500, b"x" * 500])
    assert consumed == [500, 500, 0] and sent[0]["status"] == 200


def test_declared_length_over_the_cap_never_calls_the_app():
    async def never(scope, receive, send):  # pragma: no cover - the assertion
        raise AssertionError("app must not run")
    sent, left = _run(1000, never, [b"x" * 10], scope_headers=[(b"content-length", b"5000")])
    assert sent[0]["status"] == 413 and left == [b"x" * 10]


def test_non_http_scopes_pass_straight_through():
    seen = []

    async def inner(scope, receive, send):
        seen.append(scope["type"])
    asyncio.run(_RequestBodyLimit(inner, max_bytes=1)({"type": "lifespan"}, None, None))
    assert seen == ["lifespan"]


def test_over_cap_after_the_response_started_propagates():
    """Too late to say 413: the status is spoken for, so the error must surface to the
    server (which aborts the connection) instead of being swallowed."""
    async def late_reader(scope, receive, send):
        await send({"type": "http.response.start", "status": 200, "headers": []})
        while (await receive()).get("more_body"):
            pass
    with pytest.raises(_BodyTooLarge):
        _run(100, late_reader, [b"x" * 60, b"x" * 60])


def test_output_over_the_render_budget_is_a_422_not_a_500(client):
    # support/system renders every history message; 2 KB of history against a 1 KiB cap.
    history = [{"text": "x" * 100} for _ in range(20)]
    body = {**RENDER, "variables": {"customer_name": "Acme", "history": history}}
    r = client.post("/prompt/support/system", headers=auth(client.renderer_key), json=body)
    assert r.status_code == 422, r.text
    assert "render limit" in r.json()["detail"]["detail"]
    # Same budget through the mgmt preview door.
    r = client.post("/mgmt/prompts/support/system/preview", headers=auth(), json=body)
    assert r.status_code == 422, r.text
    # Under the cap, the same template renders.
    body["variables"]["history"] = history[:5]
    assert client.post("/prompt/support/system", headers=auth(client.renderer_key),
                       json=body).status_code == 200
