# -*- coding: utf-8 -*-
"""session_driver.py - 调车会话驱动器(2026-06-10,下次调车快速通关包核心)

单进程独占 /command 通道:保活、设参、ACK 关联在同一事件循环里串行,
彻底消灭"心跳 GET STREAMS 冲掉 ACK"的竞态(2026-06-10 实测踩坑:dump 的
TRACK_DUMP ACK 被并行保活脚本覆盖,误判'固件没烧命令')。

⚠ 铁律:本脚本【永远不发任何让车动的命令】(MISSION RETURN/EVENT1/LEARN
全部拒绝)。发车 = 用户在车上按菜单,或用户口头确认后由人发。脚本只负责:
监控先行(从 idx 0 抓全程)、跑完自动出报告。

模式:
  python session_driver.py --bench-check
      开机台架自检(不动车):链路/录制点数/TRACK SAVE 0 探测(判固件
      带不带 69bb1f7e 上传族,顺手把 RAM 录制持久化进 flash 槽 0)。
  python session_driver.py --arm --label baseline
      [可选 --set "SET MISSION ADAPT_LA 1" --set ...] 逐条设参(ACK 验证)
      → 订阅 MISSION/DRIVE/STATS → 武装监控等用户按菜单发车 → 全程捕获
      → eng 1→0 收尾 → 存 recordings/run_<ts>_<label>.json + 自动报告。
  python session_driver.py --report recordings/run_xxx.json
      重打印已存捕获的报告。
  python session_driver.py --do recipe.json [--json]
      按配方一次走完(设计文档 §4.3):set 经 /batch 一次下发(ACK 验证)
      → streams 订阅 → capture_s>0 时定时捕获,=0 只布防等用户按菜单
      发车(铁律不变,脚本永不让车动)。
      退出码:0 成功 / 2 ACK 超时(含 ERR)/ 3 链路失败 / 4 配方非法 / 5 禁发命令。
      --json:结构化结果走 stdout,人类可读文本走 stderr。
  python session_driver.py --score recordings/run_x.json [--profile p.json] [--json]
      对已存捕获重算 metrics + score(P1, 设计文档 §2.3;复评历史趟)。
  python session_driver.py --sweep sweep.json [--json]
      参数网格扫描(P2, 设计文档 §3.2):每趟 下发参数(ACK 验证)→ 布防等人
      按菜单发车 → eng 1→0 收尾 → metrics+score → 写 recordings/runs.jsonl;
      支持早停(consecutive_worse)。退出码同 --do 语义。
  python session_driver.py --optimize opt.json [--json]
      TPE 贝叶斯优化(P0-1, 设计文档 2026-08-14 §1):预算感知顺序提议,
      其余趟流程与 --sweep 相同;护栏预检/回滚/审计全量生效。
  python session_driver.py --diff a.json b.json [--json]
      两个捕获文件逐键 diff(全局指标 + 段级, P0-3)。
  --profile <name>: 决议 profiles/<name>/ 的 schema/score_profile(护栏同源)。

报告指标(摆头确诊三件套 + 常规):
  rev        = m_str 翻号次数(|跳变|>4°,= 用户看到的"反向打方向盘")
  |dStr|     = 平均每帧转向变化(平滑度)
  str vs ff  = 总转向 与 前馈分量 同帧相关(几何环 vs 前馈打架判别)
  gnd        = DR 位置差分地速(m_spd 是指令不是地速!)
  duty_sat   = |duty|>7500 帧占比(后轮天花板判别)
  lostsrc    = abort 来源解码(BRAKE/DIRKEY/WATCHDOG/TCP/CPU1/HEARTBEAT)
"""
import argparse
import itertools
import json
import math
import sys
import time
import urllib.request
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
BASE = "http://127.0.0.1:9898"
APP = Path(__file__).resolve().parent
RUNS_DB = APP / "recordings" / "runs.jsonl"
LOST_BITS = {0x01: "BRAKE", 0x02: "DIRKEY", 0x04: "WATCHDOG",
             0x08: "TCP_DOWN", 0x10: "CPU1_DEAD", 0x20: "HEARTBEAT"}
# 发车/录制类命令一律拒绝(铁律:发车只能人来)。schema 未声明黑名单时的兜底;
# profile 声明 guardrails.forbidden_commands 时以声明为准(P0-2)。
FORBIDDEN = ("MISSION RETURN", "MISSION EVENT", "MISSION LEARN")

import guardrails as G          # P0-2 护栏引擎(通用核,stdlib-only)
from optimizer import Optimizer  # P0-1 TPE 优化器(通用核,stdlib-only)
try:  # profile 决议复用 config_loader(桥同源),缺失时回退 app_dir
    import config_loader as CL
except ImportError:  # pragma: no cover
    CL = None


def http_get(path, timeout=2.0):
    try:
        return json.loads(urllib.request.urlopen(BASE + path, timeout=timeout).read())
    except Exception:
        return None


def http_cmd(cmd, timeout=2.0):
    d = json.dumps({"command": cmd}).encode()
    r = urllib.request.Request(BASE + "/command", data=d,
                               headers={"Content-Type": "application/json"})
    try:
        urllib.request.urlopen(r, timeout=timeout).read()
        return True
    except Exception as e:
        print(f"  [cmd] POST失败: {e}")
        return False


def send_verified(cmd, ack_timeout=2.5):
    """发命令并等它自己的 ACK/ERR(单进程串行,无并发保活冲 cmd_result)。"""
    before = (http_get("/latest") or {}).get("cmd_result", {}) or {}
    ts0 = before.get("ts", 0)
    if not http_cmd(cmd):
        return None
    t0 = time.time()
    while time.time() - t0 < ack_timeout:
        cr = (http_get("/latest") or {}).get("cmd_result", {}) or {}
        if cr.get("ts", 0) != ts0:
            return cr
        time.sleep(0.15)
    return None


def decode_lost(v):
    try:
        v = int(v)
    except (TypeError, ValueError):
        return "?"
    if v == 0:
        return "none"
    return "+".join(n for b, n in LOST_BITS.items() if v & b)


# ---------------------------------------------------------------- P1 score
def resolve_profile_name():
    """从命令行解析 --profile <name>(与 config_loader 同语义)。"""
    if CL is not None:
        return CL.PROFILE_NAME
    for i, a in enumerate(sys.argv):
        if a == "--profile" and i + 1 < len(sys.argv):
            return sys.argv[i + 1].strip() or None
        if a.startswith("--profile="):
            return a.split("=", 1)[1].strip() or None
    return None


def schema_path():
    """当前生效的 control_schema.json(profile 优先,回退 app_dir)。与桥同源。"""
    if CL is not None:
        p = CL.profile_schema_path()
        if p is not None:
            return p
    p = APP / "control_schema.json"
    return p if p.is_file() else None


def load_schema_dict():
    try:
        return json.loads(Path(schema_path()).read_text(encoding="utf-8")) \
            if schema_path() else None
    except Exception:
        return None


def find_profile(explicit=None):
    """score profile 查找:--profile 显式 > 当前 profile 目录 > app_dir > None。"""
    from score_engine import load_profile
    if explicit:
        p = Path(explicit)
        return load_profile(p) if p.is_file() else None
    name = resolve_profile_name()
    if name:
        p = APP / "profiles" / name / "score_profile.json"
        if p.is_file():
            return load_profile(p)
    p = APP / "score_profile.json"
    return load_profile(p) if p.is_file() else None


