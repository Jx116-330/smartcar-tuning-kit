"""
track_upload.py - 把离线优化轨迹写回车端 (TRACK UPLOAD 协议,2026-06-09)

输入:path_pipeline.py 产出的 paths/<label>_upload.json
     ({meta, points:[ins_record_point_t 8 字段]})

流程:
    1. 本地预校验(点数/里程单调/enc_spd>=0/数值有限) — 不合格根本不碰车
    2. SET MISSION HEARTBEAT_TIMEOUT 5000(防轮询卡顿触发 Gate8)
    3. TRACK UPLOAD BEGIN <n> → 逐点 PT(收到上一条 ACK 再发下一条,固件
       RX ring 只有 2KB,严格 ACK 步进) → TRACK UPLOAD END
    4. 回读校验(默认开):TRACK DUMP 全量回吐,逐点 diff px/py/yaw/enc
    5. --save-slot N 时 TRACK SAVE N 持久化进 flash 槽(脱机可用)

⚠ 安全须知:
    - 上传**不会**让车动(无 engage),但它替换的是复现要跟的线。
      上传后第一次 MISSION EVENT1 必须低速并有人盯(FULL_SPEED 先压低)。
    - 固件任何 PT 报 ERR 即整次作废(轨迹保持"无点"),本脚本直接退出,
      重跑即可——不存在"半条轨迹被复现"的状态。
    - 固件守卫:mission 非 IDLE / 录制中 / 回放中会拒 BEGIN。

用法:
    python track_upload.py paths/rerecord204_upload.json
    python track_upload.py paths/rerecord204_upload.json --save-slot 2 --label opt_v1
"""
from __future__ import annotations

import argparse
import json
import math
import sys
import time
from pathlib import Path

from track_dump import (http_get, http_post, send_command,
                        project_telpt_to_record_point, collect_dump_points)


# ============================================================================
# ACK 等待
# ============================================================================

def wait_cmd_result(base: str, cmd_name: str, timeout_s: float = 5.0,
                    poll_s: float = 0.05, match: dict | None = None):
    """轮询 /latest 直到 cmd_result.cmd == cmd_name(可附加 data 字段匹配)。
    返回 cmd_result dict;超时返回 None;ERR 也返回(调用方查 ack 字段)。"""
    t_end = time.time() + timeout_s
    while time.time() < t_end:
        data = http_get(f"{base}/latest", timeout=2.0)
        if data is not None:
            cr = data.get("cmd_result") or {}
            if (cr.get("cmd") or "").upper() == cmd_name:
                d = cr.get("data") or {}
                if cr.get("ack") == "ERR":
                    return cr
                if match is None or all(str(d.get(k)) == str(v)
                                        for k, v in match.items()):
                    return cr
        time.sleep(poll_s)
    return None


def send_and_wait(base: str, command: str, cmd_name: str,
                  timeout_s: float = 5.0, match: dict | None = None,
                  quiet: bool = False):
    """POST /command 然后等对应 ACK。返回 (ok, cmd_result)。"""
    res = http_post(f"{base}/command", {"command": command})
    if res is None or res.get("status") != "sent":
        if not quiet:
            print(f"[ERR] bridge refused: {command}")
        return False, None
    cr = wait_cmd_result(base, cmd_name, timeout_s=timeout_s, match=match)
    if cr is None:
        if not quiet:
            print(f"[ERR] no ACK for {cmd_name} within {timeout_s}s")
        return False, None
    if cr.get("ack") == "ERR":
        reason = (cr.get("data") or {}).get("reason", "UNKNOWN")
        print(f"[ERR] {cmd_name} -> ERR {reason}")
        return False, cr
    return True, cr


# ============================================================================
# 本地预校验
# ============================================================================

