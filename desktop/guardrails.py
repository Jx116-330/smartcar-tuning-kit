# -*- coding: utf-8 -*-
"""guardrails.py - 通用安全护栏引擎 + append-only 审计(2026-08-14, P0-2, 设计文档 §2)

通用核:零被调对象语义。护栏规则全部来自 profile 的 control_schema.json 顶层键
"guardrails"(与 fields 同源,schema_hash 版本锚天然覆盖)。本模块只认配置:
  - set_patterns:        设参命令正则模板(只认 {param}/{value}/{p} 占位)
  - forbidden_commands:  全链路黑名单(大小写不敏感子串匹配)
  - max_step:            单次写入相对当前值的最大变化量(需 current_values)
  - fields[].min/max:    参数合法域(复用现有 schema)
  - rollback:            跑完触发回滚的条件(score 下限 / metrics 键值比较)

审计:append-only runtime/audit.jsonl(每行一个 JSON 事件),三处调用方共用。

  from guardrails import load_schema_guard, check_commands, check_metrics, audit
"""
import json
import re
import time
from pathlib import Path


# ---------------------------------------------------------------- schema 解析
def load_schema_guard(schema_dict):
    """从 control_schema.json 解析护栏配置;任何非法返回 None(调用方降级为
    仅黑名单模式)。schema_dict 为 None 或非法时返回 None。"""
    if not isinstance(schema_dict, dict):
        return None
    fields = schema_dict.get("fields", [])
    if not isinstance(fields, list):
        return None
    fmap = {}
    for f in fields:
        if not isinstance(f, dict) or not isinstance(f.get("id"), str):
            return None
        e = {"id": f["id"]}
        for k in ("min", "max"):
            v = f.get(k)
            if v is not None:
                if not isinstance(v, (int, float)) or isinstance(v, bool):
                    return None
                e[k] = float(v)
        fmap[f["id"]] = e
        fmap[f["id"].lower()] = e
    if "guardrails" not in schema_dict:
        return None  # 未声明护栏 = 护栏不激活(调用方黑名单兜底)
    guard = schema_dict.get("guardrails", {}) or {}
    if not isinstance(guard, dict):
        return None
    # set_patterns: 字符串列表 -> 编译正则(参数组名 param/value/p)
    patterns = guard.get("set_patterns", [])
    if not isinstance(patterns, list):
        return None
    compiled = []
    for pat in patterns:
        if not isinstance(pat, str) or "{param}" not in pat or "{value}" not in pat:
            return None
        rx = re.escape(pat)
        rx = rx.replace("\\{param\\}", "(?P<param>[A-Za-z_][A-Za-z0-9_]*)")
        rx = rx.replace("\\{value\\}", "(?P<value>-?[0-9]+(?:\\.[0-9]+)?)")
        rx = rx.replace("\\{p\\}", "(?P<p>[0-9]+)")
        try:
            compiled.append(re.compile("^" + rx + "$", re.IGNORECASE))
        except re.error:
            return None
    forbidden = guard.get("forbidden_commands", [])
    if not isinstance(forbidden, list) or not all(isinstance(f, str) for f in forbidden):
        return None
    # max_step: {default?, per_param?}
    ms = guard.get("max_step", {}) or {}
    if not isinstance(ms, dict):
        return None
    ms_default = ms.get("default")
    if ms_default is not None:
        ok_t = (isinstance(ms_default, (int, float))
                and not isinstance(ms_default, bool) and ms_default > 0)
        if not ok_t:
            return None
        ms_default = float(ms_default)
    ms_per = ms.get("per_param", {}) or {}
    if not isinstance(ms_per, dict):
        return None
    ms_map = {}
    for k, v in ms_per.items():
        ok_t = (isinstance(k, str) and isinstance(v, (int, float))
                and not isinstance(v, bool) and v > 0)
        if not ok_t:
            return None
        ms_map[k] = float(v)
        ms_map[k.lower()] = float(v)
    # rollback: {on_apply_fail?, score_min?, metrics?: [{key, min|max}]}
    rb = guard.get("rollback", {}) or {}
    if not isinstance(rb, dict):
        return None
    rb_score = rb.get("score_min")
    if rb_score is not None:
        if not isinstance(rb_score, (int, float)) or isinstance(rb_score, bool):
            return None
        rb_score = float(rb_score)
    rb_metrics = rb.get("metrics", []) or []
    rb_checked = []
    if not isinstance(rb_metrics, list):
        return None
    for m in rb_metrics:
        if (not isinstance(m, dict) or not isinstance(m.get("key"), str)
                or ("min" not in m and "max" not in m)):
            return None
        e = {"key": m["key"]}
        for k in ("min", "max"):
            if k in m:
                if not isinstance(m[k], (int, float)) or isinstance(m[k], bool):
                    return None
                e[k] = float(m[k])
        rb_checked.append(e)
    # restore_pattern: 回滚时反向渲染设参命令的模板(缺省 = 首个 set_pattern)
    rp = guard.get("restore_pattern", patterns[0] if patterns else None)
    if rp is not None:
        if (not isinstance(rp, str) or "{param}" not in rp or "{value}" not in rp):
            return None
    return {"fields": fmap, "patterns": compiled,
            "patterns_raw": [str(p) for p in patterns],
            "restore_pattern": rp,
            "forbidden": [f.upper() for f in forbidden],
            "max_step_default": ms_default, "max_step": ms_map,
            "rollback_on_apply_fail": bool(rb.get("on_apply_fail", False)),
            "rollback_score_min": rb_score,
            "rollback_metrics": rb_checked}