def score_metrics(metrics, profile):
    if profile is None or metrics is None:
        return None
    from score_engine import compute_score
    return compute_score(metrics, profile)


# ---------------------------------------------------------------- P2 runs db
def record_run(record):
    """append-only 趟次记录(设计文档 §3.1)。单写者=本进程串行,无锁。"""
    RUNS_DB.parent.mkdir(parents=True, exist_ok=True)
    with RUNS_DB.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")


# ---------------------------------------------------------------- bench check
def bench_check():
    st = http_get("/status")
    if not st:
        print("[FAIL] 桥不在(YawTuningTool.exe 没开?)")
        return 2
    print(f"[1] 桥 OK, connections={st.get('connections')}")
    if not st.get("connections"):
        print("[FAIL] 车没连上来(车端电源/wifi)")
        return 2
    cr = send_verified("GET ONCE MISSION")
    rec = None
    if cr and cr.get("ack") == "ACK":
        rec = cr.get("data", {}).get("m_rec_pts")
    if rec is None:
        m = (http_get("/latest") or {}).get("packets", {}).get("TELMISSION", {})
        rec = m.get("m_rec_pts")
    print(f"[2] 录制点数 m_rec_pts = {rec}")
    print("[3] TRACK SAVE 0 探测(固件带不带 69bb1f7e 上传族;顺手持久化录制)…")
    cr = send_verified("TRACK SAVE 0", ack_timeout=4.0)
    if cr is None:
        print("    -> 静默 = 固件【不带】TRACK SAVE/UPLOAD/LOAD")
        print("    -> 下一步:ADS 全量重编译烧录(同时带入 Gate8 monotonic 修复)")
        return 1
    print(f"    -> {cr.get('ack')} {json.dumps(cr.get('data', {}), ensure_ascii=False)}")
    if cr.get("ack") == "ACK":
        print("    -> 固件带上传族 ✓ 录制已存 flash 槽 0 ✓")
        print("    -> 可直接: python track_upload.py paths/time32_final_upload.json --save-slot 1")
        print("       然后 TRACK LOAD 0 还原原线(跑基线), TRACK LOAD 1 切优化线")
    return 0


# ---------------------------------------------------------------- arm/capture
def arm(label, sets, max_wait_s=300.0, max_run_s=120.0):
    st = http_get("/status")
    if not st or not st.get("connections"):
        print("[FAIL] 桥/车不在,先 --bench-check")
        return 2
    for c in sets:
        u = c.upper()
        if any(f in u for f in FORBIDDEN):
            print(f"[REJECT] '{c}' 是发车/录制类命令,本脚本铁律拒发")
            return 2
    cr = send_verified("SET MISSION HEARTBEAT_TIMEOUT 5000")
    print(f"[1] HEARTBEAT_TIMEOUT 5000 -> {cr.get('ack') if cr else 'no-ack'}")
    for c in sets:
        cr = send_verified(c)
        ack = cr.get("ack") if cr else "NO-ACK"
        print(f"[2] {c}  ->  {ack}")
        if not cr or cr.get("ack") != "ACK":
            print("    [WARN] 设参没 ACK,确认命令拼写/固件支持")
    for s in ("START STREAM MISSION 100", "START STREAM DRIVE 100",
              "START STREAM STATS 1000"):
        http_cmd(s)
    time.sleep(0.8)
    print(f"[3] 三流已订。监控已武装(label={label})。")
    print(">>> 现在可以发车(菜单 RETURN / 用户确认的命令)。我只看不发。<<<")
    rows = capture_run(max_wait_s, max_run_s)
    out = APP / "recordings" / f"run_{time.strftime('%Y%m%d-%H%M%S')}_{label}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps({"label": label, "sets": sets, "rows": rows},
                              ensure_ascii=False), encoding="utf-8")
    print(f"[saved] {out}  ({len(rows)} samples)")
    metrics = report_metrics(rows)
    record_run({"run_id": f"{time.strftime('%Y%m%d-%H%M%S')}_{label}",
                "ts": time.time(), "label": label, "sets": sets,
                "capture_file": str(out), "metrics": metrics,
                "score": score_metrics(metrics, find_profile())})
    report_rows(rows, label)
    return 0


def capture_run(max_wait_s=300.0, max_run_s=120.0):
    """布防后捕获一整趟:等 eng 1→0 收尾(--arm/--sweep 共用,row 形状不变)。
    铁律:只看不发;唯一主动命令是早期发散的 MISSION ABORT(停车兜底)。"""
    rows = []
    t0 = time.time()
    engaged_seen = False
    idle_after = 0
    last_print = 0.0
    while True:
        te = time.time() - t0
        if not engaged_seen and te > max_wait_s:
            print(f"[timeout] {max_wait_s:.0f}s 没等到发车,收监控")
            break
        if engaged_seen and te > max_run_s + max_wait_s:
            print("[timeout] 跑超时,收监控")
            break
        # 心跳保活已由桥(YawTuningTool)自动做(连着就每 2s 发 HB,不污染 cmd_result)
        # —— 这里不再自发 GET STREAMS(那会用 ACK 冲掉真命令的 cmd_result)。
        # 老 exe(无自动心跳)回退:取消下面注释。
        # if time.time() - last_ka > 2.0:
        #     http_cmd("HB"); last_ka = time.time()
        d = http_get("/latest") or {}
        pk = d.get("packets", {})
        m = pk.get("TELMISSION", {})
        dr = pk.get("TELDRIVE", {})
        s = pk.get("TELSTATS", {})
        eng = m.get("m_eng")
        row = {"te": round(te, 2)}
        row.update({k: m.get(k) for k in (
            "ms", "m_st", "m_eng", "m_idx", "m_pts", "m_xte", "m_yer",
            "m_str", "m_ff", "m_spd", "m_dst", "m_lost", "m_lostsrc",
            "m_brk", "m_cpx", "m_cpy", "m_tpx", "m_tpy", "m_rev")})
        row.update({k: dr.get(k) for k in ("d_tgt", "d_mL", "d_mR",
                                           "d_dL", "d_dR")})
        row.update({k: s.get(k) for k in ("fs", "alat", "adla", "adlt", "slew")})
        rows.append(row)
        if eng == 1:
            engaged_seen = True
            if te - last_print > 1.0:
                print(f"  t={te:5.1f} idx={m.get('m_idx')}/{m.get('m_pts')} "
                      f"xte={m.get('m_xte')} str={m.get('m_str')} "
                      f"spd={m.get('m_spd')} dL={dr.get('d_dL')} dR={dr.get('d_dR')}")
                last_print = te
            # 早期发散自动 ABORT(防撞兜底,这是【停车】命令,不是发车)
            try:
                if (int(m.get("m_idx", 99)) <= 30
                        and abs(float(m.get("m_yer", 0))) > 130.0):
                    print("  !!! 早期发散(idx<30 |yer|>130)-> MISSION ABORT")
                    http_cmd("MISSION ABORT")
            except (TypeError, ValueError):
                pass
        if engaged_seen and eng == 0:
            idle_after += 1
            if idle_after >= 5:
                print(f"  -- 结束 m_st={m.get('m_st')} "
                      f"lostsrc={decode_lost(m.get('m_lostsrc'))} --")
                break
        else:
            idle_after = 0
        time.sleep(0.1)
    return rows


