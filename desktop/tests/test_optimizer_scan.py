# -*- coding: utf-8 -*-
"""TPE 带宽真扫描(修复后):固定问题实例跨配置可比。"""
import random
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from optimizer import Optimizer


def make_f(seed):
    rng = random.Random(seed)
    center = [rng.random() for _ in range(8)]
    scale = [1.0, 2.0, 0.5, 1.5, 3.0, 0.8, 1.2, 2.5]
    def f(xs, r):
        loss = sum(s * (xs[i] - center[i]) ** 2 * 100 for i, s in enumerate(scale))
        return 100.0 - loss + r.uniform(-3, 3)
    return f


def run(n, seed, f, bw, n_cand, warmup, explore, noise_rng):
    spec = {"params": [{"name": "x%d" % i, "min": 0.0, "max": 1.0} for i in range(8)],
            "objectives": [{"key": "score", "direction": "max"}],
            "budget": {"max_runs": n}, "warmup": warmup, "seed": seed}
    opt = Optimizer(spec)
    opt._n_candidates = n_cand
    opt._explore_prob = explore
    opt._bw_base = bw
    obs = []
    while True:
        p = opt.propose(obs)
        if p is None:
            break
        xs = [p["params"]["x%d" % i] for i in range(8)]
        obs.append({"params": p["params"], "objectives": {"score": f(xs, noise_rng)}})
    return opt.summary(obs)["best"]["objectives"]["score"]


print("扫描开始")
from collections import defaultdict
agg = defaultdict(list)
for f_seed in (11, 22, 33):
    f = make_f(f_seed)
    for bw in (0.05, 0.1, 0.25, 0.5, 1.0):
        for explore in (0.15, 0.3):
            for seed in (1, 2, 3):
                noise_rng = random.Random(f_seed * 100 + seed)
                best = run(40, seed, f, bw, 1500, 4, explore, noise_rng)
                agg[(bw, explore)].append(best)
print("bw  | explore | mean  | min   | max")
for key in sorted(agg):
    vs = agg[key]
    print("%.2f |   %.2f  | %5.1f | %5.1f | %5.1f"
          % (key[0], key[1], sum(vs) / len(vs), min(vs), max(vs)))
print("SCAN-OK")