def validate_points(pts: list) -> list:
    errs = []
    n = len(pts)
    if not (2 <= n <= 2000):
        errs.append(f"点数 {n} 越界 [2, 2000]")
        return errs
    prev_dist = None
    for i, p in enumerate(pts):
        px, py = p.get("px_m"), p.get("py_m")
        yaw = p.get("yaw_deg")
        spd, dist = p.get("enc_spd_mm_s"), p.get("enc_dist_mm")
        for name, v in (("px_m", px), ("py_m", py), ("yaw_deg", yaw)):
            if not isinstance(v, (int, float)) or not math.isfinite(v):
                errs.append(f"pt{i} {name}={v} 非有限数")
        if not isinstance(spd, int) or spd < 0:
            errs.append(f"pt{i} enc_spd={spd} 必须为非负整数(前进序)")
        if not isinstance(dist, int):
            errs.append(f"pt{i} enc_dist={dist} 必须为整数")
        elif prev_dist is not None and dist < prev_dist:
            errs.append(f"pt{i} enc_dist {dist} < pt{i-1} {prev_dist}(必须单调非减)")
        prev_dist = dist if isinstance(dist, int) else prev_dist
        if isinstance(px, float) and not (-1000.0 < px < 1000.0):
            errs.append(f"pt{i} px={px} 超 ±1km")
        if isinstance(py, float) and not (-1000.0 < py < 1000.0):
            errs.append(f"pt{i} py={py} 超 ±1km")
        if len(errs) > 10:
            errs.append("...(更多错误省略)")
            break
    if not errs and pts[-1]["enc_dist_mm"] <= pts[0]["enc_dist_mm"]:
        errs.append("总里程为 0")
    return errs


# ============================================================================
# 上传主流程
# ============================================================================