# ---------------------------------------------------------------- report
def _metrics_from_eng(eng):
    """从 engaged 帧列表算核心指标(段级与全局共用同一份计算,数值必然一致)。"""
    def col(k):
        return [r[k] for r in eng if r.get(k) is not None]
    strs = col("m_str")
    rev = sum(1 for i in range(1, len(strs))
              if strs[i] * strs[i - 1] < 0 and abs(strs[i] - strs[i - 1]) > 4.0)
    dstr = (sum(abs(strs[i] - strs[i - 1]) for i in range(1, len(strs)))
            / max(len(strs) - 1, 1))
    xte = [abs(v) for v in col("m_xte")]
    # 地速:DR 位置差分(m_spd 是指令!)
    gnd = []
    prev = None
    for r in eng:
        if r.get("m_cpx") is None:
            continue
        if prev is not None:
            dt = r["te"] - prev["te"]
            if dt > 0.02:
                gnd.append(math.hypot(r["m_cpx"] - prev["m_cpx"],
                                      r["m_cpy"] - prev["m_cpy"]) / dt)
        prev = r
    duty = [max(abs(r.get("d_dL") or 0), abs(r.get("d_dR") or 0)) for r in eng]
    sat = 100.0 * sum(1 for v in duty if v > 7500) / max(len(duty), 1)
    ffs = col("m_ff")
    ff_fight_pct = None
    if ffs and strs and len(ffs) == len(strs):
        fights = sum(1 for a, b in zip(strs, ffs)
                     if abs(a) > 3 and abs(b) > 1 and a * b < 0)
        ff_fight_pct = 100.0 * fights / len(strs)
    spd = col("m_spd")
    return {
        "rev": rev,
        "dstr_deg_per_frame": round(dstr, 3),
        "ff_fight_pct": round(ff_fight_pct, 1) if ff_fight_pct is not None else None,
        "xte_mean_m": round(sum(xte) / max(len(xte), 1), 4),
        "xte_max_m": round(max(xte) if xte else 0, 4),
        "gnd_mean_ms": round(sum(gnd) / len(gnd), 3) if gnd else None,
        "gnd_p95_ms": round(sorted(gnd)[int(0.95 * len(gnd)) - 1], 3) if gnd else None,
        "spd_cmd_mean_ms": round(sum(spd) / max(len(spd), 1) / 1000.0, 3),
        "duty_sat_pct": round(sat, 1),
    }


def _segment_metrics(eng, count=10):
    """engaged 帧按 m_idx/(m_pts) 归一化分 count 段,每段算段级指标(P0-3)。"""
    buckets = [[] for _ in range(count)]
    for r in eng:
        idx = r.get("m_idx")
        pts = r.get("m_pts")
        if idx is None or not pts:
            continue
        si = int(min(float(idx) / max(float(pts), 1.0), 0.999) * count)
        buckets[si].append(r)
    out = []
    for i, rows in enumerate(buckets):
        m = _metrics_from_eng(rows)
        m["seg"] = i
        m["frames"] = len(rows)
        out.append(m)
    return {"count": count, "metrics": out}


def report_metrics(rows, segment_count=10):
    """从捕获 rows 算报告指标,返回结构化 dict;engaged 帧不足返回 None。
    打印版 report_rows 与本函数共用同一份计算,数值两边必然一致。
    P0-3: metrics["segments"] 附加分段指标(设计文档 §3.1),旧键不变。"""
    eng = [r for r in rows if r.get("m_eng") == 1]
    if len(eng) < 5:
        return None
    m = _metrics_from_eng(eng)
    end = rows[-1]
    m.update({
        "engaged_frames": len(eng),
        "duration_s": round(eng[-1]["te"] - eng[0]["te"], 2),
        "end_idx": eng[-1].get("m_idx"),
        "end_pts": eng[-1].get("m_pts"),
        "m_st": end.get("m_st"),
        "lostsrc": decode_lost(end.get("m_lostsrc")),
    })
    if segment_count and len(eng) >= segment_count * 5:
        m["segments"] = _segment_metrics(eng, segment_count)
    return m


def report_rows(rows, label):
    m = report_metrics(rows)
    if m is None:
        eng_n = sum(1 for r in rows if r.get("m_eng") == 1)
        print(f"[report:{label}] engaged 帧不足({eng_n}),没跑起来?"
              f" 终态 lostsrc={decode_lost(rows[-1].get('m_lostsrc')) if rows else '?'}")
        return
    ff_note = (f"  str/ff反号帧={m['ff_fight_pct']:.0f}%(前馈打架判别)"
               if m["ff_fight_pct"] is not None else "")
    print(f"[report:{label}] 时长={m['duration_s']:.1f}s  idx到={m['end_idx']}/{m['end_pts']}")
    print(f"  摆头: rev={m['rev']} 次翻号  |dStr|={m['dstr_deg_per_frame']:.2f}deg/帧{ff_note}")
    print(f"  xte : mean={m['xte_mean_m']:.3f} max={m['xte_max_m']:.3f} m")
    if m["gnd_mean_ms"] is not None:
        print(f"  地速: mean={m['gnd_mean_ms']:.2f} p95={m['gnd_p95_ms']:.2f} m/s"
              f"  (指令均值={m['spd_cmd_mean_ms']:.2f})")
    print(f"  duty: >7500 占比 {m['duty_sat_pct']:.0f}%  (高=后轮天花板,提速白给)")
    print(f"  终态: m_st={m['m_st']} lostsrc={m['lostsrc']}")


# ---------------------------------------------------------------- P0-2 护栏
def guard_check(commands):
    """护栏预检:读当前 profile schema 做 check_commands。schema 缺失/未声明
    黑名单时用内置 FORBIDDEN 兜底(铁律)。返回 {ok, violations, guard_active}。"""
    for c in commands:
        u = c.upper()
        for f in FORBIDDEN:
            if f in u:
                return {"ok": False, "guard_active": True, "violations": [
                    {"rule": "forbidden", "cmd": c,
                     "detail": "matches builtin FORBIDDEN %s" % f}]}
    res = G.check_commands(commands, load_schema_dict())
    res["guard_active"] = bool(res.get("guard_active")
                                or load_schema_dict() is not None)
    return res


def snapshot_current_params(param_names):
    """快照参数当前值:优先 /latest params 遥测通道,缺失键 GET 读回。"""
    vals = {}
    d = http_get("/latest") or {}
    params = ((d.get("packets") or {}).get("params") or {})
    for name in param_names:
        v = params.get(name)
        if v is not None:
            vals[name] = v
    for name in param_names:  # GET 兜底(契约 v1)
        if name in vals:
            continue
        cr = send_verified("GET %s" % name, ack_timeout=1.5)
        data = (cr or {}).get("data") or {}
        v = data.get(name, data.get("value"))
        if v is not None:
            try:
                vals[name] = float(v)
            except (TypeError, ValueError):
                vals[name] = v
    return vals


def restore_params(snapshot):
    """回滚:按快照渲染恢复命令(经 /batch ACK)。返回 (ok, detail)。"""
    guard = G.load_schema_guard(load_schema_dict())
    cmds = []
    for k, v in snapshot.items():
        c = G.render_set(guard, k, v)
        if c is None:
            return False, "no restore template for %s" % k
        cmds.append(c)
    if not cmds:
        return True, None
    code, body = http_batch([{"cmd": c, "expect": "ACK"} for c in cmds],
                            stop_on_error=True)
    if body is None or "results" not in body:
        return False, "batch failed (http=%s)" % code
    bad = [r for r in body["results"] if r["status"] != "ack"]
    return (False, bad) if bad else (True, None)


