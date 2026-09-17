"""
sim_pure_pursuit.py
Offline Pure Pursuit simulator for TC387 trajectory replay system.

Mirrors the C control law in:
  Seekfree_TC387_Opensource_Library/code/ICM42688/ins_ctrl.c
  Seekfree_TC387_Opensource_Library/code/ICM42688/ins_actuator.h

Purpose: study KP / LOOKAHEAD / HEAD_BLEND parameter behavior offline against
synthetic paths (or recorded TELPT-style logs) without real-vehicle testing.

Usage:
    python sim_pure_pursuit.py                # default arc sweep + plot
    python sim_pure_pursuit.py recording.json # use recorded path

Vehicle model: kinematic bicycle (see VehicleModel below). This is a kinematic
approximation - real car has Turn-side dynamics, encoder noise, brake-protect,
quantization etc. Sim is for parameter trends, not absolute fidelity.
"""

from __future__ import annotations

import json
import math
import os
import sys
from dataclasses import dataclass, field
from typing import Optional

import numpy as np
import matplotlib

# Use non-interactive backend so the script runs headless without errors.
matplotlib.use("Agg")
import matplotlib.pyplot as plt


# ============================================================================
# Constants (delegated to vehicle_params.py - single source of truth)
# ============================================================================
# Re-exported names kept identical so existing simulate()/sweep() bodies don't
# need to change. To update any value (e.g. measured WHEELBASE_M), edit
# vehicle_params.py and re-run; the simulator picks it up automatically.
from vehicle_params import (
    WHEELBASE_M,
    LA_GEO_MIN_M,
    CRUISE_DIST_M,
    SLOW_RATIO,
    STEER_LIMIT_DEG,
    LOST_DIST_M,
    LOST_HOLD_TICKS,
    REACH_THRESHOLD_M,
    SERVO_TAU_S,
    ENCODER_POSITION_NOISE_STD_M,
    MAGNETO_YAW_BIAS_DEG,
    MAGNETO_YAW_NOISE_STD_DEG,
    SERVO_DEADBAND_DEG,
    SERVO_BACKLASH_DEG,
)

# Curvature κ constants — mirror ins_ctrl.h (2026-06). Drive the curvature-
# continuous speed model that replaces the binary cursor-slowdown in firmware.
FF_BASELINE_PTS   = 2      # κ baseline: ±N points around the lookahead index
FF_MIN_DS_MM      = 20.0   # baseline arc < this -> κ=0 (anti div-by-zero)
FF_LP_ALPHA       = 0.3    # κ EMA low-pass coefficient (matches INS_CTRL_FF_LP_ALPHA)
SPEED_PREVIEW_PTS = 8      # speed κ: max local turn-rate over N points ahead of cursor
                           # (mirrors INS_CTRL_SPEED_PREVIEW_PTS)
END_DECEL_DIST_MM = 1000.0 # terminal decel starts within last 1.0 m (mirrors INS_CTRL_END_DECEL_DIST_MM)
END_APPROACH_MS   = 0.40   # approach speed at final point (mirrors INS_CTRL_END_APPROACH_MS)
# End-of-path instability fix A+B (2026-06-06, mirrors INS_CTRL_END_YAW_BLEND_MAX).
# (A) when remaining arc < lookahead, virtually extend the aim point along the end
#     segment instead of clamping to the last point -> la_dist stays ~lookahead, so
#     geo_yaw no longer blows up on cm DR noise (end "phantom turn" + straight weave).
# (B) within the end zone, progressively blend aim toward recorded yaw by depth
#     t = (lookahead - rem_arc)/lookahead, capped at END_YAW_BLEND_MAX.
END_YAW_BLEND_MAX = 0.70

# ============================================================================
# Tier-1 speed-up (2026-06): ① forward-backward velocity profile + ② adaptive
# lookahead. Mirrors the planned ins_ctrl.c changes. All gated by Params flags
# (default OFF = legacy behaviour, existing tests unaffected).
# ----------------------------------------------------------------------------
# ① velocity profile: per-point speed cap from a friction-/steering-authority
#    limited corner speed v=sqrt(a_lat_max/|κ|) plus a forward (accel) and
#    backward (brake) pass — straights run at v_max, corners are slowed to the
#    steering-authority limit, and braking starts BEFORE the corner (the thing
#    the current FULL/(1+gain·κ) heuristic can't do → the FS=3.0 abort cause).
# ② adaptive lookahead: L = clip(v·T, min, max) — fast→look far (smooth, allows
#    higher FULL), slow→look near (corner-accurate).
A_LAT_MAX_DEFAULT  = 3.0    # m/s^2 lateral-accel ceiling (= "transverse steering authority"); CALIBRATE on car
                            # (== vehicle_params.MAX_LATERAL_ACCEL_MS2 placeholder; speed_planner uses same)
A_ACCEL_DEFAULT    = 2.0    # m/s^2 longitudinal accel limit (== speed_planner MAX_LONG_ACCEL_MS2 placeholder)
A_BRAKE_DEFAULT    = 2.0    # m/s^2 longitudinal brake limit (separate from accel; CALIBRATE — car may brake harder)
VPROF_KAPPA_MIN    = 0.05   # rad/m: below this a point is "straight" → cap = v_max (anti div-0)
LOOKAHEAD_TIME_S   = 0.30   # ② L = clip(v·T, min, max); T = "project current speed this many seconds ahead"
LOOKAHEAD_MIN_MM   = 250    # ≈ current fixed value, keeps corner accuracy
LOOKAHEAD_MAX_MM   = 700    # straight: look this far
VPROF_V_FLOOR_MS   = 0.05   # never plan below this (matches speed_planner.SpeedConfig.v_floor_ms)


# ============================================================================
# Helpers (mirror C math)
# ============================================================================

