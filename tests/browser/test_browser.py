"""Browser end-to-end tests, promoted from the ad-hoc Playwright verification
drives. Each is an independent test function with a fresh context/page; the
module-scoped server is seeded once. State-mutating tests are ordered so they
don't collide and, where useful, create their own targets (a fresh draft) so
they're order-independent — see the per-test notes.

Ordering rationale (top to bottom = run order):
  - The two read-only/no-mutation tests run first.
  - `explicit_draft_and_autosave_conflict` needs a pristine support/greeting
    (its "Start editing" read-only state only appears with no open draft of the
    signed-in user), so it runs before `review_invalidation`, which creates its
    own dedicated draft via the mgmt API and deep-links it.
  - `targeting_composer` then `publish_impact` operate on support/system; the
    publish consumes the seeded tip-ahead edits, so it runs last of the two.
  - `signout_everywhere` and the mobile/reduced-motion checks are self-contained.
"""

from __future__ import annotations

import os

import pytest

from .conftest import ADMIN_KEY, signin

pytestmark = pytest.mark.skipif(
    os.environ.get("INCANT_BROWSER_TESTS") != "1",
    reason="browser suite is opt-in — set INCANT_BROWSER_TESTS=1 and install the 'browser' group",
)


def test_signin_session_security(server, new_context):
    """401 sign-in card (no dev-key leak) → sign in → HttpOnly SameSite=Strict
    cookie, zero credentials in web storage / State.token, self-hosted fonts."""
    requests = []
    ctx = new_context()
    page = ctx.new_page()
    page.on("request", lambda r: requests.append(r.url))

    page.goto(server + "/#/prompts")
    page.wait_for_selector("#signinBtn", timeout=15000)

    card = page.inner_text(".signin-card")
    assert "Sign in to Incant" in card
    # The card must never advertise the well-known dev key.
    assert "incant_sk_dev_admin" not in card
    assert "dev key" not in card.lower() and "dev_admin" not in card.lower()

    signin(page, ADMIN_KEY, remember=False)
    page.wait_for_selector("text=showing what's live", timeout=15000)

    # Session cookie: HttpOnly + SameSite=Strict.
    sess = [c for c in ctx.cookies() if c["name"] == "incant_session"]
    assert sess, "no incant_session cookie set"
    assert sess[0]["httpOnly"] is True
    assert sess[0]["sameSite"] == "Strict"

    # No credential is readable from JS: legacy storage keys purged, State.token empty.
    stored = page.evaluate(
        "() => ({ls: localStorage.getItem('incant_token'),"
        " ss: sessionStorage.getItem('incant_token'),"
        " token: (typeof State !== 'undefined') ? State.token : null})"
    )
    assert stored["ls"] is None and stored["ss"] is None
    assert stored["token"] in ("", None)

    # Fonts are self-hosted — at least one /ui/fonts/ request, nothing to Google.
    assert any("/ui/fonts/" in u for u in requests), "no self-hosted font request seen"
    assert not any(("googleapis" in u or "gstatic" in u or "fonts.google" in u) for u in requests)


def test_csrf_forgery_blocked(server, page):
    """A cookie-authenticated in-page POST without the CSRF header is 403
    csrf_required; the same POST over a bearer key is CSRF-immune."""
    page.goto(server + "/#/prompts")
    signin(page)
    page.wait_for_selector("text=showing what's live", timeout=15000)

    # Forged request: valid session cookie rides along automatically, but no
    # X-Incant-CSRF header. Targets an existing prompt id so, even without the
    # guard, nothing would mutate (a duplicate is a 409).
    forged = page.evaluate(
        "async () => {"
        " const r = await fetch('/mgmt/prompts', {method:'POST', cache:'no-store',"
        "   headers:{'Content-Type':'application/json'},"
        "   body: JSON.stringify({prompt_id:'support/system', description:'csrf probe'})});"
        " let d=null; try { d = await r.json(); } catch(_) {}"
        " return {status: r.status, detail: (d && d.detail) || d};"
        "}"
    )
    assert forged["status"] == 403, forged
    assert forged["detail"] == "csrf_required", forged

    # Bearer auth never reaches the CSRF guard → not 403 (409 duplicate, no mutation).
    bearer = page.evaluate(
        "async () => {"
        " const r = await fetch('/mgmt/prompts', {method:'POST', cache:'no-store',"
        "   headers:{'Content-Type':'application/json','Authorization':'Bearer " + ADMIN_KEY + "'},"
        "   body: JSON.stringify({prompt_id:'support/system', description:'csrf probe'})});"
        " return {status: r.status};"
        "}"
    )
    assert bearer["status"] != 403, bearer
    assert bearer["status"] == 409, bearer