def audit_event(action, commands, verdict, extra=None):
    """driver 侧审计事件(runtime/audit.jsonl,append-only)。"""
    try:
        rec = {"actor": "driver", "action": action, "commands": commands,
               "verdict": verdict, "role": "agent"}
        if extra:
            rec.update(extra)
        G.audit(rec, app_dir=APP)
    except Exception as e:  # 审计失败不阻塞主流程
        print("  [audit warn] %s" % e, file=sys.stderr)



# ---------------------------------------------------------------- optimize (P0-1)
def load_optimize(path):
    """读 optimize spec 并校验(设计文档 §1.3);非法返回 None(调用方 exit 4)。"""
    try:
        d = json.loads(Path(path).read_text(encoding="utf-8"))
    except Exception:
        return None
    if not isinstance(d, dict):
        return None
    params = d.get("params")
    if (not isinstance(params, list) or not params
            or not all(isinstance(p, dict)
                       and isinstance(p.get("name"), str) and p["name"].strip()
                       and isinstance(p.get("template"), str)
                       and "{value}" in p["template"]
                       for p in params)):
        return None
    # v2: type=continuous(缺省)|categorical;分类需 choices;连续数值域校验
    for p in params:
        ptype = p.get("type", "continuous")
        if ptype not in ("continuous", "categorical"):
            return None
        if ptype == "categorical":
            ch = p.get("choices")
            if (not isinstance(ch, list) or len(ch) < 2
                    or not all(c is not None for c in ch)):
                return None
            init = p.get("init")
            if init is not None and init not in ch:
                return None
        else:
            for k in ("min", "max", "step", "init"):
                v = p.get(k)
                if (v is not None and not isinstance(v, (int, float))
                        or isinstance(v, bool)):
                    return None
    base_set = d.get("base_set", [])
    if not isinstance(base_set, list) or not all(isinstance(c, str) and c.strip()
                                                 for c in base_set):
        return None
    streams = d.get("streams", [])
    if not isinstance(streams, list):
        return None
    for s in streams:
        if (not isinstance(s, (list, tuple)) or len(s) != 2
                or not isinstance(s[0], str) or not s[0].strip()
                or not isinstance(s[1], (int, float)) or isinstance(s[1], bool)):
            return None
    objectives = d.get("objectives", [{"key": "score", "direction": "max"}])
    if not isinstance(objectives, list) or not objectives:
        return None
    for o in objectives:
        if (not isinstance(o, dict) or not isinstance(o.get("key"), str)
                or o.get("direction", "max") not in ("max", "min")):
            return None
    budget = d.get("budget", {}) or {}
    mr = budget.get("max_runs")
    mw = budget.get("max_wall_s")
    if mr is not None and (not isinstance(mr, int) or isinstance(mr, bool) or mr < 1):
        return None
    if mw is not None and (not isinstance(mw, (int, float)) or isinstance(mw, bool)
                           or mw <= 0):
        return None
    warmup = d.get("warmup", 0)
    if not isinstance(warmup, int) or isinstance(warmup, bool) or warmup < 0:
        return None
    streak = d.get("max_no_result_streak", 0)
    if not isinstance(streak, int) or isinstance(streak, bool) or streak < 0:
        return None
    seed = d.get("seed", 0)
    if not isinstance(seed, int) or isinstance(seed, bool):
        return None
    return {"label": str(d.get("label", "optimize")),
            "base_set": [c.strip() for c in base_set],
            "params": params,
            "objectives": objectives,
            "budget": budget,
            "warmup": warmup,
            "max_no_result_streak": streak,
            "streams": [[s[0].strip(), int(s[1])] for s in streams],
            "max_wait_s": float(d.get("max_wait_s", 300.0)),
            "max_run_s": float(d.get("max_run_s", 120.0)),
            "report": bool(d.get("report", True)),
            "rollback": bool(d.get("rollback", True)),
            "seed": seed}


def resolve_param_ranges(params):
    """params 缺 min/max/step 时从当前 profile schema fields 补齐(v2:
    categorical 只校验 choices,不查数值域);任一连续参数最终无范围 ->
    None(调用方 exit 4)。"""
    schema = load_schema_dict()
    fmap = {}
    if schema:
        for f in schema.get("fields", []):
            if isinstance(f, dict) and f.get("id"):
                fmap[str(f["id"])] = f
                fmap[str(f["id"]).lower()] = f
    out = []
    for p in params:
        q = dict(p)
        if q.get("type") == "categorical":
            out.append(q)
            continue
        f = fmap.get(str(p["name"]).strip().lower())
        if f:
            for k in ("min", "max", "step"):
                if q.get(k) is None and f.get(k) is not None:
                    q[k] = f[k]
        if q.get("min") is None or q.get("max") is None:
            return None
        out.append(q)
    return out


