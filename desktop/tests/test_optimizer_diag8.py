# -*- coding: utf-8 -*-
"""8 维高维 benchmark:TPE vs 随机 vs 网格(超预算)。高维是真实调参场景。"""
import random
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from optimizer import Optimizer


def make_f(center, rng):
    """8 维二次形,最优在 center,坐标尺度各异,噪声 ±3。"""
    scale = [1.0, 2.0, 0.5, 1.5, 3.0, 0.8, 1.2, 2.5]
    def f(xs):
        loss = sum(s * (xs[i] - center[i]) ** 2 * 100 for i, s in enumerate(scale))
        return 100.0 - loss + rng.uniform(-3, 3)
    return f


def run_tpe(n, seed, f):
    spec = {"params": [{"name": "x%d" % i, "min": 0.0, "max": 1.0} for i in range(8)],
            "objectives": [{"key": "score", "direction": "max"}],
            "budget": {"max_runs": n}, "warmup": 6, "seed": seed}
    opt = Optimizer(spec)
    obs = []
    while True:
        p = opt.propose(obs)
        if p is None:
            break
        xs = [p["params"]["x%d" % i] for i in range(8)]
        obs.append({"params": p["params"], "objectives": {"score": f(xs)}})
    return opt.summary(obs)["best"]["objectives"]["score"]


print("=== 8 维, 40 趟, 5 seed ===")
tpe, rnd = [], []
for seed in range(5):
    rng = random.Random(seed * 7 + 1)
    center = [rng.random() for _ in range(8)]
    f = make_f(center, rng)
    tpe.append(run_tpe(40, seed, f))
    rng2 = random.Random(seed * 13 + 5)
    rnd.append(max(f([rng2.random() for _ in range(8)]) for _ in range(40)))
fmt = lambda vs: "[" + ", ".join("%.1f" % v for v in vs) + "]"
print("TPE  8维40趟 mean=%.1f  %s" % (sum(tpe) / 5, fmt(tpe)))
print("随机 8维40趟 mean=%.1f  %s" % (sum(rnd) / 5, fmt(rnd)))
# 网格: 每维 2 点 = 256 趟, 超出 40 趟预算 6.4 倍
print("网格 8维 2点/维 = 256 趟(超 40 趟预算 6.4 倍, 不可用)")
print("DIAG8-OK")
