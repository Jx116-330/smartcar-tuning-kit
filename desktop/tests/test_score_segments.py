# -*- coding: utf-8 -*-
"""score_engine 分段归因单元测试(P0-3)。"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from score_engine import load_profile, compute_score

fails = []


def expect(name, cond, detail=""):
    if cond:
        print("PASS", name)
    else:
        print("FAIL", name, detail)
        fails.append(name)


prof = load_profile("score_profile.json")
expect("profile 加载", prof is not None)

# 1. 无 segments 的旧口径(回归)
m = {"xte_mean_m": 0.08, "xte_max_m": 0.2, "rev": 5, "dstr_deg_per_frame": 1.0,
     "duty_sat_pct": 10, "gnd_mean_ms": 1.2}
s = compute_score(m, prof)
expect("旧口径回归: score 数值", s is not None and 0 <= s["score"] <= 100, str(s))

# 2. per_segment 声明生效: 分段值参与聚合
prof2 = {"metrics": [
    {"key": "xte_mean_m", "weight": 1.0, "direction": "lower", "ref": 0.1,
     "per_segment": True, "segment_aggregation": "mean", "pass_max": 0.15},
    {"key": "rev", "weight": 1.0, "direction": "lower", "ref": 6.0}],
    "missing_metric": "fail"}
m2 = {"xte_mean_m": 0.05,  # 全局值(将被分段聚合覆盖)
      "rev": 5,
      "segments": {"count": 3, "metrics": [
          {"seg": 0, "xte_mean_m": 0.02},
          {"seg": 1, "xte_mean_m": 0.08},
          {"seg": 2, "xte_mean_m": 0.20}]}}
s2 = compute_score(m2, prof2)
b = {x["key"]: x for x in s2["breakdown"]}
x = b["xte_mean_m"]
expect("分段均值聚合(mean): value=0.10",
       abs(x["contrib"] - round(1 - 0.10 / 0.1, 4)) < 0.02, str(x))
expect("归因字段: segment_values + worst_segment",
       "segment_values" in x and "worst_segment" in x, str(x))
expect("worst_segment 指向最差段(idx 2)", x.get("worst_segment") == 2, str(x))

# 3. worst 聚合
prof3 = {"metrics": [
    {"key": "xte_mean_m", "weight": 1.0, "direction": "lower", "ref": 0.1,
     "per_segment": True, "segment_aggregation": "worst", "pass_max": 0.15}],
    "missing_metric": "fail"}
s3 = compute_score(m2, prof3)
x3 = s3["breakdown"][0]
expect("worst 聚合取最大段 0.20 -> contrib 0", x3["contrib"] == 0.0, str(x3))
expect("worst 聚合 pass=false(0.20>0.15)", s3["pass"] is False, str(s3))

# 4. 分段声明但段值缺失 -> 回退全局
m4 = {"xte_mean_m": 0.05, "rev": 5}
s4 = compute_score(m4, prof2)
x4 = {i["key"]: i for i in s4["breakdown"]}["xte_mean_m"]
expect("段缺失回退全局值 0.05",
       x4.get("contrib") == round(1 - 0.05 / 0.1, 4), str(x4))

# 5. higher 方向的 worst 聚合 = 最小段
prof5 = {"metrics": [
    {"key": "gnd", "weight": 1.0, "direction": "higher", "ref": 2.0,
     "per_segment": True, "segment_aggregation": "worst"}],
    "missing_metric": "fail"}
m5 = {"segments": {"count": 3, "metrics": [
      {"seg": 0, "gnd": 2.0}, {"seg": 1, "gnd": 1.0}, {"seg": 2, "gnd": 1.5}]}}
s5 = compute_score(m5, prof5)
x5 = s5["breakdown"][0]
expect("higher+worst 取最小段 1.0 -> contrib 0.5",
       x5["contrib"] == 0.5 and x5["worst_segment"] == 1, str(x5))

print()
if fails:
    print("SCORE-FAIL", len(fails), fails)
    sys.exit(1)
print("SCORE-OK 全 PASS")