def test_explicit_draft_and_autosave_conflict(server, context):
    """The edit route shows a read-only 'Start editing' state (no silent
    auto-create); two pages in one context editing the same draft produce a 409
    conflict banner, and 'Load the newer version' recovers.

    One context = one cookie session, so the second page is already signed in
    (no per-tab re-auth) — the current cookie-session model, unlike the old
    per-tab token storage."""
    page = context.new_page()
    page.goto(server + "/#/p/support%2Fgreeting/draft")
    signin(page)

    # Read-only start state — the draft is created only on an explicit click.
    page.wait_for_selector("text=/Start editing v/", timeout=15000)
    page.click("text=/Start editing v/")
    page.wait_for_selector("#draftTa", timeout=15000)
    draft_url = page.url
    assert "draft=" in draft_url, draft_url

    page.fill("#draftTa", "Hello {{ customer_name }} — version A")
    page.wait_for_timeout(1600)  # let autosave land and advance the chain

    # Second tab on the same draft — shares the cookie, so no sign-in needed.
    page2 = context.new_page()
    page2.goto(draft_url)
    page2.wait_for_selector("#draftTa", timeout=15000)

    # Page A moves the draft forward; page2's chain is now stale.
    page.fill("#draftTa", "Hello {{ customer_name }} — version A2")
    page.wait_for_timeout(1600)
    page2.fill("#draftTa", "Hello {{ customer_name }} — version B")
    page2.wait_for_timeout(1800)

    assert "changed somewhere else" in page2.inner_text("#main")

    page2.click("text=Load the newer version")
    page2.wait_for_timeout(1000)
    assert "version A2" in page2.input_value("#draftTa")


def test_review_invalidation(server, page, bearer):
    """Approve a draft, then edit it: the approval becomes an 'earlier revision'
    and the button relabels to Re-approve. Uses a dedicated draft (created via
    the mgmt API and deep-linked) so it never collides with the conflict test."""
    draft = bearer(
        "POST", "/mgmt/prompts/support/greeting/drafts",
        {"version_number": 2, "title": "review-invalidation",
         "content": "Hi {{ customer_name }} — review test A"},
    )
    draft_id = draft["id"]

    page.goto(server + f"/#/p/support%2Fgreeting/draft?draft={draft_id}&tab=review")
    signin(page)
    page.wait_for_selector("text=/Approve/", timeout=15000)
    page.click("text=/^Approve ✓$/")
    page.wait_for_timeout(1000)
    assert "✓" in page.inner_text("#main")  # approval verdict pill

    # Edit on the write tab — a content change invalidates the approval.
    page.goto(server + f"/#/p/support%2Fgreeting/draft?draft={draft_id}")
    page.wait_for_selector("#draftTa", timeout=15000)
    page.fill("#draftTa", "Hi {{ customer_name }} — changed after approval")
    page.wait_for_timeout(1600)

    page.goto(server + f"/#/p/support%2Fgreeting/draft?draft={draft_id}&tab=review")
    page.wait_for_selector("text=/earlier revision/", timeout=15000)
    assert "Re-approve" in page.inner_text("#main")


