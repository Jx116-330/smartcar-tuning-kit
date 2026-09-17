# -*- coding: utf-8 -*-
"""archive.py — 调车轨迹/跑趟分类归档系统 (P1, 2026-07-02)

核心思想:归档跟着数据流走(tail telemetry_history.jsonl),不跟操作入口走 —— 菜单/TCP/
param_sweep 发的车只要 PC 连着桥就全被收编;回填历史 = 对旧文件跑同一个状态机。

目录:
  archive/lines/<fp8>_<nick>/line.json        录制线(指纹=点数+里程跨度+首末点坐标 hash)
  archive/runs/<日期>/<时刻>_<ST>_<fp8>/       每趟: meta.json(全参数快照+指标) + frames.jsonl
  archive/index.jsonl                          趟平表(grep/pandas 直接用)
  archive/lines.jsonl                          线平表
  archive/state.json                           当前 RAM 线 + watch 断点

用法:
  python archive.py watch                      # 实时看守(断点续传,只读不发命令)
  python archive.py watch --backfill           # 从头回填整个 telemetry_history.jsonl
  python archive.py ingest                     # 收编 recordings/ 存量(learn_dump + run_*.json + *.csv)
  python archive.py lines                      # 列所有线
  python archive.py list [--date D] [--line FP] [--preset FAST] [--st EVENT1] [--param fs=3.0]
  python archive.py compare <id片段> <id片段>...  # 多趟 A/B 指标对比(跨线会警告)
  python archive.py session-summary [--date D] # 当日趟表(markdown,喂 PLAYBOOK §9)

约定:watch 是只读消费者,绝不发 TCP 命令(监控≠发车/不污染 cmd_result)。
"""
import argparse
import csv as csv_mod
import glob
import hashlib
import json
import os
import re
import statistics
import sys
import time
from pathlib import Path

APP = Path(__file__).resolve().parent
ARCHIVE = APP / "archive"
LINES_DIR = ARCHIVE / "lines"
RUNS_DIR = ARCHIVE / "runs"
INDEX = ARCHIVE / "index.jsonl"
LINES_IDX = ARCHIVE / "lines.jsonl"
STATE = ARCHIVE / "state.json"
HISTORY_DEFAULT = APP / "dist" / "runtime" / "telemetry_history.jsonl"

ST_NAME = {0: "IDLE", 1: "EVENT1", 2: "LEARN", 3: "RETURN", 4: "ABORT"}
LOST_BITS = [(1, "BRAKE"), (2, "DIRKEY"), (4, "WATCHDOG"),
             (8, "TCP_DOWN"), (16, "CPU1_DEAD"), (32, "HB_TIMEOUT")]
# (fs, alat, cap) — menu.c SPEED_PLANS 2026-06-20 固化值
PRESETS = {"SAFE": (2.0, 1.6, 2600), "NORM": (2.5, 2.0, 3200), "FAST": (3.0, 2.2, 3800)}
MIN_ENGAGED_FRAMES = 15          # 少于此的 eng 段当抖动丢弃
MS_REBOOT_GAP = 5000             # 趟内 ms 倒退超此值 = 车重启,截断当前趟

# ---------------------------------------------------------------- utils

def ensure_dirs():
    for d in (ARCHIVE, LINES_DIR, RUNS_DIR):
        d.mkdir(parents=True, exist_ok=True)


def load_state():
    if STATE.exists():
        try:
            return json.loads(STATE.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {}


def save_state(st):
    ensure_dirs()
    STATE.write_text(json.dumps(st, ensure_ascii=False, indent=2), encoding="utf-8")


def slugify(text, maxlen=24):
    s = re.sub(r"[^0-9A-Za-z_\-]", "", str(text or ""))
    return (s[:maxlen] or "x")


def q(vals, p):
    if not vals:
        return None
    s = sorted(vals)
    return s[min(len(s) - 1, int(p * (len(s) - 1)))]


def decode_lost(v):
    v = int(v or 0)
    if not v:
        return ""
    return "+".join(name for bit, name in LOST_BITS if v & bit)


def append_jsonl(path, obj):
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(obj, ensure_ascii=False) + "\n")