def do_optimize(spec_path, json_mode=False):
    """--optimize 主流程(P0-1, 设计文档 §1.3): TPE 顺序提议,每趟 下发参数
    (ACK 验证)-> 布防等人按菜单发车 -> 捕获 -> metrics+score -> observe;
    预算/连续无结果中止;护栏预检 + 回滚与 --sweep 同。退出码沿用 0/2/3/4/5。"""
    def human(msg):
        print(msg, file=sys.stderr if json_mode else sys.stdout)

    result = {"ok": False, "label": None, "runs": []}

    def finish(code):
        result["exit"] = code
        result["ok"] = (code == 0)
        if json_mode:
            json.dump(result, sys.stdout, ensure_ascii=False, default=str)
            sys.stdout.write("\n")
            sys.stdout.flush()
        return code

    spec = load_optimize(spec_path)
    if spec is None:
        human(f"[FAIL] optimize 文件非法: {spec_path}")
        result["error"] = "invalid optimize spec"
        return finish(4)
    result["label"] = spec["label"]
    rparams = resolve_param_ranges(spec["params"])
    if rparams is None:
        human("[FAIL] 参数范围缺失(spec 与 schema 都没有 min/max)")
        result["error"] = "param range unresolved"
        return finish(4)
    spec["params"] = rparams

    # 护栏预检:base_set + 每参数样例命令(categorical 用 choices[0])
    sample_vals = []
    for p in spec["params"]:
        if p.get("type") == "categorical":
            sample_vals.append(p["template"].replace(
                "{value}", str(p["choices"][0])))
        else:
            sample_vals.append(p["template"].replace("{value}", str(p["min"])))
    sample_sets = list(spec["base_set"]) + sample_vals
    g = guard_check(sample_sets)
    if not g["ok"]:
        human(f"[REJECT] 护栏拒绝: {json.dumps(g['violations'], ensure_ascii=False)}")
        result["error"] = "guardrail violation"
        result["violations"] = g["violations"]
        audit_event("precheck", sample_sets, "rejected",
                    {"violations": g["violations"]})
        return finish(5)
    result["guard"] = {"guard_active": bool(g.get("guard_active"))}

    st = http_get("/status")
    if not st or not st.get("connections"):
        human("[FAIL] 桥/车不在,先 --bench-check")
        result["error"] = "bridge unreachable" if not st else "no car connection"
        return finish(3)

    profile = find_profile()
    opt = Optimizer({"params": spec["params"], "objectives": spec["objectives"],
                     "budget": spec["budget"], "warmup": spec["warmup"],
                     "seed": spec["seed"]})
    observations = []
    wall0 = time.time()
    no_result_streak = 0

    while True:
        wall = time.time() - wall0
        prop = opt.propose(observations, wall_s=wall)
        if prop is None:
            result["stopped_reason"] = "budget"
            break
        params = prop["params"]
        combo_sets = list(spec["base_set"]) + [
            p["template"].replace("{value}", str(params[p["name"]]))
            for p in spec["params"]]
        tag = "_".join("%s%s" % (k, params[k]) for k in sorted(params))
        run_id = f"{time.strftime('%Y%m%d-%H%M%S')}_{spec['label']}_{tag}"
        human(f"[opt {prop['phase']}] {params} -> 下发参数")
        snapshot = snapshot_current_params([p["name"] for p in spec["params"]])
        code, body = http_batch([{"cmd": c, "expect": "ACK"} for c in combo_sets],
                                stop_on_error=True)
        run_rec = {"run_id": run_id, "ts": time.time(), "label": spec["label"],
                   "sets": combo_sets, "optimize": params, "phase": prop["phase"]}
        first = len(observations) == 0
        if body is None or "results" not in body:
            human(f"  [FAIL] /batch 调用失败(http={code}),本趟记无结果")
            run_rec["skipped"] = "batch failed"
            record_run(run_rec)
            result["runs"].append(run_rec)
            observations.append({"params": params, "objectives": {}})
            if first:
                result["error"] = "batch call failed"
                return finish(3)
            continue
        bad = [r for r in body["results"] if r["status"] != "ack"]
        if bad:
            human(f"  [FAIL] 设参未获 ACK(首条: {bad[0]['cmd']} -> {bad[0]['status']})")
            run_rec["skipped"] = "ack timeout/err"
            run_rec["set_results"] = body["results"]
            record_run(run_rec)
            result["runs"].append(run_rec)
            observations.append({"params": params, "objectives": {}})
            guard = G.load_schema_guard(load_schema_dict())
            if guard and guard.get("rollback_on_apply_fail") and snapshot:
                human("  [rollback] 设参失败 -> 恢复趟前值")
                ok, detail = restore_params(snapshot)
                audit_event("rollback", combo_sets, "ok" if ok else "failed",
                            {"triggers": [{"rule": "apply_fail"}], "detail": detail})
            audit_event("set", combo_sets, "failed", {"set_results": bad})
            if first:
                result["error"] = "ack timeout/err"
                return finish(2)
            continue
        if spec["streams"]:
            http_batch([{"cmd": f"START STREAM {n} {p}"}
                        for n, p in spec["streams"]])
        human(f"  [armed] 等人按菜单发车(max_wait={spec['max_wait_s']:.0f}s),我只看不发。")
        rows = capture_run(spec["max_wait_s"], spec["max_run_s"])
        eng_n = sum(1 for r in rows if r.get("m_eng") == 1)
        if eng_n < 5:
            human("  [skip] 没跑起来,本趟记无结果")
            run_rec["skipped"] = "no launch"
            record_run(run_rec)
            result["runs"].append(run_rec)
            observations.append({"params": params, "objectives": {}})
            no_result_streak += 1
            if (spec["max_no_result_streak"] > 0
                    and no_result_streak >= spec["max_no_result_streak"]):
                human(f"  [stop] 连续 {no_result_streak} 趟无结果,中止")
                result["stopped_reason"] = "no result streak"
                break
            continue
        no_result_streak = 0
        out = APP / "recordings" / f"run_{run_id}.json"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps({"label": run_id, "sets": combo_sets,
                                   "rows": rows}, ensure_ascii=False),
                       encoding="utf-8")
        metrics = report_metrics(rows) if spec["report"] else None
        sc = score_metrics(metrics, profile)
        run_rec.update({"capture_file": str(out), "metrics": metrics, "score": sc})
        record_run(run_rec)
        result["runs"].append(run_rec)
        audit_event("set", combo_sets, "ok",
                    {"run_id": run_id, "optimize": params})
        objs = {}
        if sc is not None:
            objs["score"] = sc["score"]
        if metrics:
            for o in spec["objectives"]:
                k = o["key"]
                if k != "score" and metrics.get(k) is not None:
                    objs[k] = metrics[k]
        observations.append({"params": params, "objectives": objs})
        human(f"  [done] samples={len(rows)}"
              + (f"  score={sc['score']}/100 pass={sc['pass']}" if sc else ""))
        guard = G.load_schema_guard(load_schema_dict())
        triggers = G.check_metrics(metrics, sc["score"] if sc is not None else None,
                                   guard)
        if triggers and snapshot and spec["rollback"]:
            human(f"  [rollback] 触发: {json.dumps(triggers, ensure_ascii=False)}")
            ok, detail = restore_params(snapshot)
            run_rec["rollback"] = {"snapshot": snapshot, "triggers": triggers,
                                   "restored": ok, "detail": detail}
            audit_event("rollback", combo_sets, "ok" if ok else "failed",
                        {"triggers": triggers, "detail": detail, "run_id": run_id})

    summ = opt.summary(observations, wall_s=time.time() - wall0)
    result["summary"] = summ
    if "stopped_reason" not in result:
        result["stopped_reason"] = "budget"
    if summ.get("best") and summ["best"].get("objectives"):
        result["best_score"] = summ["best"]["objectives"].get("score")
    audit_event("optimize", sample_sets, "ok",
                {"n_runs": summ["n_runs"], "best_score": result["best_score"],
                 "stopped_reason": result["stopped_reason"]})
    return finish(0)


# ---------------------------------------------------------------- do (recipe)
def http_batch(commands, stop_on_error=False, timeout=None):
    """POST /batch。返回 (http_code, body_dict);链路失败/非 JSON 返回 (code, None)。"""
    if timeout is None:
        # 覆盖所有 ACK 等待上限 + 裕量,别让 urllib 先于桥超时
        timeout = sum(min(int(c.get("timeout_ms", 2500)), 10000)
                      for c in commands if isinstance(c, dict)) / 1000.0 + 5.0
    d = json.dumps({"commands": commands, "stop_on_error": stop_on_error,
                    "role": "agent"}).encode()
    r = urllib.request.Request(BASE + "/batch", data=d,
                               headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(r, timeout=timeout) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read())
        except Exception:
            return e.code, None
    except Exception:
        return None, None


def load_recipe(path):
    """读配方并做 schema 校验(设计文档 §4.3);任何非法返回 None(调用方 exit 4)。"""
    try:
        d = json.loads(Path(path).read_text(encoding="utf-8"))
    except Exception:
        return None
    if not isinstance(d, dict):
        return None
    sets = d.get("set", [])
    if not isinstance(sets, list) or not all(isinstance(c, str) and c.strip() for c in sets):
        return None
    streams = d.get("streams", [])
    if not isinstance(streams, list):
        return None
    for s in streams:
        if (not isinstance(s, (list, tuple)) or len(s) != 2
                or not isinstance(s[0], str) or not s[0].strip()
                or not isinstance(s[1], (int, float)) or isinstance(s[1], bool)):
            return None
    cap = d.get("capture_s", 0)
    if not isinstance(cap, (int, float)) or isinstance(cap, bool) or cap < 0:
        return None
    return {
        "label": str(d.get("label", "run")),
        "set": [c.strip() for c in sets],
        "streams": [[s[0].strip(), int(s[1])] for s in streams],
        "capture_s": float(cap),
        "report": bool(d.get("report", False)),
    }


