# -*- coding: utf-8 -*-
"""optimizer 合成冒烟:TPE vs 网格 vs 随机 收敛对比 + 多目标冒烟。"""
import random
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from optimizer import Optimizer


def f(x1, x2):
    # 合成黑盒:真最优在 (0.75, 0.25),带噪声
    return 100.0 - ((x1 - 0.75) ** 2 * 400 + (x2 - 0.25) ** 2 * 900) \
        + random.uniform(-2, 2)


spec = {"params": [{"name": "p1", "min": 0.0, "max": 1.0},
                   {"name": "p2", "min": 0.0, "max": 1.0}],
        "objectives": [{"key": "score", "direction": "max"}],
        "budget": {"max_runs": 20}, "warmup": 4, "seed": 7}

opt = Optimizer(spec)
obs = []
props = []
while True:
    p = opt.propose(obs)
    if p is None:
        break
    props.append(p)
    v = f(p["params"]["p1"], p["params"]["p2"])
    obs.append({"params": p["params"], "objectives": {"score": v}})

best_tpe = opt.summary(obs)["best"]
print("TPE  20 趟 best:", round(best_tpe["objectives"]["score"], 1),
      "at", {k: round(v, 3) for k, v in best_tpe["params"].items()})

grid_scores = []
for i in range(5):
    for j in range(4):
        grid_scores.append(f((i + 0.5) / 5.0, (j + 0.5) / 4.0))
print("网格 20 趟 best:", round(max(grid_scores), 1))

rand_scores = [f(random.random(), random.random()) for _ in range(20)]
print("随机 20 趟 best:", round(max(rand_scores), 1))
print("TPE 提议 phase 序列:", [p["phase"] for p in props])
s = opt.summary(obs)
print("summary keys:", sorted(s.keys()))
print("budget:", s["budget"], "n_runs:", s["n_runs"],
      "pareto_front len:", len(s["pareto_front"]))

# 多目标冒烟
spec2 = {"params": [{"name": "p1", "min": 0.0, "max": 1.0}],
         "objectives": [{"key": "a", "direction": "max"},
                        {"key": "b", "direction": "min"}],
         "budget": {"max_runs": 8}, "warmup": 3, "seed": 1}
opt2 = Optimizer(spec2)
obs2 = []
while True:
    p2 = opt2.propose(obs2)
    if p2 is None:
        break
    x = p2["params"]["p1"]
    obs2.append({"params": p2["params"],
                 "objectives": {"a": x * 10, "b": (x - 0.5) ** 2}})
s2 = opt2.summary(obs2)
print("多目标 pareto_front len:", len(s2["pareto_front"]))
print("SMOKE-OK")
