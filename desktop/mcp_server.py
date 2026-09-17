# -*- coding: utf-8 -*-
"""mcp_server.py - agent 调参工具契约(P4, 设计文档 §4)

最小 MCP server:stdio 上换行分隔 JSON-RPC 2.0,只实现 tools 能力子集
(initialize / tools/list / tools/call / ping)。stdlib-only,不引 mcp SDK。

薄壳原则:零业务逻辑——全部转发桥 HTTP / 读 runs.jsonl / 调 session_driver
子进程。FORBIDDEN 表是全链路第三处兜底(桥/driver 各自也拒)。

接入:agent 配置 stdio 启动 `python mcp_server.py`(桥需已在跑)。

工具:
  get_snapshot               链路+遥测+桥状态聚合(GET /snapshot)
  get_schema                 参数 schema(GET /schema, 含 schema_hash)
  set_params {commands[]}    直写(POST /batch expect ACK;FORBIDDEN 拒)
  propose_params {commands[], rationale?, expected?}  半自动主通道:
                             提交提案,人在 Web 确认后生效(POST /proposal)
  run_experiment {recipe_path?|sweep_path?|optimize_path?}
                             子进程跑 session_driver --do/--sweep/--optimize
                             --json,返回结构化结果(含 score)
  list_runs {limit?}         runs.jsonl 尾部 N 趟
  get_score {run_id?}        指定趟或最新趟的 score+breakdown
  diff_runs {run_a, run_b}  跨趟 diff(全局指标+段级, P0-3)
  list_audit {limit?}        审计日志尾部 N 条(P0-2)
P0-2 护栏: set_params/propose_params 预检(黑名单/值域/步长),违规写审计
"""
import json
import subprocess
import sys
import time
import urllib.request
import urllib.error
from pathlib import Path

BASE = "http://127.0.0.1:9898"
APP = Path(__file__).resolve().parent
RUNS_DB = APP / "recordings" / "runs.jsonl"
SERVER_NAME = "tcp_tool-tuning"
SERVER_VERSION = "2026-08-14.p0"
# 发车/录制类命令一律拒绝(铁律第三处兜底;schema 声明时以声明为准,P0-2)
FORBIDDEN = ("MISSION RETURN", "MISSION EVENT", "MISSION LEARN")

import guardrails as G  # P0-2 护栏引擎(通用核,stdlib-only)
try:  # profile 决议与桥同源
    import config_loader as CL
except ImportError:  # pragma: no cover
    CL = None


def schema_dict():
    """当前生效的 control_schema.json(profile 优先,回退 app_dir)。"""
    if CL is not None:
        p = CL.profile_schema_path()
        if p is not None:
            try:
                return json.loads(Path(p).read_text(encoding="utf-8"))
            except Exception:
                return None
    p = APP / "control_schema.json"
    try:
        return json.loads(Path(p).read_text(encoding="utf-8")) \
            if p.is_file() else None
    except Exception:
        return None


def guard_check(commands):
    """护栏预检(黑名单/值域/步长)。内置 FORBIDDEN 兜底 + schema 驱动。
    违规写审计。返回 (ok, result)。"""
    for c in commands:
        u = c.upper()
        for f in FORBIDDEN:
            if f in u:
                res = {"ok": False, "violations": [
                    {"rule": "forbidden", "cmd": c,
                     "detail": "matches builtin FORBIDDEN %s" % f}]}
                G.audit({"actor": "mcp", "action": "guard",
                         "commands": list(commands), "verdict": "rejected",
                         "role": "agent", "violations": res["violations"]},
                        app_dir=APP)
                return False, res
    res = G.check_commands([str(c) for c in commands], schema_dict())
    if not res["ok"]:
        G.audit({"actor": "mcp", "action": "guard",
                 "commands": [str(c) for c in commands], "verdict": "rejected",
                 "role": "agent", "violations": res["violations"]},
                app_dir=APP)
    return res["ok"], res


# ---------------------------------------------------------------- bridge io
def http_get(path, timeout=3.0):
    with urllib.request.urlopen(BASE + path, timeout=timeout) as r:
        return json.loads(r.read())


def http_post(path, payload, timeout=None):
    d = json.dumps(payload).encode()
    req = urllib.request.Request(BASE + path, data=d,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout or 30.0) as r:
        return json.loads(r.read())


# ---------------------------------------------------------------- tools
def tool_get_snapshot(_args):
    return http_get("/snapshot")


def tool_get_schema(_args):
    return http_get("/schema")


