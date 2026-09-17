# -*- coding: utf-8 -*-
"""P0 全量回归入口:tests/run_p0_checks.py

依次跑:
  1.  test_optimizer_smoke.py    优化器合成冒烟(2维+多目标)
  2.  test_optimizer_robust.py   10 实例稳健性(TPE vs 随机, 8维40趟)
  3.  test_optimizer_v2.py       优化器 v2(参数类型声明/穷举路由/init)
  4.  test_guardrails.py         护栏引擎单元(值域/黑名单/步长/回滚/审计)
  5.  test_score_segments.py     分段归因单元
  6.  test_p0_driver.py          driver 集成(--diff/退出码/审计)
  7.  test_p0_mcp.py             MCP 工具面冒烟
  8.  test_p0_e2e.py             桥内护栏强制 e2e(虚拟设备+桥,--headless)
  9.  test_bridge_ext.py         桥扩展机制 + headless 全矩阵(2026-09-14)
  10. test_frozen_headless.py    冻结态双 exe headless 验收(缺产物 SKIP)

源码套件失败 → 退出码 1。冻结套件 SKIP 如实分级汇总(不报成 PASS);
发布验收:build.bat 后 `python tests/test_frozen_headless.py --require`。
"""
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
SUITES = [
    "test_optimizer_smoke.py",
    "test_optimizer_robust.py",
    "test_optimizer_v2.py",
    "test_guardrails.py",
    "test_score_segments.py",
    "test_p0_driver.py",
    "test_p0_mcp.py",
    "test_p0_e2e.py",
    "test_bridge_ext.py",
    "test_frozen_headless.py",
]

failed = []
frozen_skipped = []
for name in SUITES:
    print("=" * 60)
    print("SUITE:", name)
    print("=" * 60)
    p = subprocess.run([sys.executable, str(ROOT / name)],
                       cwd=ROOT.parent, capture_output=True, text=True,
                       timeout=600)
    sys.stdout.write(p.stdout)
    if p.returncode != 0:
        failed.append(name)
        if p.stderr:
            print("--- stderr tail ---")
            print(p.stderr[-800:])
    if name == "test_frozen_headless.py":
        m = re.search(r"FROZEN-RESULT: \w+ skipped=(\[[^\]]*\])", p.stdout)
        if m:
            try:
                frozen_skipped = [s for s in __import__("json").loads(m.group(1)) if s]
            except ValueError:
                frozen_skipped = ["<parse-error>"]
    print()

if failed:
    print("P0-CHECKS-FAIL:", failed)
    sys.exit(1)
if frozen_skipped:
    print("P0-CHECKS-OK(源码测试): %d 套件 PASS" % (len(SUITES) - 1))
    print("冻结态 SKIP(产物缺失/过期):", frozen_skipped)
    print("发布验收:build.bat 后运行 python tests/test_frozen_headless.py --require")
    sys.exit(0)
print("P0-CHECKS-OK: 全部 %d 个套件 PASS(含冻结态双 exe)" % len(SUITES))