def do_recipe(recipe_path, json_mode=False):
    """--do 主流程。退出码 0/2/3/4/5 见模块 docstring。"""
    def human(msg):
        print(msg, file=sys.stderr if json_mode else sys.stdout)

    result = {"ok": False, "label": None}

    def finish(code):
        result["exit"] = code
        result["ok"] = (code == 0)
        if json_mode:
            json.dump(result, sys.stdout, ensure_ascii=False, default=str)
            sys.stdout.write("\n")
            sys.stdout.flush()
        return code

    rec = load_recipe(recipe_path)
    if rec is None:
        human(f"[FAIL] 配方非法: {recipe_path}")
        result["error"] = "invalid recipe"
        return finish(4)
    result["label"] = rec["label"]

    # 铁律 + 护栏预检(P0-2): 黑名单/值域/步长, schema 驱动
    g = guard_check(rec["set"])
    if not g["ok"]:
        human(f"[REJECT] 护栏拒绝: {json.dumps(g['violations'], ensure_ascii=False)}")
        result["error"] = "guardrail violation"
        result["violations"] = g["violations"]
        audit_event("precheck", rec["set"], "rejected",
                    {"violations": g["violations"]})
        return finish(5)
    result["guard"] = {"guard_active": bool(g.get("guard_active"))}
    # 趟前参数快照(回滚用;从护栏解析结果提取参数名)
    param_names = [p.get("param") for p in g.get("per_command", [])
                   if p.get("param")]
    snapshot = (snapshot_current_params(param_names)
                if param_names and rec["capture_s"] > 0 else {})

    st = http_get("/status")
    if not st:
        human("[FAIL] 桥不在(YawTuningTool.exe 没开?)")
        result["error"] = "bridge unreachable"
        return finish(3)
    if not st.get("connections"):
        human("[FAIL] 车没连上来(车端电源/wifi)")
        result["error"] = "no car connection"
        return finish(3)

    # set 经 /batch 一次下发(ACK 验证,stop_on_error:第一条失败即止)
    if rec["set"]:
        code, body = http_batch([{"cmd": c, "expect": "ACK"} for c in rec["set"]],
                                stop_on_error=True)
        if body is None or "results" not in body:
            human(f"[FAIL] /batch 调用失败(http={code})")
            result["error"] = "batch call failed"
            return finish(3)
        result["set_results"] = body["results"]
        for r in body["results"]:
            tail = f" ({r['elapsed_ms']}ms)" if "elapsed_ms" in r else ""
            human(f"[set] {r['cmd']}  ->  {r['status']}{tail}")
        bad = [r for r in body["results"] if r["status"] != "ack"]
        if bad:
            human(f"[FAIL] {len(bad)} 条设参未获 ACK(首条: {bad[0]['cmd']} -> {bad[0]['status']})")
            result["error"] = "ack timeout/err"
            # on_apply_fail 回滚(P0-2): 部分失败即恢复趟前值
            guard = G.load_schema_guard(load_schema_dict())
            if guard and guard.get("rollback_on_apply_fail") and snapshot:
                human("[rollback] 设参失败 -> 恢复趟前值")
                ok, detail = restore_params(snapshot)
                result["rollback"] = {"snapshot": snapshot, "restored": ok,
                                      "detail": detail}
                audit_event("rollback", rec["set"], "ok" if ok else "failed",
                            {"triggers": [{"rule": "apply_fail"}],
                             "detail": detail})
            audit_event("set", rec["set"], "failed", {"set_results": bad})
            return finish(2)
        audit_event("set", rec["set"], "ok")

    # streams 订阅(fire-and-forget)
    if rec["streams"]:
        code, body = http_batch(
            [{"cmd": f"START STREAM {name} {period}"} for name, period in rec["streams"]])
        if body is None:
            human(f"[FAIL] 流订阅 /batch 调用失败(http={code})")
            result["error"] = "batch call failed"
            return finish(3)
        for name, period in rec["streams"]:
            human(f"[stream] START STREAM {name} {period}")
    human(f"[armed] label={rec['label']} capture_s={rec['capture_s']}"
          "  —— 发车只能人来,我只看不发。")

    # capture_s=0:只布防;>0:定时捕获 rows(与 --arm 同 row 形状)
    if rec["capture_s"] > 0:
        rows = []
        t0 = time.time()
        while time.time() - t0 < rec["capture_s"]:
            te = time.time() - t0
            d = http_get("/latest") or {}
            pk = d.get("packets", {})
            m = pk.get("TELMISSION", {})
            dr = pk.get("TELDRIVE", {})
            s = pk.get("TELSTATS", {})
            row = {"te": round(te, 2)}
            row.update({k: m.get(k) for k in (
                "ms", "m_st", "m_eng", "m_idx", "m_pts", "m_xte", "m_yer",
                "m_str", "m_ff", "m_spd", "m_dst", "m_lost", "m_lostsrc",
                "m_brk", "m_cpx", "m_cpy", "m_tpx", "m_tpy", "m_rev")})
            row.update({k: dr.get(k) for k in ("d_tgt", "d_mL", "d_mR",
                                               "d_dL", "d_dR")})
            row.update({k: s.get(k) for k in ("fs", "alat", "adla", "adlt", "slew")})
            rows.append(row)
            time.sleep(0.1)
        out = APP / "recordings" / f"run_{time.strftime('%Y%m%d-%H%M%S')}_{rec['label']}.json"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps({"label": rec["label"], "sets": rec["set"],
                                   "rows": rows}, ensure_ascii=False),
                       encoding="utf-8")
        human(f"[saved] {out}  ({len(rows)} samples)")
        result["capture"] = {"samples": len(rows), "saved": str(out)}
        if rec["report"]:
            result["report"] = report_metrics(rows)
            sc = score_metrics(result["report"], find_profile())
            if sc is not None:
                result["score"] = sc
                human(f"[score] {sc['score']}/100  pass={sc['pass']}")
            record_run({"run_id": f"{time.strftime('%Y%m%d-%H%M%S')}_{rec['label']}",
                        "ts": time.time(), "label": rec["label"], "sets": rec["set"],
                        "capture_file": str(out), "metrics": result["report"],
                        "score": sc})
            if not json_mode:
                report_rows(rows, rec["label"])
            # P0-2 回滚判定: score/指标超限 -> 恢复趟前值
            guard = G.load_schema_guard(load_schema_dict())
            triggers = G.check_metrics(result["report"],
                                       sc["score"] if sc is not None else None,
                                       guard)
            if triggers and snapshot:
                human(f"[rollback] 触发: {json.dumps(triggers, ensure_ascii=False)}")
                ok, detail = restore_params(snapshot)
                result["rollback"] = {"snapshot": snapshot, "triggers": triggers,
                                      "restored": ok, "detail": detail}
                audit_event("rollback", rec["set"], "ok" if ok else "failed",
                            {"triggers": triggers, "detail": detail})
    return finish(0)


