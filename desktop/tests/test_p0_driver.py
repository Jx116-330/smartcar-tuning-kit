# -*- coding: utf-8 -*-
"""session_driver P0 集成测试:--diff 结构 / 护栏预检退出码 / audit。"""
import json
import math
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
fails = []


def expect(name, cond, detail=""):
    if cond:
        print("PASS", name)
    else:
        print("FAIL", name, detail)
        fails.append(name)


def synth_rows(seed=0.0, n=60):
    rows = []
    for i in range(n):
        rows.append({
            "te": round(i * 0.1, 2), "m_eng": 1,
            "m_idx": i, "m_pts": n,
            "m_str": math.sin(i / 4.0) * 10 + seed,
            "m_ff": math.sin(i / 8.0),
            "m_xte": 0.05 + 0.03 * math.sin(i / 3.0) + seed,
            "m_yer": 0.0, "m_spd": 1800,
            "m_cpx": i * 0.05, "m_cpy": 0.0,
            "m_dL": 3000.0, "m_dR": 3000.0,
            "m_st": 0, "m_lostsrc": 0,
        })
    return rows


def run_driver(args, timeout=60):
    p = subprocess.run([sys.executable, "session_driver.py"] + args,
                       cwd=ROOT, capture_output=True, text=True, timeout=timeout)
    return p