def normalize_angle_deg(deg: float) -> float:
    """Wrap angle to (-180, 180], matching ctrl_normalize_angle_deg in ins_ctrl.c."""
    while deg > 180.0:
        deg -= 360.0
    while deg <= -180.0:
        deg += 360.0
    return deg


def lerp_angle_deg(a: float, b: float, t: float) -> float:
    """Shortest-path angle lerp (matches ctrl_lerp_angle_deg)."""
    d = normalize_angle_deg(b - a)
    return normalize_angle_deg(a + t * d)


# ============================================================================
# Path definitions
# ============================================================================

@dataclass
class Path:
    """Sequence of recorded points (px_m, py_m, yaw_deg). yaw is direction of travel."""
    points: list  # list[tuple[float, float, float]]

    def __len__(self):
        return len(self.points)

    def cumulative_arc(self) -> np.ndarray:
        """Cumulative arc length (mm) per point, used to compute lookahead."""
        if len(self.points) == 0:
            return np.zeros(0)
        xs = np.array([p[0] for p in self.points])
        ys = np.array([p[1] for p in self.points])
        dx = np.diff(xs)
        dy = np.diff(ys)
        seg = np.sqrt(dx * dx + dy * dy) * 1000.0  # m -> mm
        cum = np.concatenate(([0.0], np.cumsum(seg)))
        return cum


def make_straight(length_m: float = 10.0, dx: float = 0.05) -> Path:
    """Straight line along +x at y=0, yaw=0."""
    n = int(length_m / dx) + 1
    pts = []
    for i in range(n):
        x = i * dx
        pts.append((x, 0.0, 0.0))
    return Path(points=pts)


def make_arc(radius_m: float = 3.0, sweep_deg: float = 180.0,
             dx: float = 0.05) -> Path:
    """Arc starting at origin moving +x, sweeping CCW (left turn)."""
    arc_len = radius_m * math.radians(sweep_deg)
    n = max(2, int(arc_len / dx) + 1)
    cx, cy = 0.0, radius_m  # center is straight up at start
    pts = []
    for i in range(n):
        # Parametrize so first point is (0,0) heading +x.
        theta = (i / (n - 1)) * math.radians(sweep_deg) - math.pi / 2.0
        x = cx + radius_m * math.cos(theta)
        y = cy + radius_m * math.sin(theta)
        # yaw = tangent direction = theta + pi/2
        yaw_rad = theta + math.pi / 2.0
        yaw_deg = math.degrees(yaw_rad)
        pts.append((x, y, normalize_angle_deg(yaw_deg)))
    return Path(points=pts)


def make_s_curve(radius_m: float = 3.0, sweep_deg: float = 90.0,
                 dx: float = 0.05) -> Path:
    """Two opposing arcs joined - tests sign reversal."""
    a1 = make_arc(radius_m=radius_m, sweep_deg=sweep_deg, dx=dx)
    # Continue from end of first arc, mirror.
    end_x, end_y, end_yaw = a1.points[-1]
    cos_y, sin_y = math.cos(math.radians(end_yaw)), math.sin(math.radians(end_yaw))
    arc_len = radius_m * math.radians(sweep_deg)
    n = max(2, int(arc_len / dx) + 1)
    # Second arc curves opposite direction.
    pts = list(a1.points)
    cx_local = 0.0
    cy_local = -radius_m  # mirrored center
    for i in range(1, n):
        theta = (i / (n - 1)) * math.radians(sweep_deg) + math.pi / 2.0
        lx = cx_local + radius_m * math.cos(theta)
        ly = cy_local + radius_m * math.sin(theta)
        yaw_rad_local = theta - math.pi / 2.0
        # rotate+translate into world via end of arc1 frame
        wx = end_x + cos_y * lx - sin_y * ly
        wy = end_y + sin_y * lx + cos_y * ly
        wy_deg = math.degrees(yaw_rad_local) + end_yaw
        pts.append((wx, wy, normalize_angle_deg(wy_deg)))
    return Path(points=pts)


def load_recording(path_file: str) -> Path:
    """Load a TELPT-style recording. Accepts JSON list of {x_m, y_m, yaw_deg} or
    list of [x, y, yaw] triples."""
    with open(path_file, "r") as f:
        data = json.load(f)
    pts = []
    if isinstance(data, list) and data and isinstance(data[0], dict):
        for p in data:
            x = float(p.get("x_m", p.get("px_m", p.get("x", 0.0))))
            y = float(p.get("y_m", p.get("py_m", p.get("y", 0.0))))
            yd = float(p.get("yaw_deg", p.get("yaw", 0.0)))
            pts.append((x, y, normalize_angle_deg(yd)))
    elif isinstance(data, list):
        for p in data:
            pts.append((float(p[0]), float(p[1]), normalize_angle_deg(float(p[2]))))
    else:
        raise ValueError(f"Unrecognized recording format in {path_file}")
    return Path(points=pts)


# ============================================================================
# Pure Pursuit core (mirror of ctrl_find_lookahead_frac)
# ============================================================================

def find_lookahead_frac(path: Path, cursor_idx: int, lookahead_mm: int,
                        cum_arc: np.ndarray):
    """Walk forward from cursor along path summing arc length until >= lookahead_mm.
    Returns (idx_a, idx_b, frac). Mirrors ctrl_find_lookahead_frac in ins_ctrl.c.
    Uses 2D Euclidean segment lengths (vs C's enc_dist_mm); identical for synthetic
    paths because we set both equally. ASSUMPTION: holds for recorded data when the
    encoder distance tracked the path closely (typical LEARN single-direction push)."""
    total = len(path)
    if total == 0 or cursor_idx >= total or lookahead_mm <= 0:
        return cursor_idx, cursor_idx, 0.0

    base = cum_arc[cursor_idx]
    target = base + lookahead_mm

    # Find smallest idx with cum_arc[idx+1] >= target.
    for idx in range(cursor_idx, total - 1):
        next_cum = cum_arc[idx + 1]
        if next_cum >= target:
            seg = next_cum - cum_arc[idx]
            remaining = target - cum_arc[idx]
            frac = (remaining / seg) if seg > 0 else 0.0
            return idx, idx + 1, max(0.0, min(1.0, frac))

    # Clamp to last point.
    return total - 1, total - 1, 0.0