def test_targeting_composer_and_audience(server, page):
    """Create a rule through the composer (a flag condition + comment, with the
    shadow/position box present); it appears in the list, and the audience tester
    resolves a matching user through the real serving path."""
    page.goto(server + "/#/p/support%2Fsystem/rules")
    signin(page)
    page.wait_for_selector("text=Who sees what", timeout=15000)

    page.click("text=＋ New rule")
    page.wait_for_selector("text=People who match", timeout=8000)
    page.click("text=＋ add condition")
    page.wait_for_timeout(300)
    row = page.locator(".cb-row").last
    row.locator("input").first.fill("tier")
    row.locator("input").last.fill("vip")
    page.fill("#co-comment", "VIP accounts get the formal voice")
    page.wait_for_timeout(300)

    modal = page.inner_text(".modal")
    assert ("checked before" in modal.lower() or "never be reached" in modal.lower()
            or "1st" in modal.lower() or "Checked" in modal), modal[:300]

    page.click("text=/^Create rule$/")
    page.wait_for_timeout(1200)
    assert "VIP accounts get the formal voice" in page.inner_text("#main")

    # Audience tester: a seeded user that a rule targets → resolves to a version.
    page.locator("#audFlags").fill('{"user_id": "u_12"}')
    page.click("text=/^Check$/")
    page.wait_for_timeout(1500)
    assert "they'd get" in page.inner_text("#main").lower()


def test_publish_impact_flow(server, page):
    """The publish impact modal shows the rendered diff, the edits going live, and
    who's affected (with a redundant test-rule to clean up); on a locked env the
    type-to-confirm enables Publish, and the publish lands."""
    page.goto(server + "/#/p/support%2Fsystem/pointers")
    signin(page)
    page.wait_for_selector("text=Publish history", timeout=15000)

    page.click("button[data-act='makeLive']")
    page.wait_for_selector("text=/Publish to prod/", timeout=8000)
    page.wait_for_timeout(1500)  # let the impact diff resolve

    modal = page.inner_text(".modal")
    assert "The edits going live" in modal, modal[:400]
    assert ("Who's affected" in modal or "Everyone" in modal), modal[:400]
    # A prompt-scoped @tip rule (seeded) is offered for cleanup after publish.
    assert page.locator("input[data-act='publishRuleToggle']").count() >= 1

    # prod is protected → type the prompt id to enable Publish.
    assert page.locator("#publishBtn").is_disabled()
    page.fill("#publishConfirm", "support/system")
    page.wait_for_timeout(300)
    assert not page.locator("#publishBtn").is_disabled()

    page.click("#publishBtn")
    page.wait_for_selector(".modal", state="detached", timeout=10000)
    # After the pointer move, the tip is live and there's nothing left to publish.
    page.wait_for_selector("text=/already live/", timeout=10000)


def test_signout_everywhere(server, page):
    """The account menu lists this device; 'Sign out everywhere' ends every
    session and GET /auth/session then returns 401."""
    page.goto(server + "/#/prompts")
    signin(page)
    page.wait_for_selector("text=showing what's live", timeout=15000)

    page.click("#acctChip")
    page.wait_for_selector("text=Sign out everywhere", timeout=8000)
    page.wait_for_selector("text=this device", timeout=8000)  # async session list

    page.click("text=Sign out everywhere")
    page.wait_for_selector("text=/Ends every signed-in session/", timeout=8000)
    page.click("button[data-act='signOutEverywhereConfirm']")

    # Back to the sign-in card, and the session is gone server-side.
    page.wait_for_selector(".signin-card", timeout=8000)
    status = page.evaluate(
        "async () => (await fetch('/auth/session', {cache:'no-store'})).status"
    )
    assert status == 401


def test_mobile_drawer_and_reduced_motion(server, new_context):
    """The hamburger drawer opens at an 800px viewport; a reduced-motion context
    disables the brand-mark animation."""
    ctx = new_context(viewport={"width": 800, "height": 900})
    page = ctx.new_page()
    page.goto(server + "/#/prompts")
    signin(page)
    page.wait_for_selector("text=showing what's live", timeout=15000)

    assert page.locator("button[aria-label='Menu']").count() >= 1
    assert not page.evaluate("document.body.classList.contains('nav-open')")
    page.click("button[aria-label='Menu']")
    page.wait_for_timeout(400)
    assert page.evaluate("document.body.classList.contains('nav-open')")

    # Reduced motion: the sidebar star's glimmer animation is switched off.
    rm = new_context(reduced_motion="reduce")
    page2 = rm.new_page()
    page2.goto(server + "/#/prompts")
    signin(page2)
    page2.wait_for_selector(".brand .star", timeout=15000)
    anim = page2.evaluate(
        "getComputedStyle(document.querySelector('.brand .star')).animationName"
    )
    assert anim in ("none", ""), anim
