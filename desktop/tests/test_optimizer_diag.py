# -*- coding: utf-8 -*-
"""诊断 TPE 收敛:打印提议轨迹,并做 40 趟多种子公平对比。"""
import random
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from optimizer import Optimizer


def f(x1, x2, rng):
    return 100.0 - ((x1 - 0.75) ** 2 * 400 + (x2 - 0.25) ** 2 * 900) \
        + rng.uniform(-2, 2)


def run_tpe(n, seed, trace=False):
    spec = {"params": [{"name": "p1", "min": 0.0, "max": 1.0},
                       {"name": "p2", "min": 0.0, "max": 1.0}],
            "objectives": [{"key": "score", "direction": "max"}],
            "budget": {"max_runs": n}, "warmup": 4, "seed": seed}
    opt = Optimizer(spec)
    rng = random.Random(seed + 1)
    obs = []
    while True:
        p = opt.propose(obs)
        if p is None:
            break
        v = f(p["params"]["p1"], p["params"]["p2"], rng)
        obs.append({"params": p["params"], "objectives": {"score": v}})
        if trace and len(obs) % 5 == 0:
            b = opt.summary(obs)["best"]["objectives"]["score"]
            pa = p["params"]
            print("  n=%2d best=%6.1f  prop=(%.2f,%.2f)"
                  % (len(obs), b, pa["p1"], pa["p2"]))
    return opt.summary(obs)["best"]["objectives"]["score"]


print("=== 40 趟 trace (seed=7) ===")
print("TPE best:", round(run_tpe(40, 7, trace=True), 1))

print("=== 多种子对比(各 5 seed, 40 趟) ===")
tpe = [run_tpe(40, s) for s in range(5)]
grid = []
rng = random.Random(99)
for _rep in range(5):
    gs = []
    for i in range(8):
        for j in range(5):
            gs.append(f((i + 0.5) / 8.0, (j + 0.5) / 5.0, rng))
    grid.append(max(gs))
rnd = []
for _rep in range(5):
    rnd.append(max(f(rng.random(), rng.random(), rng) for _ in range(40)))
fmt = lambda vs: "[" + ", ".join("%.1f" % v for v in vs) + "]"
print("TPE  40趟 mean=%.1f  %s" % (sum(tpe) / 5, fmt(tpe)))
print("网格 40趟 mean=%.1f  %s" % (sum(grid) / 5, fmt(grid)))
print("随机 40趟 mean=%.1f  %s" % (sum(rnd) / 5, fmt(rnd)))
print("DIAG-OK")