def upload(args) -> int:
    base = f"http://{args.http_host}:{args.http_port}"

    with open(args.input_json, "r", encoding="utf-8") as f:
        payload = json.load(f)
    pts = payload["points"] if isinstance(payload, dict) else payload
    label = args.label or Path(args.input_json).stem

    errs = validate_points(pts)
    if errs:
        print("[ERR] 本地预校验失败,不上传:")
        for e in errs:
            print("   ", e)
        return 2
    n = len(pts)
    total_m = (pts[-1]["enc_dist_mm"] - pts[0]["enc_dist_mm"]) / 1000.0
    print(f"[OK] 本地校验通过: {n} 点, {total_m:.1f}m  ({args.input_json})")

    st = http_get(f"{base}/status", timeout=2.0)
    if st is None:
        print(f"[ERR] YawTuningTool 不在 {base}(exe 开了吗?)"); return 3
    if int(st.get("connections") or 0) < 1:
        print("[ERR] 车没连上桥(connections=0)"); return 3

    # 心跳放宽 + 清流(减少桥噪声;上传命令流本身就是保活)
    send_command(base, "SET MISSION HEARTBEAT_TIMEOUT 5000")
    time.sleep(0.3)
    send_command(base, "STOP ALL STREAMS")
    time.sleep(0.3)

    ok, _ = send_and_wait(base, f"TRACK UPLOAD BEGIN {n}",
                          "TRACK_UPLOAD_BEGIN", match={"count": n})
    if not ok:
        return 4
    print(f"[OK] BEGIN {n}")

    t0 = time.time()
    for i, p in enumerate(pts):
        cmd = (f"TRACK UPLOAD PT {i} {p['px_m']:.4f} {p['py_m']:.4f} "
               f"{p['yaw_deg']:.2f} {p['enc_spd_mm_s']} {p['enc_dist_mm']}")
        ok, _ = send_and_wait(base, cmd, "TRACK_UPLOAD_PT",
                              timeout_s=args.ack_timeout,
                              match={"idx": i}, quiet=True)
        if not ok:
            print(f"[ERR] pt{i} 失败 → 固件已整次作废(轨迹'无点'),重跑本脚本即可")
            send_command(base, "TRACK UPLOAD ABORT")
            return 5
        if i % 50 == 0 or i == n - 1:
            rate = (i + 1) / max(time.time() - t0, 1e-3)
            print(f"  ...{i + 1}/{n}  ({rate:.0f} pt/s, 剩余 ~{(n-1-i)/max(rate,1e-3):.0f}s)")

    ok, cr = send_and_wait(base, "TRACK UPLOAD END", "TRACK_UPLOAD_END",
                           match={"pts": n})
    if not ok:
        return 6
    print(f"[OK] END 落账 {n} 点, 用时 {time.time()-t0:.0f}s")

    # 回读校验
    if not args.no_verify:
        print("[verify] TRACK DUMP 回读比对...")
        http_post(f"{base}/trajectory/clear", {})
        time.sleep(0.2)
        ok, cr = send_and_wait(base, "TRACK DUMP", "TRACK_DUMP")
        if not ok:
            print("[WARN] 回读启动失败,跳过校验(轨迹已落账)")
        else:
            raw = collect_dump_points(base, n, period_ms=200, timeout_s=120.0)
            back = [project_telpt_to_record_point(q) for q in raw]
            if len(back) != n:
                print(f"[WARN] 回读 {len(back)}/{n} 点不齐,校验不完整")
            m = min(len(back), n)
            worst_xy = worst_yaw = 0.0
            mismatch = 0
            for i in range(m):
                dxy = math.hypot(back[i]["px_m"] - pts[i]["px_m"],
                                 back[i]["py_m"] - pts[i]["py_m"])
                dyaw = abs(back[i]["yaw_deg"] - pts[i]["yaw_deg"])
                worst_xy = max(worst_xy, dxy)
                worst_yaw = max(worst_yaw, dyaw)
                if (back[i]["enc_dist_mm"] != pts[i]["enc_dist_mm"] or
                        back[i]["enc_spd_mm_s"] != pts[i]["enc_spd_mm_s"]):
                    mismatch += 1
            print(f"[verify] {m} 点: max|dxy|={worst_xy*1000:.1f}mm, "
                  f"max|dyaw|={worst_yaw:.3f}deg, enc 不匹配 {mismatch} 点")
            if worst_xy > 0.002 or worst_yaw > 0.05 or mismatch:
                print("[ERR] 校验超差!不要发车,排查后重传"); return 7
            print("[OK] 校验通过")

    # flash 持久化
    if args.save_slot is not None:
        ok, cr = send_and_wait(base, f"TRACK SAVE {args.save_slot}", "TRACK_SAVE")
        if not ok:
            return 8
        saved = (cr.get("data") or {}).get("pts", "?")
        print(f"[OK] SAVE slot {args.save_slot}: {saved} 点"
              + (f"(>1000 被裁切!)" if str(saved) != str(n) else ""))

    print(f"\n[DONE] '{label}' 已上车。第一次复现务必低速有人盯:")
    print("        SET MISSION FULL_SPEED 1.0  之后再逐步恢复 2.5")
    # 归档钩子(2026-07 P1):上传成功 = 车上 RAM 线换成这条,登记指纹并置为当前线
    try:
        from archive import register_line, set_active_line
        _fp = register_line(pts, label, "upload", origin_file=str(args.input_json))
        set_active_line(_fp, label, len(pts), "upload")
    except Exception as e:
        print(f"[WARN] 归档登记失败(不影响上传本身): {e}")
    return 0


def main(argv) -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("input_json", help="path_pipeline.py 的 *_upload.json")
    ap.add_argument("--label", default=None)
    ap.add_argument("--http-host", default="127.0.0.1")
    ap.add_argument("--http-port", type=int, default=9898)
    ap.add_argument("--ack-timeout", type=float, default=5.0,
                    help="单点 ACK 超时 s(默认 5)")
    ap.add_argument("--no-verify", action="store_true",
                    help="跳过 TRACK DUMP 回读校验(不推荐)")
    ap.add_argument("--save-slot", type=int, default=None, choices=[0, 1, 2],
                    help="校验通过后 TRACK SAVE 进 flash 槽")
    args = ap.parse_args(argv)
    return upload(args)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