# ============================================================================
# Simulation primitives
# ============================================================================

@dataclass
class Params:
    steer_kp: float = 2.5
    lookahead_mm: int = 300
    head_blend: float = 0.0
    speed_mm_s: float = 400.0          # SPEED_CAP, also the constant cruise speed
    steer_limit_deg: float = STEER_LIMIT_DEG
    dt_s: float = 0.01                 # 100 Hz tick (matches 10 ms task)
    reach_threshold_m: float = REACH_THRESHOLD_M
    # 2026-05-11 Stage 1.5 additions: bring the sim closer to real-car physics.
    # servo_tau_s = 0 means ideal (legacy behaviour); see vehicle_params.py.
    servo_tau_s: float = SERVO_TAU_S
    # Enable slowdown when within CRUISE_DIST_M of the cursor target. LEGACY —
    # only used when enable_curvature_speed=False. Reproduces the pre-2026-06
    # binary cursor-slowdown (the source of the speed jerk).
    enable_cursor_slowdown: bool = True
    # 2026-06: curvature-continuous speed (mirrors ins_ctrl.c). When True (default,
    # matches current firmware), speed = base/(1 + curve_speed_gain·|κ|) clamped to
    # [base·SLOW_RATIO, base]; κ from the path at the lookahead (EMA-filtered).
    # This supersedes enable_cursor_slowdown. Set False to get legacy behaviour.
    enable_curvature_speed: bool = True
    curve_speed_gain: float = 1.5      # mirrors INS_CTRL_CURVE_GAIN_DEFAULT
    # NOTE: with curvature speed on, speed_mm_s is the FULL_SPEED base the formula
    # divides — set it to the car's FULL_SPEED×1000 (NOT SPEED_CAP), else absolute
    # speeds and the κ at which the floor is reached won't match the car. On the
    # car, FULL_SPEED and SPEED_CAP are separate (cap clips downstream in actuator).
    # 2026-05-12 Stage 1.7 additions: noise / non-ideality model. All
    # default OFF so the Stage 1.5 deterministic behaviour is preserved;
    # callers opt in per-noise-type. Magnitudes come from
    # vehicle_params (all PLACEHOLDER until measured).
    enable_encoder_noise:   bool  = False
    enable_magneto_noise:   bool  = False
    enable_servo_deadband:  bool  = False
    enable_servo_backlash:  bool  = False
    noise_seed:             int   = 0       # 0 = OS entropy, else reproducible

    # 2026-05-12 Stage 3 addition: optional per-cursor variable target speed.
    # When set, the kinematic update reads speed_mm_s from this array indexed
    # by the active cursor instead of the scalar speed_mm_s above. Length
    # must match the path. None = legacy constant-speed behaviour.
    # The simulator still applies cursor_slowdown on top of the profile.
    # OFFLINE ONLY: this is consumed only by simulate(); no firmware path
    # exists to push a per-point speed to the car (see paths/SPEED_PROFILE_INTERFACE.md).
    path_speed_profile: object = None    # Optional[np.ndarray] of m/s

    # 2026-06 Tier-1 speed-up. Default OFF → legacy behaviour preserved.
    # ① forward-backward velocity profile (built once from the path geometry;
    #    when on, it REPLACES the curvature heuristic and the end-decel block —
    #    the backward pass to v_end already subsumes both).
    enable_velocity_profile: bool = False
    a_lat_max:  float = A_LAT_MAX_DEFAULT    # m/s^2 corner cap = sqrt(a_lat_max/κ)
    a_accel:    float = A_ACCEL_DEFAULT      # m/s^2 forward pass
    a_brake:    float = A_BRAKE_DEFAULT      # m/s^2 backward pass
    vprof_v_end_ms: float = END_APPROACH_MS  # speed cap at final point (= approach)
    # ② velocity-adaptive lookahead. L = clip(v·T, min, max).
    enable_adaptive_lookahead: bool = False
    lookahead_time_s:  float = LOOKAHEAD_TIME_S
    lookahead_min_mm:  int   = LOOKAHEAD_MIN_MM
    lookahead_max_mm:  int   = LOOKAHEAD_MAX_MM


@dataclass
class Result:
    t: list = field(default_factory=list)
    cur_x: list = field(default_factory=list)
    cur_y: list = field(default_factory=list)
    cur_yaw: list = field(default_factory=list)        # deg
    xte: list = field(default_factory=list)
    yaw_err: list = field(default_factory=list)        # deg
    steer_cmd: list = field(default_factory=list)      # deg (post-clamp, commanded)
    steer_actual: list = field(default_factory=list)   # deg (post-servo-lag)
    cursor_idx: list = field(default_factory=list)
    saturated: list = field(default_factory=list)
    la_idx: list = field(default_factory=list)
    la_dist: list = field(default_factory=list)
    distance_m: list = field(default_factory=list)     # cumulative travel along sim
    speed_used: list = field(default_factory=list)     # m/s after slow-down shaping
    lost: bool = False
    finished: bool = False                              # reached final cursor


