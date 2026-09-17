#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""enc_diag_run.py — 启动转向编码器 Enc Diag 自检 + 轮询到 DONE + 打印各段漂移 (2026-06-20)

前置:桥开 + 车连 + 前轮悬空。自检会动转向电机(~16-18s,含 3.2s 震荡段)。
用法:python enc_diag_run.py [--host 127.0.0.1:9898] [--timeout 35]
读数口径(健康 ≈ 0,±顶死抖动 ~5):
  rest  = 静止段计数漂移(电机关时还变 = 噪声多脉冲)
  net   = 5 圈往返后同端净漂移(累积丢/多脉冲)
  span0/span1 = 首圈/末圈左右量程(缩小 = 丢脉冲)
  dith  = 快换向 40 次后回同端的漂移(动态失效工况)
  gap   = CONST 匀速段最长连续 Δ=0 拍(冻结=丢脉冲,硬件信号!)
  rev   = CONST 匀速前进中逆向拍数(误数,硬件信号!)
  spk   = CONST 单拍 |Δ|>25 尖刺数(突发噪声)
  csmp  = CONST 采样拍数
"""
import argparse
import json
import time
import urllib.request

DEF_HOST = "127.0.0.1:9898"
ST = {0: "IDLE", 1: "REST", 2: "SWEEP_A", 3: "SWEEP_B", 4: "DITHER_REF0",
      5: "DITHER", 6: "DITHER_REF1", 7: "CONST", 8: "DONE", 9: "FAILED"}


def post(host, cmd):
    body = json.dumps({"command": cmd}).encode("ascii")
    req = urllib.request.Request("http://%s/command" % host, data=body,
                                 headers={"Content-Type": "application/json"})
    urllib.request.urlopen(req, timeout=4).read()


def latest(host):
    r = urllib.request.urlopen("http://%s/latest" % host, timeout=4).read()
    return json.loads(r.decode("utf-8", "replace"))


def get_cr(host):
    cr = latest(host).get("cmd_result")
    if not isinstance(cr, dict):
        return {}
    # 桥把 ACK 解析成 {cmd, ack, data:{..字段..}, ts};真正字段(st/mode/rest/..)在 data 里。
    d = cr.get("data")
    if isinstance(d, dict):
        out = dict(d)
        out.setdefault("cmd", cr.get("cmd"))
        return out
    return cr


def poll_cmd(host, expect_cmd, retries=15):
    for _ in range(retries):
        time.sleep(0.08)
        cr = get_cr(host)
        if cr.get("cmd") == expect_cmd:
            return cr
    return {}


def fmt(cr):
    return ("rest=%s net=%s span0=%s span1=%s dith=%s | gap=%s rev=%s spk=%s csmp=%s cyc=%s"
            % (cr.get("rest"), cr.get("net"), cr.get("span0"), cr.get("span1"), cr.get("dith"),
               cr.get("gap"), cr.get("rev"), cr.get("spk"), cr.get("csmp"), cr.get("cyc")))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default=DEF_HOST)
    ap.add_argument("--timeout", type=float, default=35.0)
    ap.add_argument("--read-only", action="store_true", help="只读当前(粘性)结果,不重跑自检")
    args = ap.parse_args()
    host = args.host

    try:
        st = json.loads(urllib.request.urlopen("http://%s/status" % host, timeout=4).read().decode())
    except Exception as e:
        print("连不上桥 %s: %s" % (host, e))
        return 2
    if st.get("connections", 0) < 1:
        print("车没连(connections=0)")
        return 2

    post(host, "GET TURN ENCMODE")
    em = poll_cmd(host, "GET_TURN_ENCMODE")
    print("ENCMODE mode=%s (0=dir 1=quad)" % em.get("mode"))

    if args.read_only:
        post(host, "GET TURN ENCDIAG")
        cr = poll_cmd(host, "GET_TURN_ENCDIAG")
        stv = int(cr.get("st", -1)) if cr else -1
        print("当前(粘性)结果: state=%d %s" % (stv, ST.get(stv, "?")))
        print(" ", fmt(cr))
        return 0

    print("发 START TURN ENCDIAG ...")
    post(host, "START TURN ENCDIAG")
    time.sleep(0.3)
    sc = get_cr(host)
    print("  START ->", json.dumps(sc, ensure_ascii=False))
    cmdname = (sc.get("cmd") or "")
    if "UNKNOWN" in json.dumps(sc).upper() or sc.get("error"):
        if "ENCDIAG" not in cmdname:
            print(">> START 没被认(今天的 TCP 入口没烧进去) → 改用菜单 8.Enc Diag 按 P21.4(K4) 启动")
            return 3
    if str(sc.get("error", "")).upper().find("BUSY") >= 0:
        print(">> BUSY:有别的状态机在跑(AutoCal/Test/RawPwm) → 先停再来")
        return 3

    print("轮询各段(车正在动,听震荡段)...")
    t0 = time.time()
    last = None
    final = {}
    while time.time() - t0 < args.timeout:
        post(host, "GET TURN ENCDIAG")
        cr = poll_cmd(host, "GET_TURN_ENCDIAG")
        try:
            stv = int(cr.get("st", -1))
        except Exception:
            stv = -1
        if stv != last and stv in ST:
            print("  [%4.1fs] state=%d %-12s %s" % (time.time() - t0, stv, ST.get(stv, "?"), fmt(cr)))
            last = stv
        if stv in (8, 9):
            final = cr
            break
        time.sleep(0.6)

    if not final:
        print("轮询超时没到 DONE/FAILED;再 GET 一次:")
        post(host, "GET TURN ENCDIAG")
        final = poll_cmd(host, "GET_TURN_ENCDIAG")

    stv = int(final.get("st", -1)) if final else -1
    print("\n=== FINAL: %s ===" % ST.get(stv, "?"))
    print(" ", fmt(final))
    if stv == 9:
        print(">> FAILED:某顶死段 6s 超时没判稳(编码器在该段没给出稳定计数)")
    return 0 if stv == 8 else 1


if __name__ == "__main__":
    raise SystemExit(main())