def tool_set_params(args):
    commands = args.get("commands")
    if not isinstance(commands, list) or not all(isinstance(c, str) for c in commands):
        raise ValueError("commands must be a string list")
    ok, res = guard_check(commands)
    if not ok:
        raise ValueError("guardrail violation: %s"
                         % json.dumps(res["violations"], ensure_ascii=False))
    return http_post("/batch", {
        "commands": [{"cmd": c, "expect": "ACK"} for c in commands],
        "stop_on_error": True, "role": "agent"},
        timeout=len(commands) * 3.0 + 10.0)


def tool_propose_params(args):
    commands = args.get("commands")
    if not isinstance(commands, list) or not all(isinstance(c, str) for c in commands):
        raise ValueError("commands must be a string list")
    ok, res = guard_check(commands)
    if not ok:
        raise ValueError("guardrail violation: %s"
                         % json.dumps(res["violations"], ensure_ascii=False))
    return http_post("/proposal", {
        "commands": commands,
        "rationale": args.get("rationale", ""),
        "expected": args.get("expected", "")})


def tool_run_experiment(args):
    recipe = args.get("recipe_path")
    sweep = args.get("sweep_path")
    optimize = args.get("optimize_path")
    modes = [m for m in (("--do", recipe), ("--sweep", sweep),
                         ("--optimize", optimize)) if m[1]]
    if len(modes) != 1:
        raise ValueError("exactly one of recipe_path / sweep_path / "
                         "optimize_path required")
    mode, path = modes[0]
    if not Path(path).is_file():
        raise ValueError(f"file not found: {path}")
    proc = subprocess.run(
        [sys.executable, str(APP / "session_driver.py"), mode, path, "--json"],
        capture_output=True, text=True, timeout=float(args.get("timeout_s", 900)))
    out = proc.stdout.strip().splitlines()
    result = json.loads(out[-1]) if out else {"error": "no output",
                                              "stderr": proc.stderr[-2000:]}
    result["driver_exit"] = proc.returncode
    return result


def tool_diff_runs(args):
    """跨趟 diff(P0-3): run_id 或 capture 文件路径,复用 session_driver --diff。"""
    run_a = args.get("run_a")
    run_b = args.get("run_b")
    if not run_a or not run_b:
        raise ValueError("run_a and run_b required (run_id or capture path)")

    def resolve(x):
        p = Path(str(x))
        if p.is_file():
            return str(p)
        for r in _read_runs():
            if r.get("run_id") == x and r.get("capture_file"):
                return r["capture_file"]
        raise ValueError(f"cannot resolve run: {x}")

    pa, pb = resolve(run_a), resolve(run_b)
    proc = subprocess.run(
        [sys.executable, str(APP / "session_driver.py"),
         "--diff", pa, pb, "--json"],
        capture_output=True, text=True, timeout=60)
    out = proc.stdout.strip().splitlines()
    result = json.loads(out[-1]) if out else {"error": "no output",
                                              "stderr": proc.stderr[-2000:]}
    result["driver_exit"] = proc.returncode
    return result


def tool_list_audit(args):
    """审计日志尾部 N 条(runtime/audit.jsonl)。"""
    limit = int(args.get("limit", 50))
    p = G.audit_path(APP)
    if not p.is_file():
        return {"count": 0, "events": []}
    lines = p.read_text(encoding="utf-8").splitlines()
    events = []
    for ln in lines[-limit:]:
        ln = ln.strip()
        if ln:
            try:
                events.append(json.loads(ln))
            except json.JSONDecodeError:
                continue
    return {"count": len(events), "events": events}


def _read_runs(limit=None):
    if not RUNS_DB.is_file():
        return []
    lines = RUNS_DB.read_text(encoding="utf-8").splitlines()
    runs = []
    for ln in lines:
        ln = ln.strip()
        if ln:
            try:
                runs.append(json.loads(ln))
            except json.JSONDecodeError:
                continue
    return runs[-limit:] if limit else runs


def tool_list_runs(args):
    limit = int(args.get("limit", 20))
    runs = _read_runs(limit)
    return {"count": len(runs), "runs": runs}


def tool_get_score(args):
    run_id = args.get("run_id")
    runs = _read_runs()
    if run_id:
        match = [r for r in runs if r.get("run_id") == run_id]
        if not match:
            raise ValueError(f"run_id not found: {run_id}")
        r = match[-1]
    elif runs:
        r = runs[-1]
    else:
        raise ValueError("no runs recorded yet")
    return {"run_id": r.get("run_id"), "label": r.get("label"),
            "sweep": r.get("sweep"), "score": r.get("score"),
            "metrics": r.get("metrics"), "skipped": r.get("skipped")}