def _compute_xte(path: Path, cursor_idx: int, cur_x: float, cur_y: float) -> float:
    """Cross-track error per ins_ctrl.c xte block. Mirrors:
        xte = (sx*ry - sy*rx) / |seg|
    where seg = cursor - prev_cursor and rel = cur - prev_cursor.
    Returns 0.0 for cursor 0 or degenerate segment (<1mm)."""
    if cursor_idx < 1:
        return 0.0
    prev = path.points[cursor_idx - 1]
    cur_tgt = path.points[cursor_idx]
    sx = cur_tgt[0] - prev[0]
    sy = cur_tgt[1] - prev[1]
    seg_len2 = sx * sx + sy * sy
    if seg_len2 <= 1e-6:
        return 0.0
    seg_len = math.sqrt(seg_len2)
    rx = cur_x - prev[0]
    ry = cur_y - prev[1]
    return (sx * ry - sy * rx) / seg_len


def _preview_curvature(path: Path, cursor_idx: int, cum_arc: np.ndarray) -> float:
    """Preview curvature for SPEED (rad/m): the MAX local turn-rate |Δyaw|/Δarc over
    the next SPEED_PREVIEW_PTS recorded points ahead of the cursor. Mirrors
    ins_ctrl.c ctrl_preview_curvature(). Unlike the FF ±2-pt net-difference κ — which
    cancels to ~0 at sharp corners / S-bends / cusps and let the car enter too fast
    (measured: steering saturated, xte ~0.7m) — the windowed max both anticipates
    (slows before the corner) and is cusp-robust.
    FIDELITY NOTE: firmware uses recorded enc_dist for Δarc and noisy recorded yaw;
    here cum_arc (geometric) + clean yaw make the sim slightly optimistic. Trends only."""
    total = len(path)
    kmax = 0.0
    for j in range(SPEED_PREVIEW_PTS):
        ia = cursor_idx + j
        ib = ia + 1
        if ib >= total:
            break
        dyaw_deg = abs(normalize_angle_deg(path.points[ib][2] - path.points[ia][2]))
        ds_mm = abs(float(cum_arc[ib] - cum_arc[ia]))
        if ds_mm >= FF_MIN_DS_MM:
            k = math.radians(dyaw_deg) / (ds_mm * 0.001)
            if k > kmax:
                kmax = k
    return kmax


def _advance_cursor(path: Path, cursor_idx: int, cur_x: float, cur_y: float,
                    reach: float) -> int:
    """Cursor advance for the simulator.

    ASSUMPTION: the C playback advances cursor based on signed encoder mileage
    along the recorded path (play_segment_reached + reach-distance fallback).
    We don't have an encoder here, so we use the geometric equivalent: project
    current pos onto the segment (cursor -> cursor+1) ahead and advance when
    the projection passes the segment end (proj >= 1.0), OR when we are within
    reach_threshold of the target. This mirrors C behaviour on forward-monotonic
    paths and prevents the cursor from getting stuck when the car starts with a
    lateral offset that puts dist(target) > reach_threshold."""
    while cursor_idx < len(path) - 1:
        tgt = path.points[cursor_idx]
        nxt = path.points[cursor_idx + 1]
        # Distance reach to current target.
        dist = math.hypot(cur_x - tgt[0], cur_y - tgt[1])
        if dist <= reach:
            cursor_idx += 1
            continue
        # Projection onto the FORWARD segment (cursor -> cursor+1). When proj>=1
        # we have walked past the current target along the path direction.
        sx = nxt[0] - tgt[0]
        sy = nxt[1] - tgt[1]
        seg2 = sx * sx + sy * sy
        if seg2 > 1e-6:
            rx = cur_x - tgt[0]
            ry = cur_y - tgt[1]
            proj = (sx * rx + sy * ry) / seg2
            if proj >= 1.0:
                cursor_idx += 1
                continue
        break
    return cursor_idx


def build_velocity_profile(path: Path, params: Params, cum_arc=None,
                           v_start_ms: float = None) -> np.ndarray:
    """① Forward-backward velocity profile over a FIXED path → per-point speed cap
    (m/s). **Mirrors speed_planner.plan_speed** (the validated PC planner) so the
    firmware port stays consistent — independent forward/backward sweeps then min.
    Pure scalar loops, no library calls → directly C-portable (runs once at
    playback start, mirrors planned ins_ctrl.c builder).

    Steps — in-place forward-then-backward (single array; equals plan_speed's
    independent min(v_f,v_b) for these monotone passes, but uses ONE buffer so the
    firmware can store it as a single int16 mm/s array):
      1. corner cap     v[i]  = min(v_max, sqrt(a_lat_max/|κ[i]|)), floored
      2. forward pass   v[i]  = min(v[i], sqrt(v[i-1]^2 + 2·a_accel·ds))   (accel)
      3. backward pass  v[i]  = min(v[i], sqrt(v[i+1]^2 + 2·a_brake·ds))   (brake/early-decel)
    κ = |Δyaw|/Δs central difference (recorded yaw / enc_dist on the car).
    v_max = params.speed_mm_s/1000 (FULL_SPEED base); v_end = params.vprof_v_end_ms.
    """
    n = len(path)
    if cum_arc is None:
        cum_arc = path.cumulative_arc()
    v_max = params.speed_mm_s / 1000.0
    if n < 2:
        return np.full(max(n, 1), v_max, dtype=float)
    s_m = np.asarray(cum_arc, dtype=float) / 1000.0   # arc length, m

    # 1. corner cap from path curvature (central |Δyaw|/Δs).
    v = np.full(n, v_max, dtype=float)
    for i in range(n):
        ia = max(0, i - 1)
        ib = min(n - 1, i + 1)
        dyaw = math.radians(abs(normalize_angle_deg(
            path.points[ib][2] - path.points[ia][2])))
        ds = s_m[ib] - s_m[ia]
        kappa = (dyaw / ds) if ds > 1e-6 else 0.0
        if kappa > VPROF_KAPPA_MIN:
            vc = math.sqrt(params.a_lat_max / kappa)
            if vc < v[i]:
                v[i] = vc
        if v[i] < VPROF_V_FLOOR_MS:
            v[i] = VPROF_V_FLOOR_MS

    # 2. forward pass (accel-limited rise). v_start caps point 0 when given.
    if v_start_ms is not None and v_start_ms > 0.0:
        v[0] = min(v[0], v_start_ms)
    for i in range(1, n):
        ds = max(0.0, s_m[i] - s_m[i - 1])
        v_reach = math.sqrt(v[i - 1] * v[i - 1] + 2.0 * params.a_accel * ds)
        if v[i] > v_reach:
            v[i] = v_reach

    # 3. backward pass (brake-limited; forces decel BEFORE corners). v_end caps last.
    v[n - 1] = min(v[n - 1], max(params.vprof_v_end_ms, VPROF_V_FLOOR_MS))
    for i in range(n - 2, -1, -1):
        ds = max(0.0, s_m[i + 1] - s_m[i])
        v_stop = math.sqrt(v[i + 1] * v[i + 1] + 2.0 * params.a_brake * ds)
        if v[i] > v_stop:
            v[i] = v_stop
        if v[i] < VPROF_V_FLOOR_MS:
            v[i] = VPROF_V_FLOOR_MS
    return v


