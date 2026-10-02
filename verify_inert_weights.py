#!/usr/bin/env python3
"""Regression proof: the new indicators must contribute EXACTLY 0.0 at their
default weight, so score() on frozen inputs is byte-identical to before.

Uses a frozen synthetic feature dict (no live data, no timing variance) so
this is a true before/after comparison, not noise from the market moving.
"""
import predictor

# Frozen features dict - the exact shape the OLD compute_features() produced
# (no session_sweep/dxy_roc24 keys), simulating an old stored features_json.
OLD_STYLE_FEATURES = {
    "close": 4160.0, "ema12": 4165.0, "ema26": 4162.0, "sma20": 4158.0,
    "rsi14": 58.0, "atr14": 12.5, "roc24": 0.42, "mom1": 0.03,
    "higher_high_6": True, "lower_low_6": False, "vol_vs_avg": 1.15,
    "atr_pct": 0.3, "bars": 200,
}

params = predictor.build_params()
score_old_style, contrib_old_style = predictor.score(OLD_STYLE_FEATURES, params)
print("=== old-style features (no new keys) ===")
print(f"  score: {score_old_style}")
print(f"  contrib: {contrib_old_style}")
assert "session_sweep" not in contrib_old_style, "must not fabricate a contribution with no data"
assert "dxy_proxy" not in contrib_old_style, "must not fabricate a contribution with no data"
print("  PASS: no phantom contributions when the new features are absent")

# New-style features WITH the new keys present but non-trivial values -
# weight is still 0.0, so contribution must be EXACTLY 0.0, not merely small.
NEW_STYLE_FEATURES = dict(OLD_STYLE_FEATURES)
NEW_STYLE_FEATURES["session_sweep"] = 1       # a real bullish sweep signal
NEW_STYLE_FEATURES["dxy_roc24"] = -0.8        # a real dollar-down reading

score_new_style, contrib_new_style = predictor.score(NEW_STYLE_FEATURES, params)
print("\n=== new-style features (keys present, weight=0.0) ===")
print(f"  score: {score_new_style}")
print(f"  contrib: {contrib_new_style}")

assert score_old_style == score_new_style, (
    f"INERT WEIGHT VIOLATION: score changed from {score_old_style} to "
    f"{score_new_style} even though session_sweep/dxy_proxy weights are 0.0")
print("  PASS: total score IDENTICAL with weight=0.0 despite real signal present")

assert contrib_new_style.get("session_sweep") == 0.0, "session_sweep contrib must be exactly 0.0"
assert contrib_new_style.get("dxy_proxy") == 0.0, "dxy_proxy contrib must be exactly 0.0"
print("  PASS: both new contributions are exactly 0.0, not just small")

for k in ("trend", "position", "momentum", "rsi", "breadth", "volume_confirm"):
    assert contrib_old_style[k] == contrib_new_style[k], f"{k} contribution shifted!"
print("  PASS: all 6 original contributions are byte-identical")

print("\nALL INERT-WEIGHT REGRESSION CHECKS PASS")