# ---------------------------------------------------------------- score (P1)
def score_file(capture_path, profile_path=None, json_mode=False):
    """--score:对已存捕获重算 metrics+score。退出码 0 成功 / 4 输入非法。"""
    try:
        d = json.loads(Path(capture_path).read_text(encoding="utf-8"))
        rows = d["rows"]
    except Exception:
        print(f"[FAIL] 捕获文件非法: {capture_path}", file=sys.stderr)
        return 4
    metrics = report_metrics(rows)
    if metrics is None:
        print("[FAIL] engaged 帧不足,无法评分", file=sys.stderr)
        return 4
    sc = score_metrics(metrics, find_profile(profile_path))
    out = {"ok": True, "file": capture_path, "label": d.get("label"),
           "metrics": metrics, "score": sc}
    if json_mode:
        json.dump(out, sys.stdout, ensure_ascii=False, default=str)
        sys.stdout.write("\n")
    else:
        report_rows(rows, d.get("label", "?"))
        if sc is None:
            print("[score] 无 profile,未评分")
        else:
            print(f"[score] {sc['score']}/100  pass={sc['pass']}")
            for b in sc["breakdown"]:
                flag = "" if b.get("ok", True) else "  <-- 未达标"
                print(f"    {b['key']:<22} value={b.get('value')}  "
                      f"contrib={b.get('contrib')}{flag}")
    return 0



# ---------------------------------------------------------------- diff (P0-3)
def diff_runs(path_a, path_b, json_mode=False):
    """--diff:两个 capture 文件逐键 diff(全局指标 + 段级)。退出码 0/4。"""
    def load(p):
        d = json.loads(Path(p).read_text(encoding="utf-8"))
        return d, report_metrics(d["rows"])

    try:
        (da, ma), (db, mb) = load(path_a), load(path_b)
    except Exception:
        print("[FAIL] 捕获文件非法", file=sys.stderr)
        return 4
    if ma is None or mb is None:
        print("[FAIL] engaged 帧不足,无法 diff", file=sys.stderr)
        return 4
    numeric_keys = ["rev", "dstr_deg_per_frame", "ff_fight_pct", "xte_mean_m",
                    "xte_max_m", "gnd_mean_ms", "gnd_p95_ms", "spd_cmd_mean_ms",
                    "duty_sat_pct", "end_idx", "end_pts"]
    diffs = []
    for k in numeric_keys:
        va, vb = ma.get(k), mb.get(k)
        if va is None and vb is None:
            continue
        if va is None or vb is None:
            diffs.append({"key": k, "a": va, "b": vb, "delta": None})
            continue
        diffs.append({"key": k, "a": va, "b": vb,
                      "delta": round(vb - va, 4),
                      "pct": round(100.0 * (vb - va) / abs(va), 1) if va else None})
    # 段级 diff(P0-3): 同段数时逐段逐键相减
    seg_diffs = []
    sa = (ma.get("segments") or {}).get("metrics", [])
    sb = (mb.get("segments") or {}).get("metrics", [])
    if sa and sb and len(sa) == len(sb):
        for x, y in zip(sa, sb):
            row = {"seg": x.get("seg")}
            for k in ("xte_mean_m", "xte_max_m", "rev", "dstr_deg_per_frame",
                      "duty_sat_pct", "gnd_mean_ms"):
                if x.get(k) is not None and y.get(k) is not None:
                    row[k] = round(y[k] - x[k], 4)
            seg_diffs.append(row)
    out = {"ok": True, "a": path_a, "b": path_b,
           "labels": [da.get("label"), db.get("label")],
           "metrics_a": {k: ma.get(k) for k in numeric_keys},
           "metrics_b": {k: mb.get(k) for k in numeric_keys},
           "diffs": diffs, "segment_diffs": seg_diffs,
           "score": {"a": score_metrics(ma, find_profile()),
                     "b": score_metrics(mb, find_profile())}}
    if json_mode:
        json.dump(out, sys.stdout, ensure_ascii=False, default=str)
        sys.stdout.write("\n")
    else:
        print(f"[diff] {da.get('label', '?')}  vs  {db.get('label', '?')}")
        for d in diffs:
            pct = f" ({d['pct']:+.1f}%)" if d.get("pct") is not None else ""
            print(f"  {d['key']:<22} {d['a']} -> {d['b']}  delta={d['delta']}{pct}")
        if seg_diffs:
            print(f"[segments] {len(seg_diffs)} 段逐段 delta(b-a):")
            worst = max(seg_diffs, key=lambda r: r.get("xte_mean_m", 0)
                        if r.get("xte_mean_m") is not None else -9)
            print(f"  最差段 seg={worst['seg']} xte delta={worst.get('xte_mean_m')}")
    return 0


# ---------------------------------------------------------------- sweep (P2)
def load_sweep(path):
    """读 sweep 文件并校验(设计文档 §3.2);任何非法返回 None(调用方 exit 4)。"""
    try:
        d = json.loads(Path(path).read_text(encoding="utf-8"))
    except Exception:
        return None
    if not isinstance(d, dict):
        return None
    sweep = d.get("sweep")
    if not isinstance(sweep, list) or not sweep:
        return None
    entries = []
    for e in sweep:
        if (not isinstance(e, dict)
                or not isinstance(e.get("name"), str) or not e["name"].strip()
                or not isinstance(e.get("template"), str) or "{value}" not in e["template"]
                or not isinstance(e.get("values"), list) or not e["values"]):
            return None
        entries.append({"name": e["name"].strip(), "template": e["template"],
                        "values": e["values"]})
    base_set = d.get("base_set", [])
    if not isinstance(base_set, list) or not all(isinstance(c, str) and c.strip()
                                                 for c in base_set):
        return None
    streams = d.get("streams", [])
    if not isinstance(streams, list):
        return None
    for s in streams:
        if (not isinstance(s, (list, tuple)) or len(s) != 2
                or not isinstance(s[0], str) or not s[0].strip()
                or not isinstance(s[1], (int, float)) or isinstance(s[1], bool)):
            return None
    es = d.get("early_stop", {}) or {}
    cw = es.get("consecutive_worse", 0)
    if not isinstance(cw, int) or isinstance(cw, bool) or cw < 0:
        return None
    return {
        "label": str(d.get("label", "sweep")),
        "base_set": [c.strip() for c in base_set],
        "streams": [[s[0].strip(), int(s[1])] for s in streams],
        "sweep": entries,
        "max_wait_s": float(d.get("max_wait_s", 300.0)),
        "max_run_s": float(d.get("max_run_s", 120.0)),
        "consecutive_worse": cw,
        "report": bool(d.get("report", True)),
    }


def expand_grid(entries):
    """sweep 条目笛卡尔积 -> [{name: value, ...}, ...](保持 values 给定顺序)。"""
    names = [e["name"] for e in entries]
    return [dict(zip(names, combo))
            for combo in itertools.product(*[e["values"] for e in entries])]


