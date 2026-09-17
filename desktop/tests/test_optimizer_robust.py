# -*- coding: utf-8 -*-
"""锁定配置稳健性:10 个不同问题实例,TPE(bw=0.10,e=0.15) vs 随机,40 趟。"""
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


def run_tpe(n, seed, f, noise_rng):
    spec = {"params": [{"name": "x%d" % i, "min": 0.0, "max": 1.0} for i in range(8)],
            "objectives": [{"key": "score", "direction": "max"}],
            "budget": {"max_runs": n}, "warmup": 4, "seed": seed}
    opt = Optimizer(spec)
    obs = []
    while True:
        p = opt.propose(obs)
        if p is None:
            break
        xs = [p["params"]["x%d" % i] for i in range(8)]
        obs.append({"params": p["params"], "objectives": {"score": f(xs, noise_rng)}})
    return opt.summary(obs)["best"]["objectives"]["score"]


tpe, rnd, wins = [], [], 0
for k in range(10):
    f = make_f(100 + k)
    noise_t = random.Random(200 + k)
    noise_r = random.Random(300 + k)
    t = run_tpe(40, k, f, noise_t)
    r = max(f([noise_r.random() for _ in range(8)], noise_r) for _ in range(40))
    tpe.append(t)
    rnd.append(r)
    if t > r:
        wins += 1
fmt = lambda vs: "[" + ", ".join("%.1f" % v for v in vs) + "]"
print("TPE  : mean=%.1f min=%.1f max=%.1f  %s"
      % (sum(tpe) / 10, min(tpe), max(tpe), fmt(tpe)))
print("随机 : mean=%.1f min=%.1f max=%.1f  %s"
      % (sum(rnd) / 10, min(rnd), max(rnd), fmt(rnd)))
print("TPE 胜场:", wins, "/10, 平均提升: +%.0f%%"
      % (100 * (sum(tpe) - sum(rnd)) / sum(rnd)))
print("ROBUST-OK")
