#!/usr/bin/env python3
"""
reward_budget.py
----------------
Given a total fund and a minimum reward, work out the MAXIMUM reward per sentence that the fund can
afford, using the real cost distribution of the dataset (output/sentences_for_supabase.csv).

    python reward_budget.py --budget 1600000 --min 400
    python reward_budget.py --budget 16000000 --min 400 --step 50

Two payout models are evaluated against the same fund (every reward is rounded to --step UGX):

  A. LINEAR  (recommended reading of "min for the simplest, max for the hardest")
        reward = min + (max - min) * cost            cost in [0, 1]
     Solved in closed form (then re-solved after rounding): the largest max that keeps total <= budget.

  B. FLOOR x RATE  (what the API implements today: reward_settings.rate_ugx / min_reward_ugx)
        reward = max(min, rate * cost)
     Solved by bisection for the largest rate that keeps total <= budget. Max reward = rate.

If budget < sentences * min the minimum cannot be funded at all; the script says so and shows
what the fund can afford instead.
"""
import argparse
import sys

import numpy as np
import pandas as pd


def rnd(x, step):
    return np.round(np.asarray(x, dtype=float) / step) * step


def linear(cost, budget, minimum, step):
    """Largest span such that sum(rnd(min + span*cost)) <= budget. Returns rewards or None if infeasible."""
    f = lambda span: rnd(minimum + span * cost, step).sum()
    if f(0.0) > budget:
        return None
    lo, hi = 0.0, float(budget)
    for _ in range(200):
        mid = (lo + hi) / 2
        lo, hi = (mid, hi) if f(mid) <= budget else (lo, mid)
    return rnd(minimum + lo * cost, step)


def floor_rate(cost, budget, minimum, step):
    """Largest rate such that sum(max(min, rnd(rate*cost))) <= budget."""
    f = lambda rate: np.maximum(minimum, rnd(rate * cost, step)).sum()
    if f(0.0) > budget:
        return None
    lo, hi = 0.0, float(budget)
    for _ in range(200):
        mid = (lo + hi) / 2
        lo, hi = (mid, hi) if f(mid) <= budget else (lo, mid)
    return np.maximum(minimum, rnd(lo * cost, step))


def describe(name, r, df, budget, minimum):
    total = r.sum()
    lo, hi = r.min(), r.max()
    print(f"\n{name}")
    print(f"  minimum reward ........ UGX {lo:>10,.0f}   ({int((r == lo).sum()):,} sentences at the minimum)")
    print(f"  MAXIMUM reward ........ UGX {hi:>10,.0f}   ({int((r == hi).sum()):,} sentences at the maximum)")
    print(f"  average / median ...... UGX {r.mean():>10,.0f} / {np.median(r):,.0f}")
    print(f"  90th / 99th percentile  UGX {np.percentile(r, 90):>10,.0f} / {np.percentile(r, 99):,.0f}")
    print(f"  total payout .......... UGX {total:>10,.0f}   (unspent: UGX {budget - total:,.0f})")
    print(f"  share of fund that is 'above the minimum': {1 - len(r) * lo / total:.1%}")
    by = pd.DataFrame({"d": df["difficulty"].values, "r": r}).groupby("d")["r"].agg(["count", "mean", "sum"]).reindex(["easy", "medium", "hard"])
    for d, row in by.iterrows():
        print(f"    {d:<6} {int(row['count']):>6,} sentences   avg UGX {row['mean']:>8,.0f}   {row['sum'] / total:>6.1%} of payout")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--budget", type=float, required=True, help="total fund in UGX")
    ap.add_argument("--min", type=float, required=True, dest="minimum", help="reward for the simplest sentence, UGX")
    ap.add_argument("--step", type=float, default=50, help="round every reward to a multiple of this many UGX (default 50)")
    ap.add_argument("--csv", default="output/sentences_for_supabase.csv")
    a = ap.parse_args()

    if a.minimum % a.step:
        print(f"Note: minimum {a.minimum:,.0f} is not a multiple of --step {a.step:,.0f}; rewards are rounded to the step, "
              f"so the effective minimum is {rnd(a.minimum, a.step):,.0f}. Pass --step 1 for exact amounts.")
    df = pd.read_csv(a.csv, usecols=["cost", "difficulty"])
    cost, n = df["cost"].to_numpy(float), len(df)
    print(f"Dataset: {n:,} sentences (cost mean {cost.mean():.3f}, median {np.median(cost):.3f}, "
          f"{(cost == 0).mean():.0%} at 0, {(cost == 1).mean():.1%} at the 1.0 cap, sum {cost.sum():,.1f})")
    print(f"Fund: UGX {a.budget:,.0f}  ->  average affordable reward UGX {a.budget / n:,.1f} per sentence")
    print(f"Minimum reward: UGX {a.minimum:,.0f}  ->  floor alone costs UGX {n * a.minimum:,.0f}")

    r_lin, r_fr = linear(cost, a.budget, a.minimum, a.step), floor_rate(cost, a.budget, a.minimum, a.step)
    if r_lin is None:
        print(f"\n*** NOT FEASIBLE: paying every sentence only the minimum already costs UGX {n * a.minimum:,.0f}, "
              f"which is more than the fund (short by UGX {n * a.minimum - a.budget:,.0f}). ***")
        print(f"    This fund can pay at most UGX {a.budget / n:,.0f} per sentence even if every sentence pays the same.")
        print(f"    To pay a UGX {a.minimum:,.0f} minimum the fund must be at least UGX {n * a.minimum:,.0f}, "
              f"and more than that to leave anything for harder sentences.")
    else:
        describe("A. LINEAR   reward = min + (max - min) x cost", r_lin, df, a.budget, a.minimum)
        describe("B. FLOOR x RATE   reward = max(min, rate x cost)   [current API formula]", r_fr, df, a.budget, a.minimum)

    # --- sensitivity: how the answer moves ---------------------------------------------------------------
    def max_linear(budget, minimum):
        r = linear(cost, budget, minimum, a.step)
        return None if r is None else r.max()
    print(f"\nWhat the fund (UGX {a.budget:,.0f}) can do at other minimums   [LINEAR model]")
    print(f"  {'min reward':>12}   {'max reward':>12}")
    for m in sorted({a.step * k for k in (1, 2, 3, 4, 6, 8, 10)} | {a.minimum}):
        mx = max_linear(a.budget, m)
        print(f"  {m:>12,.0f}   {('not affordable' if mx is None else f'{mx:,.0f}'):>12}")
    print(f"\nWhat a UGX {a.minimum:,.0f} minimum needs at other fund sizes   [LINEAR model]")
    print(f"  {'fund':>14}   {'avg / sentence':>14}   {'max reward':>12}")
    for b in sorted({1_600_000, 4_000_000, n * a.minimum, 8_000_000, 10_000_000, 12_000_000, 16_000_000, a.budget}):
        mx = max_linear(b, a.minimum)
        print(f"  {b:>14,.0f}   {b / n:>14,.0f}   {('not affordable' if mx is None else f'{mx:,.0f}'):>12}")


if __name__ == "__main__":
    sys.exit(main())