def read_jsonl(path):
    out = []
    if not path.exists():
        return out
    with open(path, encoding="utf-8") as f:
        for ln in f:
            ln = ln.strip()
            if ln:
                try:
                    out.append(json.loads(ln))
                except Exception:
                    pass
    return out

# ---------------------------------------------------------------- lines

def fingerprint(points):
    """线指纹:点数 + 里程跨度 + 首末点坐标。同内容同 fp,重录必不同(DR 坐标不同)。"""
    n = len(points)
    if n < 2:
        return "00000000"
    span = int(points[-1].get("enc_dist_mm", 0)) - int(points[0].get("enc_dist_mm", 0))
    key = "{}|{}|{:.3f},{:.3f}|{:.3f},{:.3f}".format(
        n, span,
        float(points[0].get("px_m", 0)), float(points[0].get("py_m", 0)),
        float(points[-1].get("px_m", 0)), float(points[-1].get("py_m", 0)))
    return hashlib.sha1(key.encode()).hexdigest()[:8]


def register_line(points, nick, source, origin_file=None, quiet=False):
    """登记一条线(幂等)。返回 fp。"""
    ensure_dirs()
    fp = fingerprint(points)
    nick_slug = slugify(nick)
    existing = list(LINES_DIR.glob(fp + "_*"))
    if existing:
        if not quiet:
            print(f"[line] {fp} 已登记 ({existing[0].name})")
        return fp
    span = int(points[-1].get("enc_dist_mm", 0)) - int(points[0].get("enc_dist_mm", 0))
    d = LINES_DIR / f"{fp}_{nick_slug}"
    d.mkdir(parents=True, exist_ok=True)
    (d / "line.json").write_text(json.dumps({
        "meta": {"fp": fp, "nick": str(nick), "source": source,
                 "origin_file": origin_file,
                 "registered_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                 "n_points": len(points), "length_m": round(span / 1000.0, 2)},
        "points": points,
    }, ensure_ascii=False), encoding="utf-8")
    append_jsonl(LINES_IDX, {"fp": fp, "nick": str(nick), "n": len(points),
                             "length_m": round(span / 1000.0, 2), "source": source,
                             "origin_file": origin_file,
                             "registered_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                             "dir": str(d.relative_to(ARCHIVE))})
    if not quiet:
        print(f"[line] 登记 {fp}_{nick_slug} n={len(points)} len={span/1000.0:.1f}m")
    return fp


def set_active_line(fp, nick, n_points, source):
    """track_dump / track_upload 成功后调用:记录'车上 RAM 线现在是谁'。"""
    st = load_state()
    st["active_line"] = {"fp": fp, "nick": str(nick), "n": int(n_points),
                         "source": source,
                         "set_at": time.strftime("%Y-%m-%d %H:%M:%S")}
    save_state(st)
    print(f"[state] 当前 RAM 线 = {fp} ({nick}, {n_points}pts, via {source})")


def lines_by_count():
    by = {}
    for e in read_jsonl(LINES_IDX):
        by.setdefault(int(e.get("n", 0)), []).append(e)
    return by

# ---------------------------------------------------------------- metrics

def run_metrics(eng):
    """eng = 引导态 TELMISSION 帧 dict 列表(m_* 键,容缺)。"""
    def col(k):
        return [f[k] for f in eng if isinstance(f.get(k), (int, float))]
    spd = col("m_spd")
    xte = [abs(v) for v in col("m_xte")]
    yer = [abs(v) for v in col("m_yer")]
    strv = col("m_str")
    ms = col("ms")
    revs = sum(1 for f in eng if f.get("m_rev") == 1)
    dstr = [strv[i + 1] - strv[i] for i in range(len(strv) - 1)]
    sig = [d for d in dstr if abs(d) > 0.3]
    flips = sum(1 for i in range(len(sig) - 1) if (sig[i] > 0) != (sig[i + 1] > 0))
    dur = (max(ms) - min(ms)) / 1000.0 if len(ms) >= 2 else len(eng) / 10.0
    idx_last = next((f.get("m_idx") for f in reversed(eng)
                     if isinstance(f.get("m_idx"), int)), None)
    pts = next((f.get("m_pts") for f in reversed(eng)
                if isinstance(f.get("m_pts"), int) and f.get("m_pts")), None)
    lost = max([int(f.get("m_lostsrc") or 0) for f in eng] or [0])
    completed = None
    if pts and isinstance(idx_last, int):
        completed = bool(idx_last >= pts - 3)
    m = {
        "n_frames": len(eng), "dur_s": round(dur, 1),
        "spd_mean": round(statistics.mean(spd), 0) if spd else None,
        "spd_max": max(spd) if spd else None,
        "xte_med": round(statistics.median(xte), 3) if xte else None,
        "xte_p95": round(q(xte, 0.95), 3) if xte else None,
        "xte_max": round(max(xte), 3) if xte else None,
        "yer_p95": round(q(yer, 0.95), 1) if yer else None,
        "str_flips": flips,
        "dstr_max": round(max(abs(d) for d in dstr), 1) if dstr else None,
        "rev_frames": revs,
        "idx_last": idx_last, "pts": pts,
        "completed": completed,
        "lostsrc": lost, "lost_str": decode_lost(lost),
    }
    return m


def params_from_frames(frames_all, eng):
    """参数快照:趟内最后一帧 TELSTATS + 引导帧 TELMISSION 回显。缺=unknown,不装全知。"""
    p = {}
    stats_keys = ("fs", "alat", "adla", "adlt", "slew", "diffg", "dsat",
                  "srlim", "ffpre", "sbrk", "rffg", "ukv", "yrkp", "ffcap")
    last_stats = None
    for ts, pk, parsed in frames_all:
        if pk == "TELSTATS":
            last_stats = parsed
    if last_stats:
        for k in stats_keys:
            if k in last_stats:
                p[k] = last_stats[k]
    if eng:
        f = eng[-1]
        for src, dst in (("m_kp", "kp"), ("m_cap", "cap"),
                         ("m_la", "la"), ("m_blnd", "blnd")):
            if isinstance(f.get(src), (int, float)):
                p[dst] = f[src]
    return p


def infer_preset(p):
    fs, alat, cap = p.get("fs"), p.get("alat"), p.get("cap")
    if fs is None:
        return "unknown"
    for name, (f0, a0, c0) in PRESETS.items():
        if abs(fs - f0) < 0.01 and (alat is None or abs(alat - a0) < 0.01) \
                and (cap is None or int(cap) == c0):
            return name
    return "custom"


def attribute_line(rec_pts, run_wallclock, state, by_count):
    """线归属:state(需在 run 之前设置且点数吻合) > 点数唯一推断 > unknown。"""
    act = (state or {}).get("active_line")
    if act and act.get("set_at", "9999") <= run_wallclock and rec_pts \
            and int(act.get("n", -1)) == int(rec_pts):
        return act["fp"], act.get("nick", ""), "verified"
    if rec_pts:
        cands = by_count.get(int(rec_pts), [])
        if len(cands) == 1:
            return cands[0]["fp"], cands[0].get("nick", ""), "inferred"
    return None, "", "unknown"

# ---------------------------------------------------------------- run write

def _near_dup(date_s, time_s, st_name):
    """同日同类型、时刻差 ≤5s 视为同一趟(CSV 收编与历史回填对同趟的时间戳差 ±1s)。"""
    try:
        t0 = int(time_s[:2]) * 3600 + int(time_s[2:4]) * 60 + int(time_s[4:6])
    except ValueError:
        return None
    for r in read_jsonl(INDEX):
        if r.get("date") != date_s or r.get("st") != st_name:
            continue
        t1 = int(r["time"][:2]) * 3600 + int(r["time"][2:4]) * 60 + int(r["time"][4:6])
        if abs(t1 - t0) <= 5:
            return r
    return None


def write_run(date_s, time_s, st_name, frames_all, eng, source, label=""):
    """frames_all=[(ts,pk,parsed)] 趟窗口内全部包;eng=引导 TELMISSION 帧。返回 index 行或 None。"""
    ensure_dirs()
    if _near_dup(date_s, time_s, st_name) is not None:
        return None
    metrics = run_metrics(eng)
    params = params_from_frames(frames_all, eng)
    preset = infer_preset(params)
    rec_pts = next((f.get("m_rec_pts") for f in reversed(eng)
                    if isinstance(f.get("m_rec_pts"), int) and f.get("m_rec_pts")), None)
    wall = f"{date_s} {time_s[:2]}:{time_s[2:4]}:{time_s[4:6]}"
    fp, nick, conf = attribute_line(rec_pts, wall, load_state(), lines_by_count())
    run_id = f"{date_s}/{time_s}_{st_name}_{fp or 'unk'}"
    d = RUNS_DIR / date_s / f"{time_s}_{st_name}_{fp or 'unk'}"
    if d.exists():
        return None                                    # 幂等:重复回填跳过
    d.mkdir(parents=True, exist_ok=True)
    entry = {
        "id": run_id, "date": date_s, "time": time_s, "st": st_name,
        "source": source, "label": label,
        "line_fp": fp, "line_nick": nick, "line_conf": conf, "rec_pts": rec_pts,
        "preset": preset, "params": params, "metrics": metrics,
        "dir": str(d.relative_to(ARCHIVE)),
    }
    (d / "meta.json").write_text(json.dumps(entry, ensure_ascii=False, indent=2),
                                 encoding="utf-8")
    with open(d / "frames.jsonl", "w", encoding="utf-8") as f:
        for ts, pk, parsed in frames_all:
            f.write(json.dumps({"ts": ts, "p": parsed}, ensure_ascii=False) + "\n")
    append_jsonl(INDEX, entry)
    return entry

# ---------------------------------------------------------------- watch

def _finalize(cur, source):
    eng = [p for (ts, pk, p) in cur["frames"]
           if pk == "TELMISSION" and p.get("m_eng") == 1]
    if len(eng) < MIN_ENGAGED_FRAMES:
        return None
    sts = [p.get("m_st") for p in eng if p.get("m_st") in (1, 2, 3)]
    st_name = ST_NAME.get(statistics.mode(sts) if sts else -1, "UNK")
    ts0 = cur["t0"]                                   # "YYYY-MM-DD HH:MM:SS"
    date_s, hms = ts0[:10], ts0[11:19].replace(":", "")
    e = write_run(date_s, hms, st_name, cur["frames"], eng, source)
    if e:
        m = e["metrics"]
        print(f"[run] {e['id']} line={e['line_fp'] or '?'}({e['line_conf']}) "
              f"preset={e['preset']} dur={m['dur_s']}s xte={m['xte_med']}/{m['xte_max']} "
              f"done={m['completed']} lost={m['lost_str'] or '-'}")
    return e


def watch(history_path, backfill=False, follow=None):
    ensure_dirs()
    hp = Path(history_path)
    if not hp.exists():
        print(f"[ERR] 找不到 {hp}")
        return 1
    st = load_state()
    wkey = st.get("watch", {})
    offset = 0
    if not backfill and wkey.get("path") == str(hp):
        offset = int(wkey.get("offset", 0))
        if offset > hp.stat().st_size:
            offset = 0                                 # 文件被截断/轮转,从头
    if follow is None:
        follow = not backfill
    print(f"[watch] {hp.name} from offset {offset/1e6:.1f}MB "
          f"(size {hp.stat().st_size/1e6:.1f}MB) backfill={backfill} follow={follow}")

    cur = None
    n_runs = 0
    partial = b""
    f = open(hp, "rb")
    f.seek(offset)
    last_report = time.time()
    try:
        while True:
            chunk = f.read(8 * 1024 * 1024)
            if not chunk:
                if cur is None:
                    # 干净断点:存 offset
                    st = load_state()
                    st["watch"] = {"path": str(hp), "offset": f.tell() - len(partial)}
                    save_state(st)
                if not follow:
                    break
                time.sleep(1.0)
                continue
            buf = partial + chunk
            lines = buf.split(b"\n")
            partial = lines.pop()                      # 末段可能是半行
            for raw in lines:
                raw = raw.strip()
                if not raw:
                    continue
                # 提速:不在趟内时只解析 TELMISSION 行
                if cur is None and b'"TELMISSION"' not in raw:
                    continue
                try:
                    obj = json.loads(raw)
                except Exception:
                    continue
                p = obj.get("parsed") or {}
                pk = p.get("_packet", "?")
                ts = obj.get("timestamp", "")
                if pk == "TELMISSION":
                    eng = p.get("m_eng")
                    ms = p.get("ms")
                    if cur is not None:
                        lm = cur.get("last_ms")
                        if (isinstance(ms, int) and isinstance(lm, int)
                                and ms < lm - MS_REBOOT_GAP):
                            if _finalize(cur, cur["src"]):
                                n_runs += 1            # 车重启截断
                            cur = None
                        elif eng == 0:
                            if _finalize(cur, cur["src"]):
                                n_runs += 1
                            cur = None
                    if eng == 1 and cur is None:
                        cur = {"t0": ts, "frames": [], "last_ms": None,
                               "src": "backfill" if backfill else "watch"}
                    if cur is not None and isinstance(ms, int):
                        cur["last_ms"] = ms
                if cur is not None:
                    cur["frames"].append((ts, pk, p))
            if backfill and time.time() - last_report > 5:
                print(f"  ... {f.tell()/1e6:.0f}MB, {n_runs} runs")
                last_report = time.time()
    except KeyboardInterrupt:
        print("\n[watch] 中断")
    if cur is not None:
        if _finalize(cur, cur["src"] + ":truncated"):
            n_runs += 1
    st = load_state()
    st["watch"] = {"path": str(hp), "offset": f.tell() - len(partial)}
    save_state(st)
    f.close()
    print(f"[watch] 结束:归档 {n_runs} 趟,断点 {st['watch']['offset']/1e6:.1f}MB")
    return 0

# ---------------------------------------------------------------- ingest

def ingest_recordings(rec_dir):
    rec = Path(rec_dir)
    n_lines = n_runs = 0
    # 1) 录制线
    for fp_ in sorted(rec.glob("learn_dump_*.json")):
        try:
            d = json.loads(fp_.read_text(encoding="utf-8"))
        except Exception as e:
            print(f"[skip] {fp_.name}: {e}")
            continue
        pts = d.get("points") or []
        if len(pts) < 2:
            continue
        m = re.match(r"learn_dump_\d{8}-\d{6}_?(.*)\.json", fp_.name)
        nick = (m.group(1) if m and m.group(1) else fp_.stem)
        register_line(pts, nick, "ingest", origin_file=fp_.name, quiet=True)
        n_lines += 1
    # 2) session_driver 的 run_*.json ({label, sets, rows})
    for fp_ in sorted(rec.glob("run_*.json")):
        try:
            d = json.loads(fp_.read_text(encoding="utf-8"))
            rows = d.get("rows") or []
        except Exception:
            continue
        eng = [r for r in rows if r.get("m_eng") == 1]
        if len(eng) < MIN_ENGAGED_FRAMES:
            continue
        m = re.match(r"run_(\d{8})-(\d{6})_?(.*)\.json", fp_.name)
        if not m:
            continue
        date_s = f"{m.group(1)[:4]}-{m.group(1)[4:6]}-{m.group(1)[6:]}"
        sts = [r.get("m_st") for r in eng if r.get("m_st") in (1, 2, 3)]
        st_name = ST_NAME.get(statistics.mode(sts) if sts else -1, "UNK")
        frames_all = [("", "TELMISSION", r) for r in rows]
        if write_run(date_s, m.group(2), st_name, frames_all, eng,
                     f"ingest:{fp_.name}", label=m.group(3) or d.get("label", "")):
            n_runs += 1
    # 3) 老 CSV (t,m_st,m_idx,m_xte,m_yer,m_spd,m_str,dL,dR,lostsrc,eng)
    for fp_ in sorted(rec.glob("*.csv")):
        m = re.match(r"(.*)_(\d{8})-(\d{6})\.csv", fp_.name)
        if not m:
            continue
        rows = []
        try:
            with open(fp_, encoding="utf-8") as f:
                for r in csv_mod.DictReader(f):
                    d = {}
                    for k, v in r.items():
                        kk = {"eng": "m_eng", "lostsrc": "m_lostsrc",
                              "dL": "d_dL", "dR": "d_dR"}.get(k, k)
                        try:
                            d[kk] = int(v) if "." not in v else float(v)
                        except (ValueError, TypeError):
                            d[kk] = v
                    if "t" in d:
                        d["ms"] = int(float(d["t"]) * 1000)
                    rows.append(d)
        except Exception as e:
            print(f"[skip] {fp_.name}: {e}")
            continue
        eng = [r for r in rows if r.get("m_eng") == 1]
        if len(eng) < MIN_ENGAGED_FRAMES:
            continue
        date_s = f"{m.group(2)[:4]}-{m.group(2)[4:6]}-{m.group(2)[6:]}"
        sts = [r.get("m_st") for r in eng if r.get("m_st") in (1, 2, 3)]
        st_name = ST_NAME.get(statistics.mode(sts) if sts else -1, "UNK")
        frames_all = [("", "TELMISSION", r) for r in rows]
        if write_run(date_s, m.group(3), st_name, frames_all, eng,
                     f"ingest:{fp_.name}", label=m.group(1)):
            n_runs += 1
    print(f"[ingest] 线 {n_lines} 条(去重后见 lines.jsonl),新归档趟 {n_runs}")
    return 0

# ---------------------------------------------------------------- query

def _fmt(v, w):
    s = "-" if v is None else (f"{v:.3f}" if isinstance(v, float) else str(v))
    return s[:w].ljust(w)


def _src_score(r):
    s = 0 if str(r.get("source", "")).startswith("ingest") else 2
    if r.get("line_conf") in ("verified", "inferred"):
        s += 1
    return s


def cmd_dedupe():
    """近邻去重压实 index:同(日,类型,±5s)保留信息最全的一条,输家目录挪到 trash/。"""
    rows = read_jsonl(INDEX)
    keep = []
    trash = ARCHIVE / "trash"
    n_drop = 0
    for r in rows:
        t0 = int(r["time"][:2]) * 3600 + int(r["time"][2:4]) * 60 + int(r["time"][4:6])
        dup_i = None
        for i, k in enumerate(keep):
            if k["date"] != r["date"] or k["st"] != r["st"]:
                continue
            t1 = int(k["time"][:2]) * 3600 + int(k["time"][2:4]) * 60 + int(k["time"][4:6])
            if abs(t1 - t0) <= 5:
                dup_i = i
                break
        if dup_i is None:
            keep.append(r)
            continue
        loser = keep[dup_i] if _src_score(r) > _src_score(keep[dup_i]) else r
        winner = r if loser is keep[dup_i] else keep[dup_i]
        keep[dup_i] = winner
        n_drop += 1
        ld = ARCHIVE / loser["dir"]
        if ld.exists():
            trash.mkdir(parents=True, exist_ok=True)
            tgt = trash / ld.name
            i2 = 0
            while tgt.exists():
                i2 += 1
                tgt = trash / f"{ld.name}.{i2}"
            ld.rename(tgt)
        print(f"[dedupe] 丢弃 {loser['id']} (保留 {winner['id']})")
    tmp = INDEX.with_suffix(".jsonl.tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        for r in keep:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    tmp.replace(INDEX)
    print(f"[dedupe] {len(rows)} -> {len(keep)} 趟(移除 {n_drop},目录在 archive/trash/)")
    return 0


def cmd_list(args):
    rows = sorted(read_jsonl(INDEX), key=lambda r: (r["date"], r["time"]))
    if args.date:
        rows = [r for r in rows if r["date"] == args.date]
    if args.line:
        rows = [r for r in rows if (r.get("line_fp") or "").startswith(args.line)
                or args.line in (r.get("line_nick") or "")]
    if args.preset:
        rows = [r for r in rows if r.get("preset") == args.preset]
    if args.st:
        rows = [r for r in rows if r.get("st") == args.st]
    for cond in (args.param or []):
        k, _, v = cond.partition("=")
        try:
            vf = float(v)
            rows = [r for r in rows
                    if isinstance(r["params"].get(k), (int, float))
                    and abs(r["params"][k] - vf) < 1e-6]
        except ValueError:
            rows = [r for r in rows if str(r["params"].get(k)) == v]
    print(f"{'id':44s} {'line':14s} {'pre':6s} {'dur':>5s} {'spd':>4s} "
          f"{'xte med/p95/max':>18s} {'flp':>3s} {'rev':>3s} done lost")
    for r in rows[-(args.limit or 10**9):]:
        mt = r["metrics"]
        line = f"{(r.get('line_fp') or 'unk')[:8]}:{(r.get('line_conf') or '')[:3]}"
        xte = f"{mt.get('xte_med')}/{mt.get('xte_p95')}/{mt.get('xte_max')}"
        print(f"{r['id'][:44]:44s} {line:14s} {r.get('preset', '?')[:6]:6s} "
              f"{_fmt(mt.get('dur_s'), 5)} {_fmt(mt.get('spd_mean'), 4)} {xte:>18s} "
              f"{_fmt(mt.get('str_flips'), 3)} {_fmt(mt.get('rev_frames'), 3)} "
              f"{'Y' if mt.get('completed') else ('N' if mt.get('completed') is False else '?'):>4s} "
              f"{mt.get('lost_str') or '-'}")
    print(f"({len(rows)} 趟)")
    return 0


def cmd_compare(ids):
    rows = read_jsonl(INDEX)
    sel = []
    for pat in ids:
        hit = [r for r in rows if pat in r["id"] or pat in (r.get("label") or "")]
        if not hit:
            print(f"[WARN] 找不到 '{pat}'")
        else:
            sel.append(hit[-1])
    if len(sel) < 2:
        print("需要至少两趟")
        return 1
    fps = {r.get("line_fp") for r in sel}
    if len(fps) > 1:
        print("⚠⚠ 跨线对比:所选趟不在同一条线上,指标不可直接比!")
        for r in sel:
            print(f"   {r['id']} -> line {r.get('line_fp')}({r.get('line_nick')})")
    keys = [("preset", lambda r: r.get("preset")),
            ("fs", lambda r: r["params"].get("fs")),
            ("alat", lambda r: r["params"].get("alat")),
            ("srlim", lambda r: r["params"].get("srlim")),
            ("ffpre", lambda r: r["params"].get("ffpre")),
            ("ffcap", lambda r: r["params"].get("ffcap")),
            ("kp", lambda r: r["params"].get("kp")),
            ("dur_s", lambda r: r["metrics"].get("dur_s")),
            ("spd_mean", lambda r: r["metrics"].get("spd_mean")),
            ("spd_max", lambda r: r["metrics"].get("spd_max")),
            ("xte_med", lambda r: r["metrics"].get("xte_med")),
            ("xte_p95", lambda r: r["metrics"].get("xte_p95")),
            ("xte_max", lambda r: r["metrics"].get("xte_max")),
            ("str_flips", lambda r: r["metrics"].get("str_flips")),
            ("dstr_max", lambda r: r["metrics"].get("dstr_max")),
            ("rev_frames", lambda r: r["metrics"].get("rev_frames")),
            ("completed", lambda r: r["metrics"].get("completed")),
            ("lost", lambda r: r["metrics"].get("lost_str") or "-")]
    w = 18
    print(f"{'':12s}" + "".join(_fmt(r["id"].split('/')[-1], w) for r in sel))
    for name, fn in keys:
        print(f"{name:12s}" + "".join(_fmt(fn(r), w) for r in sel))
    return 0


def cmd_summary(date_s):
    rows = read_jsonl(INDEX)
    if not rows:
        print("空归档")
        return 1
    if not date_s:
        date_s = max(r["date"] for r in rows)
    day = sorted([r for r in rows if r["date"] == date_s], key=lambda r: r["time"])
    print(f"### {date_s} 跑趟归档({len(day)} 趟)\n")
    print("| 时刻 | 类型 | 线 | 档 | fs/alat | 时长 | xte中/峰 | 完成 | abort |")
    print("|---|---|---|---|---|---|---|---|---|")
    for r in day:
        mt, p = r["metrics"], r["params"]
        t = r["time"]
        print(f"| {t[:2]}:{t[2:4]}:{t[4:6]} | {r['st']} "
              f"| {(r.get('line_nick') or r.get('line_fp') or '?')[:14]}({r.get('line_conf', '?')[:3]}) "
              f"| {r.get('preset')} | {p.get('fs')}/{p.get('alat')} "
              f"| {mt.get('dur_s')}s | {mt.get('xte_med')}/{mt.get('xte_max')} "
              f"| {'✓' if mt.get('completed') else ('✗' if mt.get('completed') is False else '?')} "
              f"| {mt.get('lost_str') or '-'} |")
    return 0


def cmd_lines():
    for e in read_jsonl(LINES_IDX):
        print(f"{e['fp']}  {e.get('nick', ''):24s} n={e.get('n'):4d} "
              f"len={e.get('length_m')}m  src={e.get('source')}  {e.get('origin_file') or ''}")
    return 0

# ---------------------------------------------------------------- main

def main(argv):
    ap = argparse.ArgumentParser(description="调车轨迹/跑趟归档 (P1)")
    sub = ap.add_subparsers(dest="cmd")
    w = sub.add_parser("watch")
    w.add_argument("--history", default=str(HISTORY_DEFAULT))
    w.add_argument("--backfill", action="store_true")
    w.add_argument("--no-follow", action="store_true")
    ing = sub.add_parser("ingest")
    ing.add_argument("--recordings", default=str(APP / "recordings"))
    ls = sub.add_parser("list")
    ls.add_argument("--date")
    ls.add_argument("--line")
    ls.add_argument("--preset")
    ls.add_argument("--st")
    ls.add_argument("--param", action="append")
    ls.add_argument("--limit", type=int, default=40)
    cp = sub.add_parser("compare")
    cp.add_argument("ids", nargs="+")
    sm = sub.add_parser("session-summary")
    sm.add_argument("--date")
    sub.add_parser("lines")
    sub.add_parser("dedupe")
    a = ap.parse_args(argv)
    if a.cmd == "watch":
        return watch(a.history, backfill=a.backfill,
                     follow=(False if a.no_follow else None))
    if a.cmd == "ingest":
        return ingest_recordings(a.recordings)
    if a.cmd == "list":
        return cmd_list(a)
    if a.cmd == "compare":
        return cmd_compare(a.ids)
    if a.cmd == "session-summary":
        return cmd_summary(a.date)
    if a.cmd == "lines":
        return cmd_lines()
    if a.cmd == "dedupe":
        return cmd_dedupe()
    ap.print_help()
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
