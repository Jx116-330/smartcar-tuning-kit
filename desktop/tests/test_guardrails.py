# -*- coding: utf-8 -*-
"""guardrails 单元测试:值域/黑名单/步长/回滚判定/审计。"""
import json
import sys
import tempfile
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import guardrails as G

ROOT = Path(__file__).resolve().parent.parent
CAR = json.loads((ROOT / "control_schema.json").read_text(encoding="utf-8"))
VIRT = json.loads((ROOT / "profiles/virtual/control_schema.json")
                   .read_text(encoding="utf-8"))

fails = []


def expect(name, cond, detail=""):
    if cond:
        print("PASS", name)
    else:
        print("FAIL", name, detail)
        fails.append(name)


# 1. 值域(virtual)
r = G.check_commands(["SET kp -5"], VIRT)
expect("virtual 值域下限拒绝", not r["ok"] and r["violations"][0]["rule"] == "range", str(r))
r = G.check_commands(["SET kp 60"], VIRT)
expect("virtual 值域上限拒绝", not r["ok"] and r["violations"][0]["rule"] == "range", str(r))
r = G.check_commands(["SET kp 2.0"], VIRT)
expect("virtual 合法设参放行", r["ok"], str(r))

# 2. 黑名单(车)
r = G.check_commands(["MISSION RETURN"], CAR)
expect("车 发车命令黑名单拒绝", not r["ok"] and r["violations"][0]["rule"] == "forbidden", str(r))
r = G.check_commands(["mission return"], CAR)
expect("车 黑名单大小写不敏感", not r["ok"], str(r))

# 3. max_step(virtual, 带当前值)
r = G.check_commands(["SET kp 5.0"], VIRT, {"kp": 1.0})
expect("virtual 步长 4.0 放行(无 kp 限制)", r["ok"], str(r))
r = G.check_commands(["SET max_speed 250"], VIRT, {"max_speed": 100})
expect("virtual max_speed 步长 150>100 拒绝", not r["ok"] and r["violations"][0]["rule"] == "max_step", str(r))
r = G.check_commands(["SET max_speed 250"], VIRT, {"MAX_SPEED": 100})
expect("virtual 当前值键大小写兼容", not r["ok"], str(r))

# 4. 非设参命令只受黑名单管
r = G.check_commands(["RATE ctl 50", "GET kp"], VIRT)
expect("virtual 非设参命令放行", r["ok"], str(r))

# 5. 无 guardrails 声明的 schema -> guard_active=False 但可用
BARE = {"schema_hash": "bare", "fields": [{"id": "x", "min": 0, "max": 1}]}
r = G.check_commands(["SET x 0.5"], BARE)
expect("裸 schema 不拦(guard_active=False)", r["ok"] and not r["guard_active"], str(r))

# 6. 回滚判定
guard = G.load_schema_guard(VIRT)
t = G.check_metrics({"err": 0.1}, 20.0, guard)
expect("virtual score 20<30 触发回滚", any(x["rule"] == "score_min" for x in t), str(t))
t = G.check_metrics({"err": 0.1}, 60.0, guard)
expect("virtual score 60 不触发", not t, str(t))
guard_car = G.load_schema_guard(CAR)
t = G.check_metrics({"xte_mean_m": 0.4, "rev": 3}, 70.0, guard_car)
expect("车 xte 0.4>0.25 触发回滚", any(x["rule"] == "metric_max" for x in t), str(t))
t = G.check_metrics({"xte_mean_m": 0.05, "rev": 20}, 70.0, guard_car)
expect("车 rev 20>15 触发回滚", any(x["rule"] == "metric_max" for x in t), str(t))
t = G.check_metrics({"xte_mean_m": 0.05, "rev": 3}, 70.0, guard_car)
expect("车 健康指标不触发", not t, str(t))

# 7. 审计(临时目录,不污染真 runtime)
with tempfile.TemporaryDirectory() as td:
    p = G.audit({"actor": "test", "action": "batch", "verdict": "rejected",
                 "commands": ["SET kp -5"], "violations": [{"rule": "range"}]},
                app_dir=td)
    lines = Path(p).read_text(encoding="utf-8").splitlines()
    expect("audit 写入 1 行", len(lines) == 1, str(lines))
    rec = json.loads(lines[0])
    expect("audit 字段完整", rec["actor"] == "test" and rec["verdict"] == "rejected"
           and "ts" in rec and "ts_iso" in rec, str(rec))
    G.audit({"actor": "test", "action": "rollback", "verdict": "ok"}, app_dir=td)
    lines = Path(p).read_text(encoding="utf-8").splitlines()
    expect("audit append 2 行", len(lines) == 2, str(lines))

print()
if fails:
    print("GUARD-FAIL", len(fails), fails)
    sys.exit(1)
print("GUARD-OK 全 PASS")