def simulate(path: Path, params: Params,
             init_offset_m: float = 0.0,
             init_yaw_offset_deg: float = 0.0,
             max_steps: int = 4000) -> Result:
    """Run kinematic-bicycle sim of Pure Pursuit + recorded-yaw blend control.

    init_offset_m   : lateral offset of start (perpendicular to path[0] yaw).
    init_yaw_offset : initial heading error (deg).
    """
    res = Result()
    if len(path) < 2:
        return res

    cum_arc = path.cumulative_arc()

    # Place car at path start + lateral offset.
    p0 = path.points[0]
    yaw0_rad = math.radians(p0[2])
    nx = -math.sin(yaw0_rad)
    ny = math.cos(yaw0_rad)
    cur_x = p0[0] + init_offset_m * nx
    cur_y = p0[1] + init_offset_m * ny
    cur_yaw_deg = normalize_angle_deg(p0[2] + init_yaw_offset_deg)

    cursor_idx = 0
    speed_ms = params.speed_mm_s / 1000.0
    lost_ticks = 0
    distance = 0.0
    speed_kappa = 0.0   # EMA-filtered preview curvature for curvature-continuous speed
    # ① velocity profile: built once; when present it REPLACES the curvature
    #    heuristic + end-decel (backward pass to v_end already subsumes both).
    vprof = None
    if params.enable_velocity_profile:
        vprof = build_velocity_profile(path, params, cum_arc=cum_arc,
                                       v_start_ms=params.speed_mm_s / 1000.0)
    prev_speed_ms = speed_ms   # ② adaptive lookahead uses last-tick speed (mirrors DR)
    # 2026-05-11 Stage 1.5: servo lag state. steer_actual converges toward
    # steer_cmd via a first-order filter with time constant servo_tau_s.
    # When servo_tau_s == 0, alpha = 1 and steer_actual just tracks steer_cmd
    # (legacy behaviour). The kinematic update below uses steer_actual.
    steer_actual = 0.0
    # 2026-05-12 Stage 1.7: noise/non-ideality state.
    # rng: reproducible if noise_seed != 0, else OS entropy.
    rng = np.random.default_rng(params.noise_seed if params.noise_seed != 0 else None)
    # Magneto yaw bias is a single random draw per run (constant offset).
    if params.enable_magneto_noise:
        # bias drawn from a uniform [-BIAS, +BIAS] - represents the
        # residual after calibration, not the calibration bias itself.
        yaw_bias_deg = rng.uniform(-MAGNETO_YAW_BIAS_DEG, MAGNETO_YAW_BIAS_DEG)
    else:
        yaw_bias_deg = 0.0
    # Backlash state: the previous servo direction (+1 / 0 / -1). When
    # the next steer_actual flips sign, the kinematic effective steer
    # gets a one-step subtraction of SERVO_BACKLASH_DEG until the gear
    # play is taken up again.
    prev_steer_sign = 0
    backlash_carry_deg = 0.0

    for step in range(max_steps):
        # 0. Compute controller-visible (noisy) state. Default is identity:
        # the controller sees the true state. With noise enabled the
        # controller is fed a perturbed version, while the kinematic
        # update at the end of the step uses the TRUE state. This mirrors
        # the real car, where INS / magnetometer / encoder estimates feed
        # the Pure Pursuit math but the physical car responds to commands.
        obs_x       = cur_x
        obs_y       = cur_y
        obs_yaw_deg = cur_yaw_deg
        if params.enable_encoder_noise:
            obs_x += rng.normal(0.0, ENCODER_POSITION_NOISE_STD_M)
            obs_y += rng.normal(0.0, ENCODER_POSITION_NOISE_STD_M)
        if params.enable_magneto_noise:
            obs_yaw_deg = normalize_angle_deg(
                cur_yaw_deg + yaw_bias_deg
                + rng.normal(0.0, MAGNETO_YAW_NOISE_STD_DEG))

        # 1. Advance cursor based on controller-visible position.
        cursor_idx = _advance_cursor(path, cursor_idx, obs_x, obs_y,
                                     params.reach_threshold_m)

        # 2. Cursor target -> tgt_dist (computed against observed pos so
        # the lost-path watchdog also fires on observed drift, matching
        # the firmware which only knows about INS estimate).
        tgt = path.points[cursor_idx]
        dist = math.hypot(tgt[0] - obs_x, tgt[1] - obs_y)

        # 3. Cross-track error (observed).
        xte = _compute_xte(path, cursor_idx, obs_x, obs_y)

        # 4. Lookahead point with fractional interpolation.
        # ② velocity-adaptive lookahead: L = clip(v·T, min, max). v = last-tick
        #    speed (mirrors firmware reading measured DR speed). Off → fixed value.
        if params.enable_adaptive_lookahead:
            la_mm_eff = prev_speed_ms * params.lookahead_time_s * 1000.0
            la_mm_eff = max(float(params.lookahead_min_mm),
                            min(float(params.lookahead_max_mm), la_mm_eff))
        else:
            la_mm_eff = float(params.lookahead_mm)
        idx_a, idx_b, frac = find_lookahead_frac(path, cursor_idx,
                                                 la_mm_eff, cum_arc)
        # Fix A: end-of-path lookahead virtual extension (mirror ins_ctrl.c). When
        # find_lookahead_frac clamps to the last point (remaining arc < lookahead),
        # extrude the aim point along the end-segment direction by the leftover
        # lookahead so la_dist stays ~lookahead instead of collapsing to 0.
        # end_extend_mm > 0  <=>  idx_b == idx_a (clamp); equals C's la_extend_mm.
        _ci = min(int(cursor_idx), len(cum_arc) - 1)
        end_extend_mm = la_mm_eff - float(cum_arc[-1] - cum_arc[_ci])
        pa = path.points[idx_a]
        if idx_b == idx_a:
            la_x, la_y, la_yaw = pa
            last = len(path) - 1
            if end_extend_mm > 0.0 and idx_a == last and last >= 1:
                p_prev = path.points[last - 1]
                ex = pa[0] - p_prev[0]
                ey = pa[1] - p_prev[1]
                seg_len = math.hypot(ex, ey)
                if seg_len > 1e-4:               # 0.1mm guard
                    rem_m = end_extend_mm / 1000.0
                    la_x = pa[0] + (ex / seg_len) * rem_m
                    la_y = pa[1] + (ey / seg_len) * rem_m
                    # la_yaw keeps end-segment heading pa[2] (== extrusion dir).
        else:
            pb = path.points[idx_b]
            la_x = pa[0] + frac * (pb[0] - pa[0])
            la_y = pa[1] + frac * (pb[1] - pa[1])
            # Shortest-path yaw lerp.
            dyaw = normalize_angle_deg(pb[2] - pa[2])
            la_yaw = normalize_angle_deg(pa[2] + frac * dyaw)

        # 5. Geometric and recorded yaw aim, both computed against
        # observed position.
        la_dx = la_x - obs_x
        la_dy = la_y - obs_y
        la_dist = math.hypot(la_dx, la_dy)
        geo_yaw = math.degrees(math.atan2(la_dy, la_dx))
        rec_yaw = la_yaw

        # 5b. Aim selection. la_dist < 0.15m -> drop geo_yaw (overrun guard; now
        # rarely hit because A keeps la_dist ~lookahead). Fix B: within the end
        # zone (end_extend_mm > 0) progressively blend aim toward recorded yaw by
        # depth t = end_extend_mm/lookahead, capped at END_YAW_BLEND_MAX.
        if la_dist < LA_GEO_MIN_M:
            base_aim = rec_yaw
        else:
            base_aim = lerp_angle_deg(geo_yaw, rec_yaw, params.head_blend)
        if end_extend_mm > 0.0 and la_mm_eff > 0:
            t = end_extend_mm / la_mm_eff
            end_blend = min(END_YAW_BLEND_MAX, END_YAW_BLEND_MAX * t)
            aim_yaw = lerp_angle_deg(base_aim, rec_yaw, end_blend)
        else:
            aim_yaw = base_aim

        yaw_err = normalize_angle_deg(aim_yaw - obs_yaw_deg)

        # 6. Actuator P-control + clamp -> command.
        steer_raw = params.steer_kp * yaw_err
        steer_cmd = max(-params.steer_limit_deg,
                        min(params.steer_limit_deg, steer_raw))
        saturated = abs(steer_raw) >= params.steer_limit_deg

        # 6b. Servo first-order lag: steer_actual chases steer_cmd. The mixing
        # coefficient alpha = dt / (tau + dt) makes the response converge
        # exactly per the discrete equivalent of a continuous first-order LPF.
        if params.servo_tau_s > 0.0:
            alpha = params.dt_s / (params.servo_tau_s + params.dt_s)
            steer_actual += alpha * (steer_cmd - steer_actual)
        else:
            steer_actual = steer_cmd

        # 6b'. Servo non-linearities. The deadband zeroes the kinematic
        # steer if the lagged actual is too small to move the servo.
        # Backlash subtracts a constant offset for the FIRST step after a
        # sign change, simulating gear play before the new direction's
        # tooth engages.
        steer_effective = steer_actual
        if params.enable_servo_backlash and SERVO_BACKLASH_DEG > 0.0:
            new_sign = (1 if steer_actual > 0
                        else -1 if steer_actual < 0 else 0)
            if new_sign != 0 and prev_steer_sign != 0 \
                    and new_sign != prev_steer_sign:
                # direction reversal: consume one backlash
                backlash_carry_deg = SERVO_BACKLASH_DEG * new_sign
            if abs(backlash_carry_deg) > 1e-6:
                # Subtract carry in the direction of motion; clamp at 0.
                if new_sign > 0:
                    steer_effective = max(0.0, steer_actual
                                          - backlash_carry_deg)
                    backlash_carry_deg = max(0.0, backlash_carry_deg
                                             - abs(steer_actual))
                elif new_sign < 0:
                    steer_effective = min(0.0, steer_actual
                                          - backlash_carry_deg)
                    backlash_carry_deg = min(0.0, backlash_carry_deg
                                             + abs(steer_actual))
            prev_steer_sign = new_sign
        if params.enable_servo_deadband and SERVO_DEADBAND_DEG > 0.0:
            if abs(steer_effective) < SERVO_DEADBAND_DEG:
                steer_effective = 0.0

        # 6c. Speed selection. Two layers:
        #     a) base speed = path_speed_profile[cursor] if given, else constant.
        #     b) shaping: curvature-continuous (2026-06, mirrors ins_ctrl.c) by
        #        default; legacy binary cursor-slowdown if explicitly selected.
        if vprof is not None:
            # ① profile is the speed cap: encodes corner-limit + accel/brake +
            #    end-approach. Skips the curvature heuristic AND end-decel (6d).
            effective_speed_ms = float(vprof[min(int(cursor_idx), len(vprof) - 1)])
            base_speed_ms = effective_speed_ms
        else:
            if params.path_speed_profile is not None:
                prof = params.path_speed_profile
                idx_speed = min(int(cursor_idx), len(prof) - 1)
                base_speed_ms = float(prof[idx_speed])
            else:
                base_speed_ms = speed_ms
            if params.enable_curvature_speed:
                # Preview curvature from the cursor (max local turn-rate over the window
                # ahead), EMA-filtered (mirrors ins_ctrl.c ctrl_preview_curvature + speed).
                kappa_raw = _preview_curvature(path, cursor_idx, cum_arc)
                speed_kappa += FF_LP_ALPHA * (kappa_raw - speed_kappa)
                floor_ms = base_speed_ms * SLOW_RATIO
                v = base_speed_ms / (1.0 + params.curve_speed_gain * speed_kappa)
                effective_speed_ms = max(floor_ms, min(base_speed_ms, v))
            elif params.enable_cursor_slowdown and dist < CRUISE_DIST_M:
                effective_speed_ms = base_speed_ms * SLOW_RATIO
            else:
                effective_speed_ms = base_speed_ms

            # 6d. End-of-path deceleration (mirrors ins_ctrl.c): ramp the cap down to
            #     an approach speed over the last END_DECEL_DIST_MM so the car eases
            #     onto the final point. (Profile path skips this — backward pass covers it.)
            _ci = min(int(cursor_idx), len(cum_arc) - 1)
            _rem_mm = abs(float(cum_arc[-1] - cum_arc[_ci]))
            if _rem_mm < END_DECEL_DIST_MM:
                _frac = _rem_mm / END_DECEL_DIST_MM
                _end_cap = END_APPROACH_MS + (base_speed_ms - END_APPROACH_MS) * _frac
                if effective_speed_ms > _end_cap:
                    effective_speed_ms = _end_cap

        prev_speed_ms = effective_speed_ms   # ② feed next-tick adaptive lookahead

        # 7. Lost-path watchdog.
        if dist > LOST_DIST_M:
            lost_ticks += 1
        else:
            lost_ticks = 0
        if lost_ticks >= LOST_HOLD_TICKS:
            res.lost = True
            res.t.append(step * params.dt_s)
            res.cur_x.append(cur_x)
            res.cur_y.append(cur_y)
            res.cur_yaw.append(cur_yaw_deg)
            res.xte.append(xte)
            res.yaw_err.append(yaw_err)
            res.steer_cmd.append(steer_cmd)
            res.steer_actual.append(steer_actual)
            res.cursor_idx.append(cursor_idx)
            res.saturated.append(saturated)
            res.la_idx.append(idx_a)
            res.la_dist.append(la_dist)
            res.distance_m.append(distance)
            res.speed_used.append(effective_speed_ms)
            break

        # 8. Record state BEFORE integrating (so logged state matches command).
        res.t.append(step * params.dt_s)
        res.cur_x.append(cur_x)
        res.cur_y.append(cur_y)
        res.cur_yaw.append(cur_yaw_deg)
        res.xte.append(xte)
        res.yaw_err.append(yaw_err)
        res.steer_cmd.append(steer_cmd)
        res.steer_actual.append(steer_actual)
        res.cursor_idx.append(cursor_idx)
        res.saturated.append(saturated)
        res.la_idx.append(idx_a)
        res.la_dist.append(la_dist)
        res.distance_m.append(distance)
        res.speed_used.append(effective_speed_ms)

        # 9. Kinematic bicycle update. Uses steer_effective (post-lag +
        # deadband + backlash) and the slow-down-shaped speed so the
        # integrated trajectory reflects what the car physically does,
        # not the raw command.
        steer_rad = math.radians(steer_effective)
        yaw_rad = math.radians(cur_yaw_deg)
        cur_x += effective_speed_ms * math.cos(yaw_rad) * params.dt_s
        cur_y += effective_speed_ms * math.sin(yaw_rad) * params.dt_s
        cur_yaw_deg = normalize_angle_deg(
            cur_yaw_deg + math.degrees(
                effective_speed_ms / WHEELBASE_M
                * math.tan(steer_rad) * params.dt_s))
        distance += effective_speed_ms * params.dt_s

        # 10. Termination - finished path.
        if cursor_idx >= len(path) - 1 and dist <= params.reach_threshold_m:
            res.finished = True
            break

    return res


