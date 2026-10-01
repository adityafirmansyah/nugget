#!/usr/bin/env python3
"""Independently verify the challenger's significance objection.

Challenger claims: (a) best rule 51.35% @ n=229 -> z=0.41; confluence 50.69%
@ n=1000 -> z=0.44; (b) picking best of 8 rules under pure noise yields ~52%
by max-of-8 order statistics, so the observed max is a multiple-comparisons
artifact.
"""
import json, math, random, statistics

def z(p, n, p0=0.5):
    se = math.sqrt(p0 * (1 - p0) / n)
    return (p - p0) / se

def boot_ci(wins, n, iters=20000, seed=7):
    rnd = random.Random(seed)
    p = wins / n
    out = []
    for _ in range(iters):
        out.append(sum(1 for _ in range(n) if rnd.random() < p) / n)
    out.sort()
    return out[int(0.025 * iters)], out[int(0.975 * iters)]

print("=== A. verify challenger's z-scores ===")
for name, p, n in [("RSI mean-revert (best of 8)", 0.5135, 229),
                   ("EMA+RSI confluence", 0.5069, 1000)]:
    lo, hi = boot_ci(round(p * n), n)
    print(f"{name:<30} p={p*100:.2f}% n={n:<5} z={z(p,n):+.2f}  "
          f"boot95%CI=[{lo*100:.2f}%, {hi*100:.2f}%]  "
          f"excludes50={'YES' if lo>0.5 or hi<0.5 else 'NO'}")

print("\n=== B. max-of-8 null simulation (is 50.69% just luck from testing 8?) ===")
rnd = random.Random(11)
TRADES_PER_RULE = 1000
sims = 20000
maxes = []
for _ in range(sims):
    best = 0.0
    for _r in range(8):
        w = sum(1 for _ in range(TRADES_PER_RULE) if rnd.random() < 0.5)
        best = max(best, w / TRADES_PER_RULE)
    maxes.append(best)
maxes.sort()
print(f"  under PURE NOISE, max of 8 rules @n=1000:")
print(f"    median = {maxes[sims//2]*100:.2f}%")
print(f"    95th pct = {maxes[int(0.95*sims)]*100:.2f}%")
print(f"    frac of null sims >= observed 50.69%: "
      f"{sum(1 for m in maxes if m >= 0.5069)/sims*100:.1f}%")
print(f"    -> p-value for 'best of 8' observed result = "
      f"{sum(1 for m in maxes if m >= 0.5069)/sims:.3f}")

print("\n=== C. what n would be needed to detect a real 1.5pp edge? ===")
for edge in (0.51, 0.52, 0.55):
    n_need = math.ceil((1.96 ** 2 * 0.25) / (edge - 0.5) ** 2)
    print(f"  true edge {edge*100:.0f}% -> n={n_need} graded bars needed "
          f"(~{n_need/24:.0f} days @24 bars/day, before any filtering)")

print("\n=== D. challenger's PAXG redemption claim check ===")
print("  claimed: 1:1 allocated, monthly audit, min redemption 430 oz,")
print("           settlement up to several business days -> peg is slow-arb")
print("  source status: challenger cites Paxos docs + Oct 2025 attestation.")
print("  -> verify independently before relying on it (see web check)")