# ---------------------------------------------------------------- 命令检查
def _check_one(cmd, guard, current_values):
    """单条命令检查 -> {"cmd", "status", "violations", "param", "value"}。"""
    out = {"cmd": cmd, "status": "ok", "violations": []}
    u = cmd.upper()
    if guard is None:
        return out
    # 黑名单(大小写不敏感子串)
    for f in guard["forbidden"]:
        if f in u:
            out["violations"].append({"rule": "forbidden",
                                      "detail": "matches forbidden command %s" % f})
            out["status"] = "violation"
            break
    # 设参解析:匹配 set_patterns 才做值域/步长
    param, value = None, None
    for rx in guard["patterns"]:
        m = rx.match(cmd)
        if m:
            param = m.group("param").lower()
            try:
                value = float(m.group("value"))
            except ValueError:
                param = None
            break
    if param is None:
        return out  # 非设参命令,只受黑名单管
    out["param"] = param
    out["value"] = value
    f = guard["fields"].get(param)
    if f is not None:
        if "min" in f and value < f["min"]:
            out["violations"].append({"rule": "range", "param": param,
                                      "detail": "value %s below min %s"
                                      % (value, f["min"])})
        if "max" in f and value > f["max"]:
            out["violations"].append({"rule": "range", "param": param,
                                      "detail": "value %s above max %s"
                                      % (value, f["max"])})
    # max_step(需要当前值;缺失跳过,由有当前值的那层强制)
    if current_values:
        cur = current_values.get(param)
        if cur is None:
            for k, v in current_values.items():
                if isinstance(k, str) and k.lower() == param:
                    cur = v
                    break
        if cur is not None and isinstance(cur, (int, float)):
            limit = guard["max_step"].get(param, guard["max_step_default"])
            if limit is not None and abs(value - float(cur)) > limit:
                out["violations"].append({"rule": "max_step", "param": param,
                                          "detail": "delta %s > limit %s (current %s)"
                                          % (round(abs(value - float(cur)), 4),
                                             limit, cur)})
    if out["violations"]:
        out["status"] = "violation"
    return out


def check_commands(commands, schema_dict, current_values=None):
    """批量检查。commands: str 列表。返回 {ok, per_command, violations}。"""
    guard = load_schema_guard(schema_dict)
    if not isinstance(commands, list):
        return {"ok": False, "per_command": [],
                "violations": [{"rule": "input", "detail": "commands must be list"}]}
    per = [_check_one(str(c), guard, current_values or {}) for c in commands]
    bad = [v for p in per for v in p["violations"]]
    return {"ok": not bad, "per_command": per, "violations": bad,
            "guard_active": guard is not None}


# ---------------------------------------------------------------- 命令渲染
def render_set(guard, param, value):
    """按 restore_pattern 反向渲染设参命令(回滚用)。guard=None 或 param 不在
    fields 时返回 None。"""
    if guard is None or guard.get("restore_pattern") is None:
        return None
    pat = guard["restore_pattern"]
    v = value
    if isinstance(value, float) and value == int(value):
        v = int(value)
    return pat.replace("{param}", str(param)).replace("{value}", str(v))


# ---------------------------------------------------------------- 回滚判定
def check_metrics(metrics, score, guard):
    """跑完触发回滚的条件判定。返回触发列表(空 = 不触发)。纯键值比较。"""
    if guard is None:
        return []
    triggers = []
    if guard["rollback_score_min"] is not None:
        if not isinstance(score, (int, float)) or isinstance(score, bool):
            triggers.append({"rule": "score_missing",
                             "detail": "score unavailable (expected >= %s)"
                             % guard["rollback_score_min"]})
        elif score < guard["rollback_score_min"]:
            triggers.append({"rule": "score_min",
                             "detail": "score %s < rollback threshold %s"
                             % (score, guard["rollback_score_min"])})
    if isinstance(metrics, dict):
        for m in guard["rollback_metrics"]:
            v = metrics.get(m["key"])
            if not isinstance(v, (int, float)) or isinstance(v, bool):
                continue  # 指标缺失不触发(评分层另有 missing_metric 语义)
            if "min" in m and v < m["min"]:
                triggers.append({"rule": "metric_min", "key": m["key"],
                                 "detail": "%s=%s < min %s"
                                 % (m["key"], v, m["min"])})
            if "max" in m and v > m["max"]:
                triggers.append({"rule": "metric_max", "key": m["key"],
                                 "detail": "%s=%s > max %s"
                                 % (m["key"], v, m["max"])})
    return triggers


# ---------------------------------------------------------------- 审计
def audit_path(app_dir=None):
    """审计文件路径:app_dir/runtime/audit.jsonl。"""
    base = Path(app_dir) if app_dir else Path(__file__).resolve().parent
    return base / "runtime" / "audit.jsonl"


def audit(entry, app_dir=None):
    """追加一条审计事件(append-only)。entry 至少含 actor/action/verdict。"""
    p = audit_path(app_dir)
    p.parent.mkdir(parents=True, exist_ok=True)
    rec = {"ts": time.time(), "ts_iso": time.strftime("%Y-%m-%d %H:%M:%S")}
    rec.update(entry)
    with p.open("a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False, default=str) + "\n")
    return str(p)
