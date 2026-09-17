# -*- coding: utf-8 -*-
"""mcp_server P0 冒烟:工具注册 / 护栏预检拒绝 / 审计。直接函数级调用。"""
import json
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import mcp_server as M

# 测试决议到 virtual profile(否则回退车 schema,不认识 kp 参数)
if M.CL is not None:
    M.CL.PROFILE_NAME = "virtual"

fails = []


def expect(name, cond, detail=""):
    if cond:
        print("PASS", name)
    else:
        print("FAIL", name, detail)
        fails.append(name)


req = {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}}
resp = M.handle(req)
names = [t["name"] for t in resp["result"]["tools"]]
expect("tools/list 含 diff_runs/list_audit",
       "diff_runs" in names and "list_audit" in names, str(names))
idx = names.index("run_experiment")
schema = resp["result"]["tools"][idx]["inputSchema"]
expect("run_experiment 含 optimize_path", "optimize_path" in str(schema),
       str(schema))

ok, res = M.guard_check(["SET kp 100"])
expect("mcp guard_check 值域拒绝", not ok
       and res["violations"][0]["rule"] == "range", str(res))

ok, res = M.guard_check(["MISSION RETURN"])
expect("mcp guard_check 铁律兜底", not ok
       and res["violations"][0]["rule"] == "forbidden", str(res))

err = None
try:
    M.tool_set_params({"commands": ["SET kp -1"]})
except ValueError as e:
    err = str(e)
expect("tool_set_params 违规抛错", err is not None and "guardrail" in err,
       str(err))

err = None
try:
    M.tool_propose_params({"commands": ["SET kp -1"]})
except ValueError as e:
    err = str(e)
expect("tool_propose_params 违规抛错",
       err is not None and "guardrail" in err, str(err))

err = None
try:
    M.tool_run_experiment({})
except ValueError as e:
    err = str(e)
expect("run_experiment 空参抛错", err is not None and "exactly one" in err,
       str(err))

out = M.tool_list_audit({"limit": 5})
expect("tool_list_audit 返回结构",
       "count" in out and "events" in out, str(out)[:200])

print()
if fails:
    print("MCP-FAIL", len(fails), fails)
    sys.exit(1)
print("MCP-OK 全 PASS")