def do_sweep(sweep_path, json_mode=False):
    """--sweep 主流程(设计文档 §3.2)。退出码沿用 --do 语义。"""
    def human(msg):
        print(msg, file=sys.stderr if json_mode else sys.stdout)

    result = {"ok": False, "label": None, "runs": []}

    def finish(code):
        result["exit"] = code
        result["ok"] = (code == 0)
        if json_mode:
            json.dump(result, sys.stdout, ensure_ascii=False, default=str)
            sys.stdout.write("\n")
            sys.stdout.flush()
        return code

    spec = load_sweep(sweep_path)
    if spec is None:
        human(f"[FAIL] sweep 文件非法: {sweep_path}")
        result["error"] = "invalid sweep"
        return finish(4)
    result["label"] = spec["label"]

    combos = expand_grid(spec["sweep"])
    all_sets = list(spec["base_set"])
    for e in spec["sweep"]:
        all_sets += [e["template"].replace("{value}", str(v)) for v in e["values"]]
    # 铁律 + 护栏预检(P0-2): 覆盖本 sweep 所有可能命令
    g_all = guard_check(all_sets)
    if not g_all["ok"]:
        human(f"[REJECT] 护栏拒绝: {json.dumps(g_all['violations'], ensure_ascii=False)}")
        result["error"] = "guardrail violation"
        result["violations"] = g_all["violations"]
        audit_event("precheck", all_sets, "rejected",
                    {"violations": g_all["violations"]})
        return finish(5)
    result["guard"] = {"guard_active": bool(g_all.get("guard_active"))}

    st = http_get("/status")
    if not st or not st.get("connections"):
        human("[FAIL] 桥/车不在,先 --bench-check")
        result["error"] = "bridge unreachable" if not st else "no car connection"
        return finish(3)

    profile = find_profile()
    best = None
    worse_streak = 0
    for i, combo in enumerate(combos):
        combo_sets = list(spec["base_set"]) + [
            e["template"].replace("{value}", str(combo[e["name"]]))
            for e in spec["sweep"]]
        tag = "_".join(f"{k}{v}" for k, v in combo.items())
        run_id = f"{time.strftime('%Y%m%d-%H%M%S')}_{spec['label']}_{tag}"
        human(f"[sweep {i + 1}/{len(combos)}] {combo} -> 下发参数")
        snapshot = snapshot_current_params([e["name"] for e in spec["sweep"]])
        code, body = http_batch([{"cmd": c, "expect": "ACK"} for c in combo_sets],
                                stop_on_error=True)
        run_rec = {"run_id": run_id, "ts": time.time(), "label": spec["label"],
                   "sets": combo_sets, "sweep": combo}
        if body is None or "results" not in body:
            human(f"  [FAIL] /batch 调用失败(http={code}),本趟跳过")
            run_rec["skipped"] = "batch failed"
            record_run(run_rec)
            result["runs"].append(run_rec)
            if i == 0:  # 首趟链路级失败,整体按链路失败退出
                result["error"] = "batch call failed"
                return finish(3)
            continue
        bad = [r for r in body["results"] if r["status"] != "ack"]
        if bad:
            human(f"  [FAIL] 设参未获 ACK(首条: {bad[0]['cmd']} -> {bad[0]['status']}),本趟跳过")
            run_rec["skipped"] = "ack timeout/err"
            run_rec["set_results"] = body["results"]
            record_run(run_rec)
            result["runs"].append(run_rec)
            if i == 0:
                result["error"] = "ack timeout/err"
                return finish(2)
            continue
        if spec["streams"]:
            http_batch([{"cmd": f"START STREAM {n} {p}"} for n, p in spec["streams"]])
        human(f"  [armed] 等人按菜单发车(max_wait={spec['max_wait_s']:.0f}s),我只看不发。")
        rows = capture_run(spec["max_wait_s"], spec["max_run_s"])
        eng_n = sum(1 for r in rows if r.get("m_eng") == 1)
        if eng_n < 5:
            human("  [skip] 没跑起来(engaged 帧不足),本趟记为 no launch")
            run_rec["skipped"] = "no launch"
            record_run(run_rec)
            result["runs"].append(run_rec)
            continue
        out = APP / "recordings" / f"run_{run_id}.json"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps({"label": run_id, "sets": combo_sets,
                                   "rows": rows}, ensure_ascii=False),
                       encoding="utf-8")
        metrics = report_metrics(rows) if spec["report"] else None
        sc = score_metrics(metrics, profile)
        run_rec.update({"capture_file": str(out), "metrics": metrics, "score": sc})
        record_run(run_rec)
        result["runs"].append(run_rec)
        human(f"  [done] samples={len(rows)}"
              + (f"  score={sc['score']}/100 pass={sc['pass']}" if sc else ""))
        audit_event("set", combo_sets, "ok",
                    {"run_id": run_id, "sweep": combo})
        # P0-2 回滚判定
        guard = G.load_schema_guard(load_schema_dict())
        triggers = G.check_metrics(metrics, sc["score"] if sc is not None else None,
                                   guard)
        if triggers and snapshot:
            human(f"  [rollback] 触发: {json.dumps(triggers, ensure_ascii=False)}")
            ok, detail = restore_params(snapshot)
            run_rec["rollback"] = {"snapshot": snapshot, "triggers": triggers,
                                   "restored": ok, "detail": detail}
            audit_event("rollback", combo_sets, "ok" if ok else "failed",
                        {"triggers": triggers, "detail": detail,
                         "run_id": run_id})
        # 早停:score 相对历史最佳连续变差 N 趟 -> 中止(设计文档 §3.2)
        if sc is not None:
            if best is None or sc["score"] > best:
                best = sc["score"]
                worse_streak = 0
            else:
                worse_streak += 1
                if (spec["consecutive_worse"] > 0
                        and worse_streak >= spec["consecutive_worse"]):
                    human(f"  [early-stop] score 连续 {worse_streak} 趟劣于最佳 "
                          f"{best},中止本次 sweep")
                    result["early_stopped"] = True
                    break
    result["best_score"] = best
    return finish(0)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--bench-check", action="store_true")
    ap.add_argument("--arm", action="store_true")
    ap.add_argument("--label", default="run")
    ap.add_argument("--set", action="append", default=[],
                    help="发车前设参命令(可多个);发车/录制类一律拒绝")
    ap.add_argument("--report", help="重打印已存捕获的报告")
    ap.add_argument("--do", metavar="RECIPE.json",
                    help="按配方执行:set 经 /batch 一次下发(ACK 验证)+ streams 订阅"
                         "(+ capture_s>0 定时捕获)")
    ap.add_argument("--json", action="store_true",
                    help="--do/--sweep/--score 模式下结构化结果走 stdout,人类可读文本走 stderr")
    ap.add_argument("--score", metavar="RUN.json",
                    help="对已存捕获重算 metrics+score(P1 复评历史趟)")
    ap.add_argument("--profile", metavar="PROFILE.json",
                    help="score profile 路径(缺省 app_dir/score_profile.json)")
    ap.add_argument("--sweep", metavar="SWEEP.json",
                    help="参数网格扫描(P2):每趟 下发参数→布防等发车→评分→runs.jsonl")
    ap.add_argument("--optimize", metavar="OPT.json",
                    help="TPE 贝叶斯优化(P0-1):预算感知顺序提议,替代网格扫描")
    ap.add_argument("--diff", metavar="A.json B.json", nargs=2,
                    help="两个捕获文件逐键 diff(全局指标+段级, P0-3)")
    a = ap.parse_args()
    if a.bench_check:
        sys.exit(bench_check())
    if a.report:
        d = json.loads(Path(a.report).read_text(encoding="utf-8"))
        report_rows(d["rows"], d.get("label", "?"))
        sys.exit(0)
    if a.score:
        sys.exit(score_file(a.score, profile_path=a.profile, json_mode=a.json))
    if a.sweep:
        sys.exit(do_sweep(a.sweep, json_mode=a.json))
    if a.optimize:
        sys.exit(do_optimize(a.optimize, json_mode=a.json))
    if a.diff:
        sys.exit(diff_runs(a.diff[0], a.diff[1], json_mode=a.json))
    if a.do:
        sys.exit(do_recipe(a.do, json_mode=a.json))
    if a.arm:
        sys.exit(arm(a.label, a.set))
    ap.print_help()


if __name__ == "__main__":
    main()
