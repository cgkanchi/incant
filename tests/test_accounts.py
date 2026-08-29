"""Human accounts (§11 Users): first-boot setup, password sign-in, invites,
resets, disable, password change. API keys stay the machine door throughout."""

from __future__ import annotations

import concurrent.futures as cf
import datetime as dt
import threading

from sqlalchemy import select

from incant import models
from incant.db import session_scope
from incant.server.accounts import (
    begin_initial_setup,
    create_user,
    issue_invite,
    redeem_invite,
)

from .test_server import ADMIN, auth, make_client

EMAIL = "pat@example.com"
PW = "correct-horse-battery"


def _setup_admin(client, email=EMAIL, name="Pat", password=PW):
    r = client.post("/auth/setup", json={"name": name, "email": email, "password": password})
    assert r.status_code == 200, r.text
    return r.json()


def _login(client, email=EMAIL, password=PW, remember=False):
    return client.post("/auth/session",
                       json={"email": email, "password": password, "remember": remember})


# ── first-boot setup ─────────────────────────────────────────────────

def test_setup_flow_creates_first_admin(tmp_path):
    with make_client(tmp_path) as client:
        assert client.get("/auth/setup").json() == {"needs_setup": True}

        body = _setup_admin(client)
        assert {"role": "admin", "project_id": None, "environment_id": None} in body["roles"]
        assert body["csrf"]
        # Signed in immediately: cookie-authenticated whoami works.
        assert client.get("/auth/session").status_code == 200

        assert client.get("/auth/setup").json() == {"needs_setup": False}
        # Exactly once: a second setup attempt is refused outright.
        r = client.post("/auth/setup", json={"name": "Mallory",
                                             "email": "mallory@example.com",
                                             "password": "n0t-t0day-thanks"})
        assert r.status_code == 409


def test_setup_validation(tmp_path):
    with make_client(tmp_path) as client:
        assert client.post("/auth/setup", json={
            "name": "Pat", "email": "not-an-email", "password": PW}).status_code == 422
        assert client.post("/auth/setup", json={
            "name": "Pat", "email": EMAIL, "password": "short"}).status_code == 422
        assert client.get("/auth/setup").json()["needs_setup"] is True  # nothing landed


def test_concurrent_setup_allows_exactly_one_initial_admin(tmp_path):
    with make_client(tmp_path):
        start = threading.Barrier(2)
        winner_has_lock = threading.Event()
        release_winner = threading.Event()

        def attempt_setup(index: int) -> bool:
            with session_scope() as s:
                start.wait(timeout=5)
                if not begin_initial_setup(s):
                    return False

                # Keep the database empty while the other transaction attempts
                # setup.  It must block on the advisory lock, not also see zero.
                winner_has_lock.set()
                assert release_winner.wait(timeout=5)
                user = create_user(
                    s, email=f"admin-{index}@example.com", name=f"Admin {index}"
                )
                user.status = "active"
                s.add(models.RoleBinding(principal_id=user.principal_id, role="admin"))
                return True

        with cf.ThreadPoolExecutor(max_workers=2) as executor:
            futures = [executor.submit(attempt_setup, index) for index in range(2)]
            try:
                assert winner_has_lock.wait(timeout=5)
            finally:
                release_winner.set()
            results = [future.result(timeout=5) for future in futures]

        assert sorted(results) == [False, True]
        with session_scope() as s:
            user = s.execute(select(models.User)).scalar_one()
            assert len(s.execute(select(models.RoleBinding).where(
                models.RoleBinding.role == "admin",
                models.RoleBinding.principal_id == user.principal_id,
            )).scalars().all()) == 1


# ── password sign-in ─────────────────────────────────────────────────

def test_password_login_and_uniform_failures(tmp_path):
    with make_client(tmp_path) as client:
        _setup_admin(client)
        client.cookies.clear()

        r = _login(client, email="PAT@Example.COM")   # case-insensitive email
        assert r.status_code == 200 and r.json()["name"] == "Pat"

        # Wrong password and unknown email fail identically — no account oracle.
        wrong = _login(client, password="wrong-password-here")
        ghost = _login(client, email="ghost@example.com", password="wrong-password-here")
        assert wrong.status_code == ghost.status_code == 401
        assert wrong.json()["detail"] == ghost.json()["detail"]

        # Both doors at once is a caller error; neither alone half-supplied works.
        assert client.post("/auth/session", json={
            "email": EMAIL, "password": PW, "key": ADMIN}).status_code == 422
        assert client.post("/auth/session", json={"email": EMAIL}).status_code == 422

        # The API-key door still works (machines, recovery, tests).
        assert client.post("/auth/session", json={"key": ADMIN}).status_code == 200