# ============================================================================
# Sweep + reporting
# ============================================================================

def sweep(path: Path, kp_list, la_list, blend_list, base: Params,
          init_offset_m: float = 0.5):
    """Run all combos. Returns dict keyed by (kp, la, blend) -> Result."""
    out = {}
    for kp in kp_list:
        for la in la_list:
            for bl in blend_list:
                p = Params(steer_kp=kp, lookahead_mm=la, head_blend=bl,
                           speed_mm_s=base.speed_mm_s,
                           steer_limit_deg=base.steer_limit_deg,
                           dt_s=base.dt_s)
                out[(kp, la, bl)] = simulate(path, p,
                                             init_offset_m=init_offset_m)
    return out


def summarize(results) -> str:
    """Build summary table string. Columns: KP, LA, BLEND, max|xte|, ss xte,
    %sat, lost, finished."""
    lines = []
    header = ("  KP   LA  BLEND   max|xte|(mm)  ss|xte|(mm)   %sat  lost  finished")
    lines.append(header)
    lines.append("-" * len(header))
    for key, r in results.items():
        kp, la, bl = key
        if not r.xte:
            lines.append(f"{kp:5.2f} {la:4d} {bl:5.2f}     no-data")
            continue
        xte_arr = np.array(r.xte)
        n = len(xte_arr)
        max_xte_mm = np.max(np.abs(xte_arr)) * 1000.0
        # Steady-state: last 25% of run.
        tail = xte_arr[max(0, int(0.75 * n)):]
        ss_mm = np.mean(np.abs(tail)) * 1000.0
        pct_sat = 100.0 * np.mean(r.saturated)
        lines.append(
            f"{kp:5.2f} {la:4d} {bl:5.2f} "
            f"{max_xte_mm:13.1f} {ss_mm:12.1f} {pct_sat:6.1f} "
            f"{str(r.lost):>5}  {str(r.finished):>8}"
        )
    return "\n".join(lines)