with tempfile.TemporaryDirectory() as td:
    td = Path(td)
    # ---- 1. --diff 合成捕获 ----
    ca = td / "a.json"
    cb = td / "b.json"
    ca.write_text(json.dumps({"label": "a", "sets": [], "rows": synth_rows(0.0)}),
                   encoding="utf-8")
    cb.write_text(json.dumps({"label": "b", "sets": [], "rows": synth_rows(0.02)}),
                   encoding="utf-8")
    p = run_driver(["--diff", str(ca), str(cb), "--json"])
    expect("--diff 退出码 0", p.returncode == 0, p.stderr[-300:])
    out = json.loads(p.stdout.strip().splitlines()[-1])
    expect("--diff 有全局 diffs", out.get("ok") and len(out.get("diffs", [])) > 0,
           str(out)[:300])
    expect("--diff 有段级 segment_diffs", len(out.get("segment_diffs", [])) == 10,
           str(out.get("segment_diffs"))[:300])
    expect("--diff 带 score 对比", "score" in out and out["score"]["a"] is not None,
           str(out.get("score"))[:300])
    expect("--diff xte delta 为正(0.02 偏移)",
           any(d["key"] == "xte_mean_m" and d["delta"] > 0 for d in out["diffs"]),
           str(out["diffs"])[:300])

    # ---- 2. --do 护栏预检(桥不在, 违规先于链路检查) ----
    rec = td / "bad_recipe.json"
    rec.write_text(json.dumps({"label": "bad", "set": ["SET kp -1"],
                               "streams": [], "report": False}),
                   encoding="utf-8")
    p = run_driver(["--profile", "virtual", "--do", str(rec), "--json"])
    expect("--do 违规 recipe 退出码 5", p.returncode == 5,
           "rc=%d out=%s" % (p.returncode, p.stdout[-300:]))
    out = json.loads(p.stdout.strip().splitlines()[-1])
    expect("--do 违规带 violations",
           out.get("error") == "guardrail violation"
           and out.get("violations"), str(out)[:300])

    # ---- 3. --do 合法但桥不在 -> 3 ----
    rec2 = td / "ok_recipe.json"
    rec2.write_text(json.dumps({"label": "ok", "set": ["SET kp 2.0"],
                                "streams": [], "report": False}),
                    encoding="utf-8")
    p = run_driver(["--profile", "virtual", "--do", str(rec2), "--json"])
    expect("--do 合法但桥不在 退出码 3", p.returncode == 3, str(p.returncode))

    # ---- 4. --optimize 非法 spec -> 4 ----
    badopt = td / "bad_opt.json"
    badopt.write_text(json.dumps({"params": [], "budget": {"max_runs": 5}}),
                      encoding="utf-8")
    p = run_driver(["--profile", "virtual", "--optimize", str(badopt), "--json"])
    expect("--optimize 非法 spec 退出码 4", p.returncode == 4, str(p.returncode))

    # ---- 5. --optimize 范围缺失(schema 没有该参数)-> 4 ----
    opt2 = td / "norange_opt.json"
    opt2.write_text(json.dumps({"label": "t",
                                "params": [{"name": "ghost",
                                            "template": "SET ghost {value}"}],
                                "budget": {"max_runs": 3}}),
                    encoding="utf-8")
    p = run_driver(["--profile", "virtual", "--optimize", str(opt2), "--json"])
    expect("--optimize 参数范围缺失 退出码 4", p.returncode == 4, str(p.returncode))

    # ---- 6. --optimize 违规命令 -> 5 ----
    opt3 = td / "forbidden_opt.json"
    opt3.write_text(json.dumps({"label": "t",
                                "params": [{"name": "kp",
                                            "template": "SET kp {value}",
                                            "min": -5, "max": 999}],
                                "budget": {"max_runs": 3}}),
                    encoding="utf-8")
    p = run_driver(["--profile", "virtual", "--optimize", str(opt3), "--json"])
    expect("--optimize 违规(值域超 schema)退出码 5", p.returncode == 5,
           "rc=%d out=%s" % (p.returncode, p.stdout[-300:]))

    # ---- 7. --optimize 合法但桥不在 -> 3 ----
    opt4 = td / "ok_opt.json"
    opt4.write_text(json.dumps({"label": "t",
                                "params": [{"name": "kp",
                                            "template": "SET kp {value}"}],
                                "budget": {"max_runs": 3}}),
                    encoding="utf-8")
    p = run_driver(["--profile", "virtual", "--optimize", str(opt4), "--json"])
    expect("--optimize 合法但桥不在 退出码 3", p.returncode == 3, str(p.returncode))

    # ---- 7b. v2: categorical spec 合法但桥不在 -> 3 ----',
    opt5 = td / "cat_opt.json"
    opt5.write_text(json.dumps({"label": "t",
                                "params": [
                                    {"name": "algo", "type": "categorical",
                                     "choices": ["A", "B"],
                                     "template": "SET algo {value}"},
                                    {"name": "kp",
                                     "template": "SET kp {value}"}],
                                "budget": {"max_runs": 3}}),
                    encoding="utf-8")
    p = run_driver(["--profile", "virtual", "--optimize", str(opt5), "--json"])
    expect("--optimize categorical 合法但桥不在 退出码 3", p.returncode == 3,
           "rc=%d out=%s" % (p.returncode, p.stdout[-300:]))

    # ---- 7c. v2: categorical choices 非法 -> 4 ----',
    opt6 = td / "badcat_opt.json"
    opt6.write_text(json.dumps({"label": "t",
                                "params": [
                                    {"name": "algo", "type": "categorical",
                                     "choices": ["A"],
                                     "template": "SET algo {value}"}],
                                "budget": {"max_runs": 3}}),
                    encoding="utf-8")
    p = run_driver(["--profile", "virtual", "--optimize", str(opt6), "--json"])
    expect("--optimize categorical choices<2 退出码 4", p.returncode == 4,
           str(p.returncode))

    # ---- 8. driver 审计: 违规预检已入 audit.jsonl ----
    ap = ROOT / "runtime" / "audit.jsonl"
    if ap.is_file():
        lines = ap.read_text(encoding="utf-8").splitlines()
        drv_rej = [ln for ln in lines
                   if "actor" in ln and "driver" in ln and "rejected" in ln]
        expect("driver 拒绝事件已审计", len(drv_rej) >= 1, "found %d" % len(drv_rej))
    else:
        expect("driver 拒绝事件已审计", False, "audit.jsonl 不存在")

print()
if fails:
    print("DRIVER-FAIL", len(fails), fails)
    sys.exit(1)
print("DRIVER-OK 全 PASS")