def test_password_failures_are_throttled(tmp_path):
    with make_client(tmp_path, auth_throttle_limit=3, auth_throttle_window=60.0) as client:
        _setup_admin(client)
        client.cookies.clear()
        for _ in range(3):
            assert _login(client, password="wrong-password-here").status_code == 401
        r = _login(client, password="wrong-password-here")
        assert r.status_code == 429 and r.headers.get("Retry-After")


# ── invites ──────────────────────────────────────────────────────────

def test_invite_lifecycle(tmp_path):
    with make_client(tmp_path) as client:
        r = client.post("/mgmt/users",
                        json={"email": "sam@example.com", "name": "Sam",
                              "role": "editor", "project_id": "support"},
                        headers=auth())
        assert r.status_code == 200, r.text
        body = r.json()
        token = body["invite_token"]
        assert token and body["invite_path"].endswith(token)
        assert body["user"]["status"] == "invited"

        listed = client.get("/mgmt/users", headers=auth()).json()["users"]
        assert listed[0]["email"] == "sam@example.com"
        assert listed[0]["invite_pending"] is True

        # Weak password refused; the token survives the attempt.
        assert client.post("/auth/accept-invite",
                           json={"token": token, "password": "short"}).status_code == 422

        r = client.post("/auth/accept-invite",
                        json={"token": token, "password": PW, "name": "Sam R."})
        assert r.status_code == 200, r.text
        assert {"role": "editor", "project_id": "support",
                "environment_id": None} in r.json()["roles"]

        # Single use: redeeming again fails; password sign-in now works.
        assert client.post("/auth/accept-invite",
                           json={"token": token, "password": PW}).status_code == 401
        client.cookies.clear()
        assert _login(client, email="sam@example.com").status_code == 200

        # Duplicate invite for an existing email is refused.
        assert client.post("/mgmt/users", json={"email": "Sam@example.com"},
                           headers=auth()).status_code == 409


def test_concurrent_invite_redemption_has_one_winner(tmp_path):
    with make_client(tmp_path):
        with session_scope() as s:
            user = create_user(s, email="race@example.com", name="Race")
            token = issue_invite(user)

        start = threading.Barrier(2)
        winner_has_lock = threading.Event()
        release_winner = threading.Event()

        def redeem() -> bool:
            with session_scope() as s:
                start.wait(timeout=5)
                user = redeem_invite(s, token)
                if user is None:
                    return False

                # Hold the matching row lock while the competing SELECT runs.
                winner_has_lock.set()
                assert release_winner.wait(timeout=5)
                user.invite_token_hash = None
                user.invite_expires_at = None
                user.status = "active"
                return True

        with cf.ThreadPoolExecutor(max_workers=2) as executor:
            futures = [executor.submit(redeem) for _ in range(2)]
            try:
                assert winner_has_lock.wait(timeout=5)
            finally:
                release_winner.set()
            results = [future.result(timeout=5) for future in futures]

        assert sorted(results) == [False, True]
        with session_scope() as s:
            user = s.execute(select(models.User).where(
                models.User.email == "race@example.com"
            )).scalar_one()
            assert user.status == "active"
            assert user.invite_token_hash is None


def test_reset_link_replaces_old_token_and_password(tmp_path):
    with make_client(tmp_path) as client:
        first = client.post("/mgmt/users", json={"email": "lee@example.com"},
                            headers=auth()).json()
        uid = first["user"]["id"]
        second = client.post(f"/mgmt/users/{uid}/reset", headers=auth()).json()
        assert client.post("/auth/accept-invite",
                           json={"token": first["invite_token"],
                                 "password": PW}).status_code == 401  # superseded
        assert client.post("/auth/accept-invite",
                           json={"token": second["invite_token"],
                                 "password": PW}).status_code == 200

        # Reset for an ACTIVE user: old password keeps working until redemption,
        # then the new one replaces it.
        third = client.post(f"/mgmt/users/{uid}/reset", headers=auth()).json()
        client.cookies.clear()
        assert _login(client, email="lee@example.com", password=PW).status_code == 200
        assert client.post("/auth/accept-invite",
                           json={"token": third["invite_token"],
                                 "password": "a-brand-new-secret"}).status_code == 200
        client.cookies.clear()
        assert _login(client, email="lee@example.com", password=PW).status_code == 401
        assert _login(client, email="lee@example.com",
                      password="a-brand-new-secret").status_code == 200