def plot_sweep(results, path: Path, out_png: str, title: str):
    """One subplot of xte vs distance per combo + path/trajectory overlay."""
    keys = list(results.keys())
    n = len(keys)
    fig = plt.figure(figsize=(14, 8))

    # XTE vs distance (left).
    ax1 = fig.add_subplot(2, 2, 1)
    for key, r in results.items():
        kp, la, bl = key
        if not r.xte:
            continue
        ax1.plot(np.array(r.distance_m), np.array(r.xte) * 1000.0,
                 label=f"KP={kp} LA={la}", linewidth=1.0)
    ax1.set_xlabel("distance traveled (m)")
    ax1.set_ylabel("xte (mm)")
    ax1.set_title("Cross-track error vs distance")
    ax1.grid(True, alpha=0.3)
    ax1.legend(fontsize=7, loc="best")
    ax1.axhline(0, color="k", linewidth=0.5)

    # XY trajectories with path overlay (right).
    ax2 = fig.add_subplot(2, 2, 2)
    px = [p[0] for p in path.points]
    py = [p[1] for p in path.points]
    ax2.plot(px, py, "k--", linewidth=1.5, label="path")
    for key, r in results.items():
        kp, la, bl = key
        if not r.cur_x:
            continue
        ax2.plot(r.cur_x, r.cur_y, label=f"KP={kp} LA={la}", linewidth=0.8)
    ax2.set_xlabel("x (m)")
    ax2.set_ylabel("y (m)")
    ax2.set_title("Trajectory (path = dashed)")
    ax2.set_aspect("equal", adjustable="datalim")
    ax2.grid(True, alpha=0.3)
    ax2.legend(fontsize=7, loc="best")

    # Steer command vs distance.
    ax3 = fig.add_subplot(2, 2, 3)
    for key, r in results.items():
        kp, la, bl = key
        if not r.steer_cmd:
            continue
        ax3.plot(np.array(r.distance_m), r.steer_cmd,
                 label=f"KP={kp} LA={la}", linewidth=0.8)
    ax3.axhline(STEER_LIMIT_DEG, color="r", linewidth=0.5, linestyle=":")
    ax3.axhline(-STEER_LIMIT_DEG, color="r", linewidth=0.5, linestyle=":")
    ax3.set_xlabel("distance traveled (m)")
    ax3.set_ylabel("steer cmd (deg)")
    ax3.set_title("Steering command (dotted = +/-29 limit)")
    ax3.grid(True, alpha=0.3)
    ax3.legend(fontsize=7, loc="best")

    # Yaw error vs distance.
    ax4 = fig.add_subplot(2, 2, 4)
    for key, r in results.items():
        kp, la, bl = key
        if not r.yaw_err:
            continue
        ax4.plot(np.array(r.distance_m), r.yaw_err,
                 label=f"KP={kp} LA={la}", linewidth=0.8)
    ax4.set_xlabel("distance traveled (m)")
    ax4.set_ylabel("yaw err (deg)")
    ax4.set_title("Heading error")
    ax4.grid(True, alpha=0.3)
    ax4.legend(fontsize=7, loc="best")

    fig.suptitle(title)
    fig.tight_layout()
    fig.savefig(out_png, dpi=110)
    plt.close(fig)


