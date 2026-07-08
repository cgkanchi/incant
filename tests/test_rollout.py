from collections import Counter

from incant.core import RolloutBand, bucket_point
from incant.core.rollout import pick_band


def test_bucket_point_is_deterministic():
    a = bucket_point("rule-1", "u_42", None)
    b = bucket_point("rule-1", "u_42", None)
    assert a == b
    assert 0.0 <= a < 1.0


def test_global_vs_prompt_scoped_bucketing_differ():
    g = bucket_point("rule-1", "u_42", None)
    p = bucket_point("rule-1", "u_42", "support/system")
    assert g != p  # prompt id enters the hash only for prompt-scoped rollouts


def test_global_bucketing_coherent_across_prompts():
    # Global rollout ignores prompt id, so the same user buckets identically
    # regardless of which prompt is being resolved.
    assert bucket_point("exp-142", "u_7", None) == bucket_point("exp-142", "u_7", None)


def test_weights_distribute_roughly():
    bands = (
        RolloutBand(weight=20, label="voice-v2"),
        RolloutBand(weight=80, is_default=True),
    )
    counts = Counter()
    for i in range(20000):
        band = pick_band(bands, "exp-142", f"u_{i}", None)
        counts["v2" if band.label == "voice-v2" else "default"] += 1
    frac = counts["v2"] / 20000
    assert 0.18 < frac < 0.22  # ~20%


def test_ramp_is_monotonic():
    # Users in the 10% cohort stay in when the ramp widens to 30%.
    def cohort(width):
        bands = (RolloutBand(weight=width, label="v2"), RolloutBand(weight=100 - width, is_default=True))
        return {u for u in range(3000) if pick_band(bands, "r", f"u_{u}", None).label == "v2"}

    assert cohort(10) <= cohort(30) <= cohort(60)


def test_zero_weights_returns_none():
    assert pick_band((RolloutBand(weight=0),), "r", "u", None) is None