def test_expired_invite_is_refused(tmp_path):
    with make_client(tmp_path) as client:
        body = client.post("/mgmt/users", json={"email": "old@example.com"},
                           headers=auth()).json()
        with session_scope() as s:
            u = s.get(models.User, body["user"]["id"])
            u.invite_expires_at = dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=1)
        assert client.post("/auth/accept-invite",
                           json={"token": body["invite_token"],
                                 "password": PW}).status_code == 401


# ── disable / enable ─────────────────────────────────────────────────

def test_disable_is_immediate_and_total(tmp_path):
    with make_client(tmp_path) as client:
        invited = client.post("/mgmt/users", json={"email": "dana@example.com"},
                              headers=auth()).json()
        uid = invited["user"]["id"]
        pid = invited["user"]["principal_id"]
        client.post("/auth/accept-invite",
                    json={"token": invited["invite_token"], "password": PW})
        # They also hold an API key (admin issued it to their principal).
        key = client.post(f"/mgmt/principals/{pid}/keys", headers=auth()).json()["key"]
        assert client.get("/mgmt/overview?environment=prod",
                          headers=auth(key)).status_code in (200, 403)  # key authenticates

        r = client.patch(f"/mgmt/users/{uid}", json={"disabled": True}, headers=auth())
        assert r.status_code == 200 and r.json()["user"]["status"] == "disabled"

        # Session dead, key revoked, password refused — all at once.
        assert client.get("/auth/session").status_code == 401
        assert client.get("/mgmt/overview?environment=prod",
                          headers=auth(key)).status_code == 401
        assert _login(client, email="dana@example.com", password=PW).status_code == 401
        # And a reset link can't be minted for a disabled account.
        assert client.post(f"/mgmt/users/{uid}/reset", headers=auth()).status_code == 409

        # Re-enable: the password still stands (nothing else is restored).
        client.patch(f"/mgmt/users/{uid}", json={"disabled": False}, headers=auth())
        assert _login(client, email="dana@example.com", password=PW).status_code == 200


def test_cannot_disable_own_account(tmp_path):
    with make_client(tmp_path) as client:
        _setup_admin(client)
        with session_scope() as s:
            uid = s.execute(select(models.User)).scalar_one().id
        csrf = client.get("/auth/session").json()["csrf"]
        r = client.patch(f"/mgmt/users/{uid}", json={"disabled": True},
                         headers={"X-Incant-CSRF": csrf})
        assert r.status_code == 409


# ── password change ──────────────────────────────────────────────────

def test_password_change_rotates_and_revokes_other_sessions(tmp_path):
    with make_client(tmp_path) as client:
        _setup_admin(client)
        csrf = client.get("/auth/session").json()["csrf"]
        cookie_a = client.cookies.get("incant_session")

        # A second session for the SAME user (another device); it must die on rotation.
        assert _login(client).status_code == 200          # jar now holds session B
        client.cookies.set("incant_session", cookie_a)    # act as session A again

        r = client.post("/auth/password",
                        json={"current_password": "wrong-guess-entirely",
                              "new_password": "an-even-better-one"},
                        headers={"X-Incant-CSRF": csrf})
        assert r.status_code == 403

        r = client.post("/auth/password",
                        json={"current_password": PW,
                              "new_password": "an-even-better-one"},
                        headers={"X-Incant-CSRF": csrf})
        assert r.status_code == 204

        # Current session survives; the other one is gone; only the new password works.
        assert client.get("/auth/session").status_code == 200
        with session_scope() as s:
            assert len(s.execute(select(models.Session)).scalars().all()) == 1
        client.cookies.clear()
        assert _login(client, password=PW).status_code == 401
        assert _login(client, password="an-even-better-one").status_code == 200
