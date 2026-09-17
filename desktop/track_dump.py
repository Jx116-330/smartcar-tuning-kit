"""
track_dump.py - retrieve a complete INS record (LEARN-time path) from the car.

The car keeps the LEARN-mode trajectory in RAM as an `ins_record_point_t[]`
buffer of up to 2000 points (8 fields per point: t_ms, px_m, py_m, vx_ms,
vy_ms, yaw_deg, enc_spd_mm_s, enc_dist_mm). When the operator issues
`TRACK DUMP`, the firmware streams the whole buffer back over TCP as a
sequence of TELPT lines (re-using the TELPT stream format with the GPS
fields zeroed out).

YawTuningTool already accumulates TELPT into `app.trajectory_points` and
exposes them via the HTTP /trajectory endpoint. This script wraps the whole
exchange:

    1. POST /command "STOP ALL STREAMS"   (prevent realtime TELPT mixing
       with dump frames)
    2. POST /trajectory/clear              (start with empty buffer)
    3. POST /command "TRACK DUMP"
    4. Wait for ACK,cmd=TRACK_DUMP,count=N (parse N from /latest)
    5. Poll /trajectory?since=0 until count == N
    6. Re-key the GPS-overloaded TELPT fields onto the
       ins_record_point_t schema
    7. Save to recordings/learn_dump_<timestamp>.json

The output JSON is consumed by `path_optimizer.Path.from_learn_dump_json`
without further conversion - schema agreed across the toolchain.

WHY THIS MATTERS
----------------
All recordings in `recordings/` so far are RETURN-mode TELMISSION captures.
Those carry the cpx/cpy/cyaw the car ACTUALLY drove during a replay - which
already contains the previous-run tracking error. A TRACK DUMP gives the
LEARN-time reference path - i.e. what the car was SUPPOSED to follow. For
Stage 2 path geometry optimization the LEARN-time path is the right input.

This script does NOT modify firmware. It only consumes the existing
TRACK DUMP / TRACK CLEAR / TRACK RECORD START/STOP commands defined in
code/tuning/tuning_dispatch.c:191-281, and uses /command + /trajectory
HTTP endpoints already exposed by tuning_tool.py.

RELATED TOOLS
-------------
* `record_session.py` — captures the TELMISSION stream during RETURN
  (a live realtime monitor; output is per-frame TELMISSION JSON).
  Use record_session for parameter tuning / XTE analysis; use this tool
  for path geometry capture.

* Output schema is **deliberately different** from record_session.py:
  record_session.py writes a flat array of TELMISSION frames; this
  script writes `{meta, points: [...ins_record_point_t...]}`. Each
  schema serves a different downstream consumer (replay_to_dataframe
  vs path_optimizer.from_learn_dump_json). Don't try to share them.

Usage:
    # live (car connected via TCP, YawTuningTool running on :9898)
    python track_dump.py --label trail_v1

    # validation (no car needed): re-load a previously saved dump
    python track_dump.py --replay recordings/learn_dump_2026-05-12.json
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Optional


DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 9898
DEFAULT_OUT_DIR = Path(__file__).resolve().parent / "recordings"
DEFAULT_POLL_MS = 200
DEFAULT_TIMEOUT_S = 30.0   # dumping 2000 points at ~50 points/sec ~= 40s; be generous


# ============================================================================
# HTTP helpers (same shape as record_session.py for consistency)
# ============================================================================

def http_get(url: str, timeout: float = 2.0) -> Optional[object]:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            return json.loads(r.read())
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError):
        return None


def http_post(url: str, payload: dict, timeout: float = 3.0) -> Optional[dict]:
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url, data=body, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read())
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError):
        return None


def send_command(base: str, command: str) -> bool:
    res = http_post(f"{base}/command", {"command": command})
    ok = res is not None and res.get("status") == "sent"
    print(f"[CMD] {command:30s} -> {'OK' if ok else 'FAIL'}")
    return ok


# ============================================================================
# TELPT → ins_record_point_t projection
# ============================================================================

# TELPT line format (from code/tuning/tuning_send.c:337-340 during dump):
#   ms, px, py, vx, vy, yaw, spd, lat, lon, sat, gv, stat, fpx, fpy, esm, edm
# Of these we keep only the 8 that map to ins_record_point_t. The GPS
# fields (spd, lat, lon, sat, gv, stat, fpx, fpy) are explicitly set to 0
# during dump by the firmware and carry no information.

POINT_FIELD_MAP = {
    "ms":  ("t_ms",          int),
    "px":  ("px_m",          float),
    "py":  ("py_m",          float),
    "vx":  ("vx_ms",         float),
    "vy":  ("vy_ms",         float),
    "yaw": ("yaw_deg",       float),
    "esm": ("enc_spd_mm_s",  int),
    "edm": ("enc_dist_mm",   int),
}


def project_telpt_to_record_point(src: dict) -> dict:
    """Take one /trajectory point (TELPT-parsed dict) and project to
    ins_record_point_t schema. Missing fields default to 0."""
    out = {}
    for src_key, (dst_key, caster) in POINT_FIELD_MAP.items():
        v = src.get(src_key, 0)
        try:
            out[dst_key] = caster(v)
        except (TypeError, ValueError):
            out[dst_key] = caster(0)
    return out


# ============================================================================
# Live dump flow
# ============================================================================

def wait_for_dump_ack(base: str, period_ms: int, timeout_s: float
                     ) -> Optional[int]:
    """Poll /latest until cmd_result shows TRACK_DUMP ACK with a count.
    Returns the point count from the ACK, or None on timeout / NO_POINTS."""
    t_end = time.time() + timeout_s
    while time.time() < t_end:
        data = http_get(f"{base}/latest")
        if data is None:
            time.sleep(period_ms / 1000.0)
            continue
        cr = data.get("cmd_result") or {}
        cmd_name = (cr.get("cmd") or "").upper()
        if cmd_name == "TRACK_DUMP":
            if cr.get("ack") == "ERR":
                # ACK,cmd=TRACK_DUMP carries reason on ERR (e.g. NO_POINTS).
                reason = (cr.get("data") or {}).get("reason", "UNKNOWN")
                print(f"[ERR] TRACK_DUMP refused: {reason}")
                return None
            data_dict = cr.get("data") or {}
            count = data_dict.get("count")
            if count is not None:
                try:
                    return int(count)
                except (TypeError, ValueError):
                    return None
        time.sleep(period_ms / 1000.0)
    print(f"[ERR] no TRACK_DUMP ACK within {timeout_s}s")
    return None


def collect_dump_points(base: str, expected: int, period_ms: int,
                        timeout_s: float) -> list:
    """Poll /trajectory until trajectory_total >= expected points.
    Returns the full point list, in receive order. Each entry is whatever
    YawTuningTool put in trajectory_points (parsed TELPT dict)."""
    t_end = time.time() + timeout_s
    last_print = 0
    while time.time() < t_end:
        data = http_get(f"{base}/trajectory?since=0", timeout=3.0)
        if data is None:
            time.sleep(period_ms / 1000.0)
            continue
        pts = data.get("points") or []
        total = int(data.get("count") or 0)
        if total >= expected:
            return pts
        # Progress print every ~2s
        now = time.time()
        if now - last_print > 2.0:
            print(f"  ...received {total}/{expected} points")
            last_print = now
        time.sleep(period_ms / 1000.0)
    print(f"[WARN] timeout: received {total}/{expected} points; saving partial")
    pts = (http_get(f"{base}/trajectory?since=0") or {}).get("points") or []
    return pts


def dump_live(args: argparse.Namespace) -> int:
    base = f"http://{args.http_host}:{args.http_port}"
    if http_get(f"{base}/status", timeout=2.0) is None:
        print(f"YawTuningTool not reachable at {base}/status. "
              "Is the exe running?", file=sys.stderr)
        return 3

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    # 1. Clear the road. Stopping all streams and the trajectory buffer
    # ensures the next batch of TELPT lines we see are dump frames, not
    # leftover realtime data.
    send_command(base, "STOP ALL STREAMS")
    time.sleep(0.2)
    http_post(f"{base}/trajectory/clear", {})
    time.sleep(0.1)

    # 2. Fire the dump command.
    if not send_command(base, "TRACK DUMP"):
        print("send TRACK DUMP failed", file=sys.stderr)
        return 4

    # 3. Wait for the ACK so we know how many points to expect.
    expected = wait_for_dump_ack(base, args.poll_ms, args.ack_timeout)
    if expected is None:
        return 5
    print(f"[OK] expecting {expected} points")

    # 4. Drain /trajectory until we get them all (or hit timeout).
    pts = collect_dump_points(base, expected, args.poll_ms, args.timeout)
    if len(pts) == 0:
        print("[ERR] no points captured", file=sys.stderr)
        return 6
    print(f"[OK] captured {len(pts)} points "
          f"(expected {expected})")

    # 5. Project TELPT keys to ins_record_point_t schema.
    record_pts = [project_telpt_to_record_point(p) for p in pts]

    # 6. Save. File name embeds timestamp + the original ACK count for
    # provenance; mismatches between len(record_pts) and expected are
    # recorded in meta so analysis tools can flag them.
    ts = time.strftime("%Y%m%d-%H%M%S")
    out_path = out_dir / f"learn_dump_{ts}_{args.label}.json"
    payload = {
        "meta": {
            "source": "track_dump",
            "captured_at": ts,
            "label": args.label,
            "expected_points": expected,
            "captured_points": len(record_pts),
            "schema": "ins_record_point_t",
            "schema_version": 1,
            "schema_doc": (
                "8 fields per point: t_ms (ms since car boot), "
                "px_m, py_m (position in m, navigation frame), "
                "vx_ms, vy_ms (velocity m/s), yaw_deg (heading), "
                "enc_spd_mm_s (rear-wheel encoder speed mm/s), "
                "enc_dist_mm (rear-wheel cumulative distance mm)"
            ),
        },
        "points": record_pts,
    }
    out_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    print(f"[SAVED] {out_path}  ({out_path.stat().st_size} bytes)")
    # 归档钩子(2026-07 P1):登记线指纹 + 标记"当前 RAM 线"(此后 watch 归档的趟都归到这条线名下)
    try:
        from archive import register_line, set_active_line
        _fp = register_line(record_pts, args.label, "dump", origin_file=out_path.name)
        set_active_line(_fp, args.label, len(record_pts), "dump")
    except Exception as e:
        print(f"[WARN] 归档登记失败(不影响 dump 本身): {e}")
    return 0


# ============================================================================
# Replay (no car)
# ============================================================================

def replay(args: argparse.Namespace) -> int:
    p = Path(args.replay)
    if not p.exists():
        print(f"file not found: {p}", file=sys.stderr)
        return 2
    raw = json.loads(p.read_text(encoding="utf-8"))
    if not isinstance(raw, dict) or "points" not in raw:
        print(f"{p}: not a track-dump JSON (expected {{meta, points}})",
              file=sys.stderr)
        return 2
    pts = raw["points"]
    meta = raw.get("meta", {})

    print(f"\n=== {p.name} ===")
    print(f"  captured_at:  {meta.get('captured_at', '?')}")
    print(f"  label:        {meta.get('label', '?')}")
    print(f"  point count:  {len(pts)} (expected {meta.get('expected_points', '?')})")
    if pts:
        first = pts[0]; last = pts[-1]
        # Distance summary using enc_dist_mm and px/py if available.
        if "enc_dist_mm" in first:
            d_enc = last.get("enc_dist_mm", 0) - first.get("enc_dist_mm", 0)
            print(f"  encoder distance:  {d_enc/1000.0:.2f} m  "
                  f"(start enc_dist_mm={first.get('enc_dist_mm', 0)}, "
                  f"end={last.get('enc_dist_mm', 0)})")
        if "t_ms" in first:
            dt = last.get("t_ms", 0) - first.get("t_ms", 0)
            print(f"  duration:          {dt/1000.0:.2f} s")
        # Path-length via integration.
        import math
        L = 0.0
        for a, b in zip(pts[:-1], pts[1:]):
            dx = b.get("px_m", 0) - a.get("px_m", 0)
            dy = b.get("py_m", 0) - a.get("py_m", 0)
            L += math.hypot(dx, dy)
        print(f"  XY path length:    {L:.2f} m")
        print(f"  first point: t={first.get('t_ms',0)}ms "
              f"pos=({first.get('px_m',0):.2f}, {first.get('py_m',0):.2f}) "
              f"yaw={first.get('yaw_deg',0):.1f}deg")
        print(f"  last  point: t={last.get('t_ms',0)}ms "
              f"pos=({last.get('px_m',0):.2f}, {last.get('py_m',0):.2f}) "
              f"yaw={last.get('yaw_deg',0):.1f}deg")
    return 0


# ============================================================================
# CLI
# ============================================================================

def main(argv: list) -> int:
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--label", default="run",
                    help="experiment label, embedded in output filename")
    ap.add_argument("--http-host", default=DEFAULT_HOST)
    ap.add_argument("--http-port", type=int, default=DEFAULT_PORT)
    ap.add_argument("--out-dir", default=str(DEFAULT_OUT_DIR),
                    help=f"output directory (default {DEFAULT_OUT_DIR})")
    ap.add_argument("--poll-ms", type=int, default=DEFAULT_POLL_MS,
                    help=f"poll period in ms (default {DEFAULT_POLL_MS})")
    ap.add_argument("--ack-timeout", type=float, default=5.0,
                    help="seconds to wait for TRACK_DUMP ACK (default 5)")
    ap.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT_S,
                    help=f"overall timeout for the dump in seconds "
                         f"(default {DEFAULT_TIMEOUT_S})")
    ap.add_argument("--replay", default=None,
                    help="re-summarize an existing dump JSON without "
                         "talking to a car")
    args = ap.parse_args(argv)

    if args.replay:
        return replay(args)
    return dump_live(args)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
