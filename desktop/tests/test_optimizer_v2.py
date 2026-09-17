# -*- coding: utf-8 -*-
"""optimizer v2 测试:混合空间收敛 / 穷举路由 / init / 分类-连续混合。"""
import random
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from optimizer import Optimizer

fails = []


def expect(name, cond, detail=""):
    if cond:
        print("PASS", name)
    else:
        print("FAIL", name, detail)
        fails.append(name)


# ---- 1. init: 首趟 = 声明组合, 后续正常优化 ----
spec1 = {"params": [
    {"name": "p1", "min": 0.0, "max": 1.0, "init": 0.3},
    {"name": "p2", "min": 0.0, "max": 1.0, "init": 0.7}],
    "objectives": [{"key": "s", "direction": "max"}],
    "budget": {"max_runs": 3}, "warmup": 2, "seed": 1}
opt1 = Optimizer(spec1)
p = opt1.propose([])
expect("init 首趟返回声明组合", p["phase"] == "init"
       and p["params"] == {"p1": 0.3, "p2": 0.7}, str(p))
obs1 = [{"params": p["params"], "objectives": {"s": 50.0}}]
p2 = opt1.propose(obs1)
expect("init 后进入 warmup", p2["phase"] == "warmup", str(p2))

# ---- 2. 穷举路由: 2x3 有限空间 -> 6 趟穷举 ----
spec2 = {"params": [
    {"name": "m", "type": "categorical", "choices": ["A", "B"]},
    {"name": "v", "min": 0.0, "max": 2.0, "step": 1.0}],
    "objectives": [{"key": "s", "direction": "max"}],
    "budget": {"max_runs": 10}, "seed": 5}
opt2 = Optimizer(spec2)
expect("穷举路由生效", opt2._mode == "exhaust" and opt2._total_exhaust == 6,
       str(opt2._mode))
obs2 = []
seen = set()
while True:
    q = opt2.propose(obs2)
    if q is None:
        break
    key = tuple(sorted(q["params"].items()))
    seen.add(key)
    obs2.append({"params": q["params"], "objectives": {"s": 1.0}})
expect("穷举 6 趟无重复", len(seen) == 6, str(len(seen)))
expect("穷举后预算内返回 None(空间耗尽)", len(obs2) == 6)

# ---- 3. 混合空间收敛: 真值在类别 C 上, TPE 应锁定 C ----
spec3 = {"params": [
    {"name": "algo", "type": "categorical", "choices": ["A", "B", "C"]},
    {"name": "kp", "min": 0.0, "max": 1.0}],
    "objectives": [{"key": "s", "direction": "max"}],
    "budget": {"max_runs": 30}, "warmup": 6, "seed": 3}
opt3 = Optimizer(spec3)
rng = random.Random(42)


def f3(params):
    base = 0.0 if params["algo"] == "C" else -40.0
    return base + 100.0 - (params["kp"] - 0.75) ** 2 * 400 + rng.uniform(-1, 1)


obs3 = []
while True:
    q = opt3.propose(obs3)
    if q is None:
        break
    obs3.append({"params": q["params"], "objectives": {"s": f3(q["params"])}})
s3 = opt3.summary(obs3)
best_algo = s3["best"]["params"]["algo"]
best_kp = s3["best"]["params"]["kp"]
expect("混合空间锁定正确类别 C", best_algo == "C", str(best_algo))
expect("混合空间连续维收敛(0.75±0.25)", abs(best_kp - 0.75) < 0.25, str(best_kp))
expect("混合空间 best score > 70", s3["best"]["objectives"]["s"] > 70,
       str(s3["best"]["objectives"]["s"]))

# ---- 4. 非法声明拒收 ----
bad_specs = [
    {"params": [{"name": "x", "type": "categorical", "choices": ["A"]}],
     "objectives": [{"key": "s"}]},
    {"params": [{"name": "x", "type": "categorical", "choices": ["A", "B"],
                 "init": "Z"}], "objectives": [{"key": "s"}]},
    {"params": [{"name": "x", "type": "weird"}], "objectives": [{"key": "s"}]},
    {"params": [{"name": "x", "min": 0, "max": 1, "init": 5.0}],
     "objectives": [{"key": "s"}]},
]
for i, bs in enumerate(bad_specs):
    err = None
    try:
        Optimizer(bs)
    except ValueError as e:
        err = str(e)
    expect("非法声明 %d 拒收" % i, err is not None, str(err))

print()
if fails:
    print("V2-FAIL", len(fails), fails)
    sys.exit(1)
print("V2-OK 全 PASS")
