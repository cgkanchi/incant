"""Seed the design's example dataset so the app is explorable out of the box.

Two projects (support, shared), two environments (prod protected, staging
track-tip), the support/system v1–v3 lineage with v2 live and tip ahead +2, the
shared style fragment, rules, segments, refinements, and test contexts.
"""

from __future__ import annotations

from sqlalchemy import select

from . import models
from .db import session_scope
from .server.auth import ensure_bootstrap_admin, hash_key, key_prefix
from .service import AppContext

LANGUAGE_RULES_V1 = "Write in plain English. Avoid jargon. Prefer short sentences."

SYSTEM_V2_FORMAL = (
    "You are a support agent for {{ customer_name }}.\n"
    "Always answer in a formal, professional tone.\n"
    "{% if plan_name %}The customer is on the {{ plan_name }} plan.{% endif %}\n"
    "Use plain English and avoid jargon.\n"
    "{% for m in history %}{{ m.text }}{% endfor %}"
)

SYSTEM_V2_WARM = (
    "You are a support agent for {{ customer_name }}.\n"
    "Match the customer's tone; default to warm and concise.\n"
    "{% if plan_name %}The customer is on the {{ plan_name }} plan.{% endif %}\n"
    '{% include "shared/style/language-rules" %}\n'
    "Never promise timelines you cannot verify.\n"
    "{% for m in history %}{{ m.text }}{% endfor %}"
)

SYSTEM_V3_VOICE = (
    "You're on the {{ customer_name }} support team — speak like a helpful colleague.\n"
    '{% include "shared/style/language-rules" %}\n'
    "Lead with the answer, then the why.\n"
    "{% for m in history %}{{ m.text }}{% endfor %}"
)


def _author(ctx: AppContext, s, prompt_id, version, content, author, message, *, make_live=True, env="prod"):
    reg = ctx.registry(s, author)
    if not reg.prompt_exists(prompt_id):
        reg.create_prompt(prompt_id)
    d = reg.create_draft(prompt_id, version_number=version, author=author, content=content)
    out = reg.commit_draft(d.id, author=author, message=message)
    assert out.validation["status"] == "valid", out.validation
    tgt = ctx.targeting(s, author)
    if make_live:
        tgt.make_live(env, prompt_id, version, out.sha, comment=message, force=True)
    return out