# ============================================================================
# Main
# ============================================================================

def main(argv):
    here = os.path.dirname(os.path.abspath(__file__))
    out_png = os.path.join(here, "sim_pure_pursuit_sweep.png")

    # Pick path: recording arg or default 175-pt arc.
    if len(argv) > 1 and os.path.isfile(argv[1]):
        print(f"[sim] loading recorded path: {argv[1]}")
        path = load_recording(argv[1])
        title = f"Recorded path ({len(path)} pts) - sweep"
    else:
        # 175-point arc, ~3m radius, 180deg sweep -> arc length pi*3 = 9.42m,
        # spacing 0.054m. Matches user's "175 dian wandao" reference.
        arc_len = math.pi * 3.0
        dx = arc_len / 174.0
        path = make_arc(radius_m=3.0, sweep_deg=180.0, dx=dx)
        title = f"175-pt arc R=3m sweep=180 deg, init_offset=0.5m"
        print(f"[sim] default path: 175-pt 180deg arc, "
              f"length={arc_len:.2f}m points={len(path)}")

    base = Params()
    kp_list = [1.0, 2.5, 5.0]
    la_list = [150, 300, 600]
    blend_list = [0.0]

    print(f"[sim] sweep: KP={kp_list} LA={la_list} BLEND={blend_list}")
    results = sweep(path, kp_list, la_list, blend_list, base,
                    init_offset_m=0.5)

    table = summarize(results)
    print()
    print(table)
    print()

    plot_sweep(results, path, out_png, title)
    print(f"[sim] plot saved: {out_png}")
    print(f"[sim] done.")


if __name__ == "__main__":
    main(sys.argv)
