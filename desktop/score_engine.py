# -*- coding: utf-8 -*-
"""score_engine.py - 配置驱动的试验评分引擎(2026-08-13, 设计文档 §2)

通用核:零被调对象语义。指标键/权重/阈值全部来自 score_profile.json
(领域数据文件),本模块只做 lower/higher 归一化、加权求和、pass/fail 判定。

  from score_engine import load_profile, compute_score
  prof = load_profile("score_profile.json")          # 非法返回 None
  out  = compute_score(metrics_dict, prof)           # metrics 来自 report_metrics()
  # -> {"score": 72.5, "pass": True, "breakdown": [{key,value,weight,contrib,ok}, ...]}

方向约定:
  direction="lower": contrib = clamp(1 - value/ref, 0, 1)  (越小越好, 0 得满分)
  direction="higher": contrib = clamp(value/ref, 0, 1)     (越大越好, >=ref 得满分)
pass 判定: 声明了 pass_max(lower)/pass_min(higher) 的指标全部达标;
  未声明阈值的指标只参与 score 不参与 pass。
missing_metric: "fail"=缺指标则 pass=False 且贡献按 0; "skip"=从权重剔除。
P0-3 分段归因: 指标声明 "per_segment": true 时,引擎从 metrics["segments"]
  取各段值,按 segment_aggregation("mean" 段均值 / "worst" 最差段)聚合,
  breakdown 附加 segment_values/worst_segment 供归因钻取。
"""
import json
from pathlib import Path


def load_profile(path):
    """读 score profile 并校验;任何非法返回 None(调用方按"不打分"处理)。"""
    try:
        d = json.loads(Path(path).read_text(encoding="utf-8"))
    except Exception:
        return None
    if not isinstance(d, dict):
        return None
    metrics = d.get("metrics")
    if not isinstance(metrics, list) or not metrics:
        return None
    checked = []
    for m in metrics:
        if not isinstance(m, dict):
            return None
        key = m.get("key")
        direction = m.get("direction", "lower")
        weight = m.get("weight", 1.0)
        ref = m.get("ref")
        if (not isinstance(key, str) or not key.strip()
                or direction not in ("lower", "higher")
                or not isinstance(weight, (int, float)) or isinstance(weight, bool)
                or weight <= 0
                or not isinstance(ref, (int, float)) or isinstance(ref, bool)
                or ref <= 0):
            return None
        entry = {"key": key.strip(), "direction": direction,
                 "weight": float(weight), "ref": float(ref)}
        # P0-3 分段归因(设计文档 §3.2): 指标可声明按段计算
        ps = m.get("per_segment", False)
        if not isinstance(ps, bool):
            return None
        agg = m.get("segment_aggregation", "mean")
        if agg not in ("mean", "worst"):
            return None
        entry["per_segment"] = ps
        entry["segment_aggregation"] = agg
        pmx, pmn = m.get("pass_max"), m.get("pass_min")
        if pmx is not None:
            if not isinstance(pmx, (int, float)) or isinstance(pmx, bool):
                return None
            entry["pass_max"] = float(pmx)
        if pmn is not None:
            if not isinstance(pmn, (int, float)) or isinstance(pmn, bool):
                return None
            entry["pass_min"] = float(pmn)
        checked.append(entry)
    missing = d.get("missing_metric", "fail")
    if missing not in ("fail", "skip"):
        return None
    return {"metrics": checked, "missing_metric": missing}


def _contrib(value, direction, ref):
    if direction == "lower":
        c = 1.0 - value / ref
    else:
        c = value / ref
    return min(max(c, 0.0), 1.0)


def _segment_values(metrics, key):
    """取分段值列表:metrics["segments"]["metrics"] 中每段的 key。"""
    seg = metrics.get("segments") if isinstance(metrics, dict) else None
    if not isinstance(seg, dict):
        return None
    rows = seg.get("metrics")
    if not isinstance(rows, list) or not rows:
        return None
    out = []
    for i, r in enumerate(rows):
        v = r.get(key) if isinstance(r, dict) else None
        if isinstance(v, (int, float)) and not isinstance(v, bool):
            out.append(float(v))
        else:
            out.append(None)
    return out if any(v is not None for v in out) else None


def _num(v):
    """数值判定(与旧实现同口径)。"""
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def _judge(v, m, direction, ref):
    """单值 -> (contrib, ok)。"""
    contrib = round(_contrib(v, direction, ref), 4)
    ok = True
    if "pass_max" in m and v > m["pass_max"]:
        ok = False
    if "pass_min" in m and v < m["pass_min"]:
        ok = False
    return contrib, ok


def compute_score(metrics, profile):
    """metrics: report_metrics() 的 dict(可含 segments 分段)。
    profile 指标声明 per_segment 时按段聚合(mean 段均值 / worst 最差段),
    breakdown 附加 segment_values/worst_segment 供归因钻取。"""
    if not isinstance(metrics, dict) or not isinstance(profile, dict):
        return None
    breakdown = []
    wsum = 0.0
    acc = 0.0
    all_ok = True
    for m in profile["metrics"]:
        item = {"key": m["key"], "weight": m["weight"]}
        value = metrics.get(m["key"])
        seg_vals = None
        if m.get("per_segment"):
            seg_vals = _segment_values(metrics, m["key"])
        has = _num(value) or seg_vals is not None
        item["value"] = float(value) if _num(value) else None
        if not has:
            if profile["missing_metric"] == "skip":
                item["skipped"] = True
                breakdown.append(item)
                continue
            item["contrib"] = 0.0
            item["ok"] = False
            all_ok = False
        elif seg_vals is not None:
            valid = [v for v in seg_vals if v is not None]
            if not valid:  # 分段声明但段值全缺: 回退全局值
                if _num(value):
                    v = float(value)
                    item["contrib"], item["ok"] = _judge(v, m, m["direction"], m["ref"])
                    if not item["ok"]:
                        all_ok = False
                else:
                    item["contrib"] = 0.0
                    item["ok"] = False
                    all_ok = False
            else:
                if m.get("segment_aggregation") == "worst":
                    # worst 对 lower 方向 = 最大值段;higher 方向 = 最小值段
                    if m["direction"] == "lower":
                        idx = max((i for i, v in enumerate(seg_vals) if v is not None),
                                  key=lambda i: seg_vals[i])
                    else:
                        idx = min((i for i, v in enumerate(seg_vals) if v is not None),
                                  key=lambda i: seg_vals[i])
                    v = seg_vals[idx]
                else:  # mean
                    v = sum(valid) / len(valid)
                item["contrib"], item["ok"] = _judge(v, m, m["direction"], m["ref"])
                if not item["ok"]:
                    all_ok = False
                item["segment_values"] = seg_vals
                # worst_segment 恒取方向上最差段(lower=最大, higher=最小),归因用
                if m["direction"] == "lower":
                    item["worst_segment"] = max(
                        (i for i, vv in enumerate(seg_vals) if vv is not None),
                        key=lambda i: seg_vals[i])
                else:
                    item["worst_segment"] = min(
                        (i for i, vv in enumerate(seg_vals) if vv is not None),
                        key=lambda i: seg_vals[i])
                item["segment_aggregation"] = m.get("segment_aggregation", "mean")
        else:
            v = float(value)
            item["contrib"], item["ok"] = _judge(v, m, m["direction"], m["ref"])
            if not item["ok"]:
                all_ok = False
        acc += m["weight"] * item["contrib"]
        wsum += m["weight"]
        breakdown.append(item)
    if wsum <= 0:
        return None
    return {"score": round(100.0 * acc / wsum, 1),
            "pass": all_ok,
            "breakdown": breakdown}