def seed() -> str:
    ctx = AppContext()
    ctx.initialize()

    with session_scope() as s:
        ensure_bootstrap_admin(s, ctx.settings.bootstrap_admin_key)
        # Environments
        for eid, protected, track_tip in [("prod", True, False), ("staging", False, True)]:
            if s.get(models.Environment, eid) is None:
                s.add(models.Environment(id=eid, name=eid, protected=protected, track_tip=track_tip))
        # Projects (seed with review_policy 0 so we can commit freely).
        reg = ctx.registry(s, "system")
        reg.ensure_project("support", review_policy=0)
        reg.ensure_project("shared", review_policy=0)

    with session_scope() as s:
        # Shared fragment first (referenced by support/system).
        _author(ctx, s, "shared/style/language-rules", 1, LANGUAGE_RULES_V1, "Rae", "language rules v1")
        _author(ctx, s, "shared/style/language-rules", 2,
                LANGUAGE_RULES_V1 + "\nNo double negatives.", "Rae", "tighten language rules",
                make_live=False)
        ctx.targeting(s, "Rae").set_default("prod", "shared/style/language-rules", 1)
        ctx.targeting(s, "Rae").set_default("staging", "shared/style/language-rules", 1)

    with session_scope() as s:
        # support/system v1 (archived), v2 (live + tip ahead +2), v3 (voice-v2 label).
        _author(ctx, s, "support/system", 1,
                "You are a support agent for {{ customer_name }}.", "Jamie", "v1 initial")
        v = s.execute(select(models.Version).where(
            models.Version.prompt_id == "support/system", models.Version.number == 1)).scalar_one()
        v.status = "archived"

    with session_scope() as s:
        # v2 first commit = live pointer target
        first = _author(ctx, s, "support/system", 2, SYSTEM_V2_FORMAL, "Dana",
                        "v2 formal baseline")
        # two more tweak commits => tip ahead +2 (do NOT move the pointer)
        reg = ctx.registry(s, "Sam")
        d = reg.create_draft("support/system", version_number=2, author="Sam",
                             content=SYSTEM_V2_FORMAL + "\n")
        reg.commit_draft(d.id, author="Sam", message="whitespace pass")
        d2 = reg.create_draft("support/system", version_number=2, author="Sam",
                              content=SYSTEM_V2_WARM)
        reg.commit_draft(d2.id, author="Sam", message="Warm tone + shared style fragment")
        ctx.targeting(s, "Sam").set_default("prod", "support/system", 2)
        ctx.targeting(s, "Sam").set_default("staging", "support/system", 2)

    with session_scope() as s:
        # v3 voice-v2
        out3 = _author(ctx, s, "support/system", 3, SYSTEM_V3_VOICE, "Maya",
                       "v3 voice rewrite", make_live=True)
        v3 = s.execute(select(models.Version).where(
            models.Version.prompt_id == "support/system", models.Version.number == 3)).scalar_one()
        v3.label = "voice-v2"
        # v3 lives in staging as default via track_tip; make it live there.
        ctx.targeting(s, "Maya").make_live("staging", "support/system", 3, out3.sha,
                                           comment="v3 to staging", force=True)

    with session_scope() as s:
        _author(ctx, s, "support/greeting", 1,
                "Hello {{ customer_name }} — thanks for reaching out. How can I help?",
                "Maya", "greeting v1")
        _author(ctx, s, "support/greeting", 2,
                "Hi {{ customer_name }}! What can I help with today?", "Maya",
                "greeting v2", make_live=False)
        ctx.targeting(s, "Maya").set_default("prod", "support/greeting", 1)
        _author(ctx, s, "support/escalation/triage", 1,
                "Triage the issue: {{ issue }}. Severity: {{ severity | default('normal') }}.",
                "Dana", "triage v1")
        ctx.targeting(s, "Dana").set_default("prod", "support/escalation/triage", 1)

    with session_scope() as s:
        tgt = ctx.targeting(s, "Dana")
        tgt.upsert_segment("prod", "beta-us", {"all": [
            {"flag": "beta_opt_in", "op": "eq", "value": True},
            {"flag": "region", "op": "in", "values": ["us", "us-gov"]},
        ]})
        # v3 needs a prod live pointer for "v3 @ live" rule targets to resolve.
        v3 = s.execute(select(models.CommitValidation).where(
            models.CommitValidation.prompt_id == "support/system",
            models.CommitValidation.version_number == 3,
            models.CommitValidation.status == "valid",
        ).order_by(models.CommitValidation.validated_at.desc())).scalars().first()
        tgt.make_live("prod", "support/system", 3, v3.sha, comment="v3 pointer for beta rule", force=True)
        tgt.upsert_rule("prod", {
            "id": "beta-gets-v3", "scope": "prompt", "prompt_id": "support/system",
            "priority": 10, "comment": "Voice v2 beta — EXP-142",
            "when": {"all": [{"segment": "beta-us"},
                             {"flag": "tier", "op": "in", "values": ["enterprise", "pro"]}]},
            "serve": {"version": 3, "at": "live"},
        })
        # team-x-tip: testing the v2 tweak before make-live
        tgt.upsert_rule("prod", {
            "id": "team-x-tip", "scope": "prompt", "prompt_id": "support/system",
            "priority": 20, "comment": "Testing the v2 tweak before make-live",
            "when": {"flag": "user_id", "op": "in", "values": ["u_12", "u_88", "u_301"]},
            "serve": {"version": 2, "at": "tip"},
        })

    with session_scope() as s:
        reg = ctx.registry(s, "system")
        reg.set_test_context("support/system", "enterprise-us",
                             {"tier": "enterprise", "region": "us"},
                             {"customer_name": "Acme Corp", "plan_name": "Enterprise", "history": []})
        reg.set_test_context("support/system", "free-eu",
                             {"tier": "free", "region": "eu"},
                             {"customer_name": "Lumen GmbH", "history": []})
        reg.set_refinement("support/system", 2, "customer_name", type="string", required=True,
                           description="The customer's company or name.")
        reg.set_refinement("support/system", 2, "history", type="list", required=False, default=[])

    with session_scope() as s:
        # Review policy on support; an open draft to populate the review/editor screens.
        proj = s.get(models.Project, "support")
        proj.review_policy = 1
        reg = ctx.registry(s, "Sam")
        reg.create_draft("support/system", version_number=2, author="Sam",
                         title="Warm tone + shared style fragment", content=SYSTEM_V2_WARM)

    # A renderer service key scoped to (support, prod).
    with session_scope() as s:
        import uuid
        raw = "incant_sk_" + uuid.uuid4().hex
        pid = "p_render_support"
        if s.get(models.Principal, pid) is None:
            s.add(models.Principal(id=pid, kind="service", subject="support-service",
                                   name="support-service"))
            s.flush()  # parent row before FK-bearing children
            s.add(models.ApiKey(principal_id=pid, prefix=key_prefix(raw), hash=hash_key(raw),
                                name="support renderer"))
            s.add(models.RoleBinding(principal_id=pid, role="renderer",
                                     project_id="support", environment_id="prod"))
            renderer_key = raw
        else:
            renderer_key = "(already seeded)"

    return renderer_key


if __name__ == "__main__":
    key = seed()
    print("Seeded. Renderer key:", key)
