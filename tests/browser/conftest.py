"""Fixtures for the opt-in browser end-to-end suite (tests/browser).

The whole suite is gated twice over so the default ``uv run pytest`` is never
disturbed:

* If Playwright is not importable (the ``browser`` dependency group isn't
  installed), ``collect_ignore_glob`` below drops every ``test_*.py`` here, so
  collection stays at the baseline 179 passed / 2 skipped with zero new errors.
* When Playwright *is* installed but ``INCANT_BROWSER_TESTS`` isn't ``1``, the
  module-level ``skipif`` in each test file skips them (they show as skipped and
  the module-scoped server never boots).

To run for real::

    INCANT_BROWSER_TESTS=1 uv run --group browser pytest tests/browser -q

The server is a real uvicorn subprocess over a throwaway SQLite DB + git repo,
seeded once per module via the ``incant`` CLI. Browser flows are driven with the
Playwright *sync* API directly (no pytest-playwright), exactly like the ad-hoc
verification drives this suite was promoted from.
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request

import pytest

# ── collection guard ─────────────────────────────────────────────────────────
# Without Playwright the browser tests must not even be collected, so the default
# suite's counts are untouched. conftest is always imported (no __init__ needed),
# but its playwright import must be optional.
try:  # noqa: SIM105
    import playwright  # noqa: F401
    _HAS_PLAYWRIGHT = True
except Exception:  # pragma: no cover - exercised only when the group is absent
    _HAS_PLAYWRIGHT = False

if not _HAS_PLAYWRIGHT:  # pragma: no cover
    collect_ignore_glob = ["test_*.py"]


# ── constants ────────────────────────────────────────────────────────────────
ADMIN_KEY = "incant_sk_dev_admin"
CHROME = os.environ.get("INCANT_BROWSER_CHROME", "/usr/bin/google-chrome")


def _free_port() -> int:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _wait_ready(base_url: str, proc: subprocess.Popen, log_path: str, timeout: float = 40.0) -> None:
    """Poll /readyz until the node reports 'ready' (warm complete), or fail with logs."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if proc.poll() is not None:
            raise RuntimeError(
                f"server exited early (rc={proc.returncode}):\n{_tail(log_path)}"
            )
        try:
            with urllib.request.urlopen(base_url + "/readyz", timeout=1) as r:
                if r.status == 200 and r.read().decode().strip() == "ready":
                    return
        except Exception:
            pass
        time.sleep(0.2)
    raise RuntimeError(f"server not ready within {timeout}s:\n{_tail(log_path)}")


def _tail(path: str, n: int = 60) -> str:
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            return "".join(f.readlines()[-n:])
    except OSError:
        return "(no server log)"


# ── server ───────────────────────────────────────────────────────────────────
@pytest.fixture(scope="module")
def server(tmp_path_factory):
    """A real uvicorn subprocess on a free port over a throwaway SQLite DB + repo.

    ``incant init`` + ``incant seed`` run once (module scope) via the CLI as a
    subprocess (``python -m incant.cli`` — resolves against the installed package,
    no ``uv run`` needed), then uvicorn serves ``incant.server:app``. Yields the
    base URL; the process is terminated on teardown.
    """
    tmp = tmp_path_factory.mktemp("incant-browser")
    env = dict(os.environ)
    env.update({
        "INCANT_DATABASE_URL": f"sqlite:///{tmp}/incant.db",
        "INCANT_REPO_PATH": f"{tmp}/repo",
        "INCANT_ALLOW_DEV_KEY": "1",
        "INCANT_BOOTSTRAP_ADMIN_KEY": ADMIN_KEY,
        "INCANT_MODE": "full",
    })

    for step in (["init"], ["seed"]):
        r = subprocess.run(
            [sys.executable, "-m", "incant.cli", *step],
            env=env, capture_output=True, text=True,
        )
        if r.returncode != 0:
            raise RuntimeError(f"`incant {step[0]}` failed:\n{r.stdout}\n{r.stderr}")

    port = _free_port()
    base_url = f"http://127.0.0.1:{port}"
    log_path = str(tmp / "server.log")
    with open(log_path, "w") as log:
        proc = subprocess.Popen(
            [sys.executable, "-m", "uvicorn", "incant.server:app",
             "--host", "127.0.0.1", "--port", str(port), "--log-level", "warning"],
            env=env, stdout=log, stderr=subprocess.STDOUT,
        )
    try:
        _wait_ready(base_url, proc, log_path)
        yield base_url
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=8)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=5)


@pytest.fixture(scope="module")
def bearer(server):
    """Server-side mgmt calls with the admin bearer key (bypasses the browser
    session). Used to set up per-test state (e.g. a fresh draft) so tests stay
    independent regardless of order."""
    def _req(method: str, path: str, body=None):
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(
            server + path, data=data, method=method,
            headers={"Authorization": f"Bearer {ADMIN_KEY}", "Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(req, timeout=15) as r:
                text = r.read().decode()
                return json.loads(text) if text else None
        except urllib.error.HTTPError as e:  # surface the body for debugging
            raise RuntimeError(f"{method} {path} -> {e.code}: {e.read().decode()}") from e
    return _req


# ── playwright ───────────────────────────────────────────────────────────────
@pytest.fixture(scope="session")
def _playwright():
    from playwright.sync_api import sync_playwright
    with sync_playwright() as p:
        yield p


@pytest.fixture(scope="session")
def browser(_playwright):
    b = _playwright.chromium.launch(executable_path=CHROME, headless=True)
    yield b
    b.close()


def _attach_error_collector(page, sink):
    """Record uncaught page errors and CSP-violation console messages. A CSP
    violation surfaces as a console message naming the policy; ordinary network
    errors (e.g. an intentional 403 from the CSRF test) are not flagged."""
    page.on("pageerror", lambda e: sink.append(("pageerror", str(e))))

    def _on_console(msg):
        text = msg.text or ""
        if "Content Security Policy" in text or "violates the following" in text:
            sink.append(("csp", text))

    page.on("console", _on_console)


@pytest.fixture
def new_context(browser):
    """Factory for browser contexts with page-error / CSP collection wired to every
    page (including tabs a test opens later). Any collected error fails the test on
    teardown. Cookies are context-wide, so multi-page tests share one sign-in."""
    contexts = []
    errors = []

    def _make(**kwargs):
        ctx = browser.new_context(**kwargs)
        ctx.on("page", lambda pg: _attach_error_collector(pg, errors))
        contexts.append(ctx)
        return ctx

    yield _make

    for ctx in contexts:
        try:
            ctx.close()
        except Exception:
            pass
    assert not errors, f"page errors / CSP violations occurred: {errors}"


@pytest.fixture
def context(new_context):
    return new_context()


@pytest.fixture
def page(context):
    return context.new_page()


# ── helpers ──────────────────────────────────────────────────────────────────
def signin(page, key: str = ADMIN_KEY, remember: bool = False) -> None:
    """Fill the sign-in card and submit, waiting for it to be replaced. The card
    posts the key to /auth/session, which sets the HttpOnly session cookie."""
    page.wait_for_selector("#signinBtn", timeout=15000)
    page.fill("#signinKey", key)
    if remember:
        page.check("#signinRemember")
    page.click("#signinBtn")
    # A successful sign-in re-renders the route, dropping the sign-in card.
    page.wait_for_selector(".signin-card", state="detached", timeout=15000)