TOOLS = [
    ("get_snapshot", "链路+遥测+桥状态聚合快照", {
        "type": "object", "properties": {}}),
    ("get_schema", "参数 schema(含 schema_hash 版本锚)", {
        "type": "object", "properties": {}}),
    ("set_params", "直写参数(ACK 验证;发车/录制类拒绝)。调参参数优先用 propose_params", {
        "type": "object", "required": ["commands"],
        "properties": {"commands": {"type": "array", "items": {"type": "string"}}}}),
    ("propose_params", "提交参数提案,人在 Web 确认后生效(半自动主通道)", {
        "type": "object", "required": ["commands"],
        "properties": {"commands": {"type": "array", "items": {"type": "string"}},
                       "rationale": {"type": "string"},
                       "expected": {"type": "string"}}}),
    ("run_experiment", "执行试验(recipe 单趟 / sweep 网格 / optimize TPE 贝叶斯),返回结构化结果含 score", {
        "type": "object",
        "properties": {"recipe_path": {"type": "string"},
                       "sweep_path": {"type": "string"},
                       "optimize_path": {"type": "string"},
                       "timeout_s": {"type": "number"}}}),
    ("list_runs", "趟次历史(runs.jsonl 尾部 N 趟)", {
        "type": "object",
        "properties": {"limit": {"type": "integer", "default": 20}}}),
    ("get_score", "指定趟(run_id)或最新趟的 score+breakdown", {
        "type": "object",
        "properties": {"run_id": {"type": "string"}}}),
    ("diff_runs", "两个趟次逐键 diff(全局指标+段级归因;run_id 或 capture 路径)", {
        "type": "object", "required": ["run_a", "run_b"],
        "properties": {"run_a": {"type": "string"},
                       "run_b": {"type": "string"}}}),
    ("list_audit", "安全审计日志尾部 N 条(护栏判定/回滚/设参, append-only)", {
        "type": "object",
        "properties": {"limit": {"type": "integer", "default": 50}}}),
]

TOOL_FUNCS = {
    "get_snapshot": tool_get_snapshot,
    "get_schema": tool_get_schema,
    "set_params": tool_set_params,
    "propose_params": tool_propose_params,
    "run_experiment": tool_run_experiment,
    "list_runs": tool_list_runs,
    "get_score": tool_get_score,
    "diff_runs": tool_diff_runs,
    "list_audit": tool_list_audit,
}


# ---------------------------------------------------------------- jsonrpc
def _result(rid, result):
    return {"jsonrpc": "2.0", "id": rid, "result": result}


def _error(rid, code, message):
    return {"jsonrpc": "2.0", "id": rid,
            "error": {"code": code, "message": message}}


def handle(req):
    method = req.get("method", "")
    rid = req.get("id")
    if method == "initialize":
        return _result(rid, {
            "protocolVersion": (req.get("params") or {}).get(
                "protocolVersion", "2024-11-05"),
            "capabilities": {"tools": {}},
            "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION}})
    if method == "ping":
        return _result(rid, {})
    if method == "tools/list":
        return _result(rid, {"tools": [
            {"name": n, "description": d, "inputSchema": s}
            for n, d, s in TOOLS]})
    if method == "tools/call":
        params = req.get("params") or {}
        name = params.get("name", "")
        func = TOOL_FUNCS.get(name)
        if func is None:
            return _error(rid, -32602, f"unknown tool: {name}")
        try:
            out = func(params.get("arguments") or {})
            return _result(rid, {"content": [{
                "type": "text",
                "text": json.dumps(out, ensure_ascii=False, default=str)}]})
        except urllib.error.URLError as e:
            return _result(rid, {"content": [{
                "type": "text", "text": f"bridge unreachable: {e}"}],
                "isError": True})
        except Exception as e:
            return _result(rid, {"content": [{
                "type": "text", "text": f"{type(e).__name__}: {e}"}],
                "isError": True})
    if method.startswith("notifications/"):
        return None  # 通知不应答
    if rid is None:
        return None
    return _error(rid, -32601, f"method not found: {method}")


def main():
    # stdout 只走协议帧;任何诊断走 stderr
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
        except json.JSONDecodeError:
            sys.stdout.write(json.dumps(
                _error(None, -32700, "parse error")) + "\n")
            sys.stdout.flush()
            continue
        resp = handle(req)
        if resp is not None:
            sys.stdout.write(json.dumps(resp, ensure_ascii=False) + "\n")
            sys.stdout.flush()


if __name__ == "__main__":
    main()
