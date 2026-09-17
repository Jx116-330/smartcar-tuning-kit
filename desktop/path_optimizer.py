"""
path_optimizer.py - Stage 2: path geometry re-planning.

Reads a recorded TELMISSION trajectory (or a future LEARN-dump record point
sequence), then produces an optimized point sequence using one of:

  * savgol      - Savitzky-Golay filter on (x, y) separately. Removes IMU
                  drift / encoder jitter but does not reshape the path.
  * min_curv    - lateral-offset optimization along the path's local normal,
                  minimizing the discrete second-difference energy (proxy for
                  squared curvature integral). The classical "minimum-
                  curvature racing line" formulation.
  * shorten     - lateral-offset optimization that minimizes total arc length
                  instead, useful when the path is too snake-shaped relative
                  to its corridor budget.

The optimizer never touches start and end points, and clamps lateral offset
to a configurable budget (default 0.30m). Anything past that is left to a
future on-car re-LEARN pass.

REAL-CAR SAFETY NOTE
--------------------
This module is a PURE GEOMETRY transformer. It does NOT read
vehicle_params.WHEELBASE_M (which is currently USER_SIM and not measured).
The optimized path is only checked against feasibility using the OBSERVED
minimum curvature of the input recording itself - i.e. we never produce a
path tighter than what the car has already demonstrated it can drive. This
sidesteps the WB-uncertainty issue: even if the kinematic limit is wrong,
the observed limit is real.

Usage:
    python path_optimizer.py recordings/tmp_run_xte_test3.json \
        --method min_curv --max-offset 0.30 \
        --save-png paths/xte_test3_opt.png \
        --save-json paths/xte_test3_opt.json
"""
from __future__ import annotations

import argparse
import json
import math
import sys
import time
from dataclasses import dataclass
from pathlib import Path as _FsPath
from typing import Optional

import numpy as np

try:
    from scipy.optimize import minimize
    from scipy.signal import savgol_filter
    _HAS_SCIPY = True
except ImportError:
    _HAS_SCIPY = False


# ============================================================================
# Path container
# ============================================================================

@dataclass
class Path:
    """A sequence of waypoints in the global frame.

    Fields:
        x, y       - position arrays (m), N elements
        yaw_deg    - heading at each waypoint (deg)
        meta       - dict of provenance / sampling info; carried through
                     optimization for the output JSON header
    """
    x: np.ndarray
    y: np.ndarray
    yaw_deg: np.ndarray
    meta: dict

    def __len__(self) -> int:
        return len(self.x)

    # ---------- factories ------------------------------------------------

    @classmethod
    def from_xy(cls, x, y, yaw_deg=None, meta=None) -> "Path":
        x = np.asarray(x, dtype=float).ravel()
        y = np.asarray(y, dtype=float).ravel()
        if yaw_deg is None:
            yaw_deg = cls._derive_yaw(x, y)
        else:
            yaw_deg = np.asarray(yaw_deg, dtype=float).ravel()
        return cls(x=x, y=y, yaw_deg=yaw_deg, meta=meta or {})

    @classmethod
    def from_recording_json(cls, file_path: str) -> "Path":
        """Load from a TELMISSION recording (record_session / tmp_capture).
        Uses cpx / cpy / cyaw of the engaged frames; this is the trajectory
        the car ACTUALLY drove during a RETURN, which is the best stand-in
        for the LEARN path we have without a TRACK DUMP. Future work: add a
        from_learn_dump_json() that reads the 8-field ins_record_point_t
        schema directly."""
        with open(file_path, "r", encoding="utf-8") as f:
            frames = json.load(f)
        if isinstance(frames, dict) and "frames" in frames:
            frames = frames["frames"]
        eng = [f for f in frames if f.get("eng", 0) == 1]
        if len(eng) < 5:
            raise ValueError(f"{file_path}: only {len(eng)} engaged frames")
        x = np.array([f.get("cpx", 0.0) for f in eng], dtype=float)
        y = np.array([f.get("cpy", 0.0) for f in eng], dtype=float)
        yaw = np.array([f.get("cyaw", 0.0) for f in eng], dtype=float)
        # Dedupe near-duplicate consecutive points (cursor not yet advancing).
        keep = [0]
        for i in range(1, len(x)):
            if math.hypot(x[i] - x[keep[-1]], y[i] - y[keep[-1]]) > 0.01:
                keep.append(i)
        keep = np.asarray(keep)
        meta = {
            "source": "telmission_recording",
            "file": file_path,
            "n_engaged_raw": len(eng),
            "n_after_dedup": len(keep),
            "params_at_record": {
                "kp":   eng[0].get("kp"),
                "la":   eng[0].get("la"),
                "blnd": eng[0].get("blnd"),
                "pts":  eng[0].get("pts"),
            },
        }
        return cls.from_xy(x[keep], y[keep], yaw[keep], meta)

    @classmethod
    def from_learn_dump_json(cls, file_path: str) -> "Path":
        """Load from an INS record dump (ins_record_point_t array).
        Schema: [{t_ms, px_m, py_m, vx_ms, vy_ms, yaw_deg, enc_spd_mm_s,
        enc_dist_mm}, ...]. Currently not produced by any tool; placeholder
        for when TRACK DUMP gets implemented in record_session.py."""
        with open(file_path, "r", encoding="utf-8") as f:
            pts = json.load(f)
        if isinstance(pts, dict) and "points" in pts:
            pts = pts["points"]
        x = np.array([p["px_m"] for p in pts], dtype=float)
        y = np.array([p["py_m"] for p in pts], dtype=float)
        yaw = np.array([p["yaw_deg"] for p in pts], dtype=float)
        meta = {"source": "ins_record_dump", "file": file_path,
                "n_points": len(pts)}
        return cls.from_xy(x, y, yaw, meta)

    # ---------- derived geometry ----------------------------------------

    @staticmethod
    def _derive_yaw(x: np.ndarray, y: np.ndarray) -> np.ndarray:
        """Approximate yaw from forward differences at every point.
        Endpoints use one-sided diffs; interior uses centered diffs."""
        dx = np.zeros_like(x); dy = np.zeros_like(y)
        dx[1:-1] = (x[2:] - x[:-2]) / 2.0
        dy[1:-1] = (y[2:] - y[:-2]) / 2.0
        dx[0]  = x[1]  - x[0];   dy[0]  = y[1]  - y[0]
        dx[-1] = x[-1] - x[-2];  dy[-1] = y[-1] - y[-2]
        return np.degrees(np.arctan2(dy, dx))

    def cumulative_arc(self) -> np.ndarray:
        """Cumulative path length in meters; same length as the point array,
        starting at 0."""
        if len(self.x) < 2:
            return np.zeros_like(self.x)
        dx = np.diff(self.x); dy = np.diff(self.y)
        seg = np.hypot(dx, dy)
        return np.concatenate(([0.0], np.cumsum(seg)))

    def total_length(self) -> float:
        return float(self.cumulative_arc()[-1])

    def normals(self) -> tuple:
        """Unit normal vectors at every point (perpendicular to local
        tangent, rotated 90deg CCW). Returns (nx, ny) arrays."""
        n = len(self.x)
        tx = np.zeros(n); ty = np.zeros(n)
        tx[1:-1] = self.x[2:] - self.x[:-2]
        ty[1:-1] = self.y[2:] - self.y[:-2]
        tx[0]  = self.x[1]  - self.x[0];   ty[0]  = self.y[1]  - self.y[0]
        tx[-1] = self.x[-1] - self.x[-2];  ty[-1] = self.y[-1] - self.y[-2]
        mag = np.hypot(tx, ty)
        mag[mag < 1e-9] = 1.0
        tx /= mag; ty /= mag
        # Normal = tangent rotated +90deg.
        return -ty, tx

    def curvature(self) -> np.ndarray:
        """Signed curvature kappa per point in 1/m. Endpoints set to 0."""
        n = len(self.x)
        kap = np.zeros(n)
        if n < 3:
            return kap
        for i in range(1, n - 1):
            dx1 = self.x[i] - self.x[i-1]; dy1 = self.y[i] - self.y[i-1]
            dx2 = self.x[i+1] - self.x[i]; dy2 = self.y[i+1] - self.y[i]
            cross = dx1 * dy2 - dy1 * dx2
            len1 = math.hypot(dx1, dy1)
            len2 = math.hypot(dx2, dy2)
            len3 = math.hypot(self.x[i+1] - self.x[i-1],
                              self.y[i+1] - self.y[i-1])
            denom = len1 * len2 * len3
            if denom > 1e-9:
                kap[i] = 2.0 * cross / denom
        return kap

    def max_abs_curvature(self) -> float:
        c = self.curvature()
        return float(np.max(np.abs(c))) if c.size else 0.0

    def observed_min_radius(self) -> float:
        """1 / max(|kappa|). Tightest turn the input path actually contained.
        Used as a feasibility bound for the optimizer so we never propose
        something tighter than what was demonstrated."""
        k = self.max_abs_curvature()
        return 1.0 / k if k > 1e-9 else float("inf")


# ============================================================================
# Smoothing
# ============================================================================

def smooth_savgol(path: Path, window: int = 11, polyorder: int = 3) -> Path:
    """Savitzky-Golay on x and y independently; re-derive yaw from new x, y.

    Removes high-frequency jitter (IMU drift, encoder noise) without
    reshaping the path's mean geometry. Use as a pre-step before
    min_curvature so the optimizer doesn't chase noise."""
    if not _HAS_SCIPY:
        raise RuntimeError("scipy is required for savgol smoothing")
    if window % 2 == 0:
        window += 1
    if window > len(path.x):
        window = len(path.x) if len(path.x) % 2 == 1 else len(path.x) - 1
    if window < polyorder + 2:
        return path  # path too short to smooth meaningfully
    x_sm = savgol_filter(path.x, window, polyorder)
    y_sm = savgol_filter(path.y, window, polyorder)
    # Endpoints: pin to original values so the path still starts and ends
    # exactly where the recording said.
    x_sm[0] = path.x[0];   x_sm[-1] = path.x[-1]
    y_sm[0] = path.y[0];   y_sm[-1] = path.y[-1]
    meta = dict(path.meta)
    meta["smoothing"] = {"method": "savgol", "window": window,
                         "polyorder": polyorder}
    return Path.from_xy(x_sm, y_sm, meta=meta)


# ============================================================================
# Minimum curvature optimization
# ============================================================================

def _second_diff_matrix(n: int) -> np.ndarray:
    """N x N tridiagonal second-difference operator. Multiplying this matrix
    by a column vector v returns v[i-1] - 2*v[i] + v[i+1] for interior rows
    and zeros for the first/last (boundary)."""
    A = np.zeros((n, n))
    for i in range(1, n - 1):
        A[i, i-1] =  1.0
        A[i, i]   = -2.0
        A[i, i+1] =  1.0
    return A


def min_curvature(path: Path, max_offset_m: float = 0.30,
                  smoothness_weight: float = 0.0,
                  pin_yaw_endpoints: bool = True,
                  enforce_feasibility: bool = True,
                  feasibility_margin: float = 1.05,
                  weights=None,
                  max_offset_per_point=None) -> Path:
    """Lateral-offset optimization minimizing discrete curvature energy.

    Decision variable d in R^N: lateral offset along the local normal at
    each waypoint. Cost:
        J(d) = || A * (P0 + d * n) ||^2  +  w * || diff(d) ||^2
    where A is the second-difference matrix and n the normal vectors.

    Constraints:
        d[0] = d[-1] = 0                          (start/end fixed)
        |d[i]| <= max_offset_m                    (corridor budget)

    enforce_feasibility=True (default) guarantees the output's tightest
    turn is no tighter than the input's (within feasibility_margin). The
    optimizer first solves the unconstrained QP, then if the result has
    higher max curvature than the input, scales the offset vector by
    bisection until max|kappa_opt| <= max|kappa_orig| * margin. This
    sidesteps the WHEELBASE_M=USER_SIM trust issue: if the car drove the
    original recording, it can drive any path no tighter than that.

    Pass enforce_feasibility=False to get full racing-line / apex-cut
    behavior; the output may demand a smaller turn radius than the
    original recording and is then only safe to drive if the kinematic
    limit (which depends on the unverified WHEELBASE_M) allows it.

    Returns a new Path containing the optimized waypoints."""
    if not _HAS_SCIPY:
        raise RuntimeError("scipy is required for min_curvature optimization")

    n = len(path)
    if n < 5:
        return path  # nothing to optimize

    nx, ny = path.normals()
    A = _second_diff_matrix(n)

    # Optional per-point weights (2026-06-09, minimax-IRLS 用):
    # J = sum_i w_i * (rx_i^2 + ry_i^2) = ||W^0.5 A (P+dn)||^2,等价于
    # 把 A 预乘 diag(sqrt(w))。weights=None 时行为与旧版完全一致。
    if weights is not None:
        w = np.sqrt(np.maximum(np.asarray(weights, dtype=float).ravel(), 0.0))
        A = np.diag(w) @ A

    # Constant terms: A * P0_x and A * P0_y. New coords are P0 + d*n, so
    # A * (P0_x + d * nx) = A_P0x + A * diag(nx) * d  =  c_x + M_x d
    A_nx = A @ np.diag(nx)
    A_ny = A @ np.diag(ny)
    c_x  = A @ path.x
    c_y  = A @ path.y

    # Smoothness regularizer = || D1 * d ||^2 where D1 is first-difference.
    D1 = np.zeros((n - 1, n))
    for i in range(n - 1):
        D1[i, i]   = -1.0
        D1[i, i+1] =  1.0

    def cost(d):
        rx = c_x + A_nx @ d
        ry = c_y + A_ny @ d
        j = float(rx @ rx + ry @ ry)
        if smoothness_weight > 0.0:
            dd = D1 @ d
            j += smoothness_weight * float(dd @ dd)
        return j

    def grad(d):
        rx = c_x + A_nx @ d
        ry = c_y + A_ny @ d
        g = 2.0 * (A_nx.T @ rx + A_ny.T @ ry)
        if smoothness_weight > 0.0:
            dd = D1 @ d
            g += 2.0 * smoothness_weight * (D1.T @ dd)
        return g

    # 逐点 box 约束(2026-06-09):SQP 式外层迭代(rebase)时,每轮的可用偏移
    # = 总走廊预算 − 已用累计偏移,按点给。None 时回退统一 max_offset_m(旧行为)。
    if max_offset_per_point is not None:
        b = np.maximum(np.asarray(max_offset_per_point, dtype=float).ravel(), 0.0)
        bounds = [(0.0, 0.0)] + [(-float(b[i]), float(b[i]))
                                 for i in range(1, n - 1)] + [(0.0, 0.0)]
    else:
        bounds = [(0.0, 0.0)] + [(-max_offset_m, max_offset_m)] * (n - 2) \
                             + [(0.0, 0.0)]

    d0 = np.zeros(n)
    t0 = time.time()
    res = minimize(cost, d0, jac=grad, method="L-BFGS-B", bounds=bounds,
                   options={"maxiter": 500, "ftol": 1e-9})
    elapsed = time.time() - t0

    d_opt = res.x

    # Feasibility enforcement. The unconstrained QP minimizes 2nd-diff
    # energy; on a curved input it tends to apex-cut, producing a path
    # whose tightest point is sharper than the input. We don't want to
    # ship a path the car has never driven, so optionally bisect-scale
    # d_opt down until max|kappa| <= max|kappa_orig| * margin.
    orig_kmax = path.max_abs_curvature()
    feasibility_action = "none"
    scale = 1.0
    if enforce_feasibility and orig_kmax > 1e-9:
        target_kmax = orig_kmax * feasibility_margin
        # Closed form would require a nonlinear constraint; bisection is
        # 2-3 orders cheaper and good to 1mm of offset.
        candidate_path = Path.from_xy(path.x + d_opt * nx,
                                      path.y + d_opt * ny)
        if candidate_path.max_abs_curvature() > target_kmax:
            lo, hi = 0.0, 1.0
            for _ in range(40):  # 1/2^40 precision is overkill
                mid = 0.5 * (lo + hi)
                trial = Path.from_xy(path.x + mid * d_opt * nx,
                                     path.y + mid * d_opt * ny)
                if trial.max_abs_curvature() <= target_kmax:
                    lo = mid
                else:
                    hi = mid
            scale = lo
            d_opt = scale * d_opt
            feasibility_action = f"scaled to {scale:.3f} to keep R_opt >= R_orig"

    x_new = path.x + d_opt * nx
    y_new = path.y + d_opt * ny

    meta = dict(path.meta)
    meta["optimization"] = {
        "method": "min_curvature",
        "max_offset_m": max_offset_m,
        "smoothness_weight": smoothness_weight,
        "enforce_feasibility": enforce_feasibility,
        "feasibility_margin": feasibility_margin,
        "feasibility_action": feasibility_action,
        "feasibility_scale": scale,
        "elapsed_s": elapsed,
        "converged": bool(res.success),
        "cost_initial": float(cost(d0)),
        "cost_final":   float(res.fun),
        "max_offset_used_m": float(np.max(np.abs(d_opt))),
    }
    return Path.from_xy(x_new, y_new, meta=meta)


def shorten_arc(path: Path, max_offset_m: float = 0.30,
                curvature_weight: float = 1.0) -> Path:
    """Minimize arc length with a soft curvature penalty.

    Useful when the input is snake-shaped and the rider wants to 'cut the
    corners' inside the corridor budget. The cost is the discrete arc
    length plus curvature_weight * curvature_energy."""
    if not _HAS_SCIPY:
        raise RuntimeError("scipy is required for shorten_arc")

    n = len(path)
    if n < 5:
        return path

    nx, ny = path.normals()
    A = _second_diff_matrix(n)
    A_nx = A @ np.diag(nx)
    A_ny = A @ np.diag(ny)
    c_x  = A @ path.x
    c_y  = A @ path.y

    def cost(d):
        xs = path.x + d * nx
        ys = path.y + d * ny
        seg = np.hypot(np.diff(xs), np.diff(ys))
        arc = float(np.sum(seg))
        rx = c_x + A_nx @ d
        ry = c_y + A_ny @ d
        kap_energy = float(rx @ rx + ry @ ry)
        return arc + curvature_weight * kap_energy

    bounds = [(0.0, 0.0)] + [(-max_offset_m, max_offset_m)] * (n - 2) \
                         + [(0.0, 0.0)]
    d0 = np.zeros(n)
    t0 = time.time()
    res = minimize(cost, d0, method="L-BFGS-B", bounds=bounds,
                   options={"maxiter": 500, "ftol": 1e-9})
    elapsed = time.time() - t0
    d_opt = res.x

    meta = dict(path.meta)
    meta["optimization"] = {
        "method": "shorten_arc",
        "max_offset_m": max_offset_m,
        "curvature_weight": curvature_weight,
        "elapsed_s": elapsed,
        "converged": bool(res.success),
        "max_offset_used_m": float(np.max(np.abs(d_opt))),
    }
    return Path.from_xy(path.x + d_opt * nx, path.y + d_opt * ny, meta=meta)


# ============================================================================
# Report + safety check
# ============================================================================

def diff_report(orig: Path, opt: Path) -> dict:
    """Summarize the change between input and output paths.

    Includes the feasibility check: the optimized path's tightest turn must
    be no tighter than the input path's tightest turn (we never push the car
    beyond what it has demonstrated). If the check fails we flag it so the
    operator knows to either widen max_offset (more room to relax tight
    corners) or raise smoothness_weight (don't try as hard)."""
    a_orig = orig.total_length()
    a_opt  = opt.total_length()
    k_orig = orig.max_abs_curvature()
    k_opt  = opt.max_abs_curvature()
    r_orig = orig.observed_min_radius()
    r_opt  = opt.observed_min_radius()

    # Lateral offset profile if both paths have equal length.
    if len(orig) == len(opt):
        offsets = np.hypot(opt.x - orig.x, opt.y - orig.y)
        max_off = float(np.max(offsets))
        mean_off = float(np.mean(offsets))
    else:
        max_off = float("nan"); mean_off = float("nan")

    report = {
        "n_points":          {"orig": len(orig),    "opt": len(opt)},
        "arc_length_m":      {"orig": a_orig,       "opt": a_opt,
                              "change_pct": 100.0 * (a_opt - a_orig) / max(a_orig, 1e-9)},
        "max_abs_curvature": {"orig": k_orig,       "opt": k_opt,
                              "change_pct": 100.0 * (k_opt - k_orig) / max(k_orig, 1e-9)},
        "min_radius_m":      {"orig": r_orig,       "opt": r_opt},
        "lateral_offset_m":  {"max":  max_off,      "mean": mean_off},
        "feasibility":       {
            "opt_tighter_than_orig": k_opt > k_orig * 1.05,  # 5% slack
            "feasibility_note":
                "OK" if k_opt <= k_orig * 1.05 else
                "WARN: optimized path tighter than observed minimum radius",
        },
    }
    return report


# ============================================================================
# Plotting
# ============================================================================

def plot_comparison(orig: Path, opt: Path, report: dict,
                    save_path: Optional[str] = None) -> None:
    """Side-by-side XY plot + curvature-along-arc plot."""
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    fig, axes = plt.subplots(1, 2, figsize=(14, 6))

    ax = axes[0]
    ax.plot(orig.x, orig.y, "-", color="#888888", linewidth=1.5,
            label=f"orig (L={report['arc_length_m']['orig']:.2f}m)")
    ax.plot(opt.x,  opt.y,  "-", color="#FF7F00", linewidth=1.6,
            label=f"opt  (L={report['arc_length_m']['opt']:.2f}m, "
                  f"{report['arc_length_m']['change_pct']:+.1f}%)")
    ax.plot(orig.x[0], orig.y[0], "go", markersize=8, label="start")
    ax.plot(orig.x[-1], orig.y[-1], "rs", markersize=8, label="end")
    ax.set_aspect("equal", adjustable="datalim")
    ax.grid(True, alpha=0.3)
    ax.legend(loc="best", fontsize=9)
    ax.set_xlabel("X (m)"); ax.set_ylabel("Y (m)")
    ax.set_title(
        f"XY trajectory  |  R_min: {report['min_radius_m']['orig']:.2f}m → "
        f"{report['min_radius_m']['opt']:.2f}m  |  "
        f"max offset: {report['lateral_offset_m']['max']:.2f}m"
    )

    ax = axes[1]
    arc_orig = orig.cumulative_arc()
    arc_opt  = opt.cumulative_arc()
    ax.plot(arc_orig, orig.curvature(), "-", color="#888888",
            linewidth=1.4, label="orig")
    ax.plot(arc_opt,  opt.curvature(),  "-", color="#FF7F00",
            linewidth=1.4, label="opt")
    ax.axhline(0, color="k", linewidth=0.5)
    ax.grid(True, alpha=0.3)
    ax.legend(loc="best", fontsize=9)
    ax.set_xlabel("arc length (m)"); ax.set_ylabel("signed curvature (1/m)")
    ax.set_title(
        f"Curvature profile  |  max|k|: "
        f"{report['max_abs_curvature']['orig']:.3f} → "
        f"{report['max_abs_curvature']['opt']:.3f} "
        f"({report['max_abs_curvature']['change_pct']:+.1f}%)"
    )

    method = opt.meta.get("optimization", {}).get("method", "unknown")
    fig.suptitle(f"path_optimizer.py - method={method}",
                 fontsize=13, fontweight="bold")
    fig.tight_layout()
    if save_path:
        fig.savefig(save_path, dpi=110)
        print(f"[plot] saved {save_path}")
    plt.close(fig)


# ============================================================================
# I/O
# ============================================================================

def save_path_json(path: Path, file_path: str, source_report: Optional[dict] = None) -> None:
    payload = {
        "meta": dict(path.meta),
        "points": [
            {"x_m": float(x), "y_m": float(y), "yaw_deg": float(yaw)}
            for x, y, yaw in zip(path.x, path.y, path.yaw_deg)
        ],
    }
    if source_report is not None:
        payload["meta"]["diff_report"] = source_report
    with open(file_path, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2)
    print(f"[json] saved {file_path}  ({len(path)} points)")


# ============================================================================
# CLI
# ============================================================================

def main(argv: list) -> int:
    if not _HAS_SCIPY:
        print("scipy not available (pip install scipy)", file=sys.stderr)
        return 2

    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("input_json",
                    help="TELMISSION recording or LEARN dump")
    ap.add_argument("--source", choices=["recording", "learn_dump"],
                    default="recording")
    ap.add_argument("--method", choices=["savgol", "min_curv", "shorten"],
                    default="min_curv",
                    help="optimization method (default min_curv)")
    ap.add_argument("--max-offset", type=float, default=0.30,
                    help="lateral offset budget per point in meters "
                         "(default 0.30)")
    ap.add_argument("--smoothness-weight", type=float, default=0.0,
                    help="L2 penalty on first-diff of offset (default 0)")
    ap.add_argument("--curvature-weight", type=float, default=1.0,
                    help="curvature penalty for shorten method (default 1)")
    ap.add_argument("--aggressive", action="store_true",
                    help="allow optimized path tighter than original "
                         "(racing-line apex cut). Default is conservative: "
                         "output bisect-scaled so R_opt >= R_orig.")
    ap.add_argument("--feasibility-margin", type=float, default=1.05,
                    help="when --aggressive is OFF, the optimizer is "
                         "allowed up to this multiplier of original max "
                         "curvature (default 1.05 = 5%% slack)")
    ap.add_argument("--savgol-window", type=int, default=11)
    ap.add_argument("--savgol-polyorder", type=int, default=3)
    ap.add_argument("--save-png", default=None)
    ap.add_argument("--save-json", default=None)
    args = ap.parse_args(argv)

    if args.source == "recording":
        orig = Path.from_recording_json(args.input_json)
    else:
        orig = Path.from_learn_dump_json(args.input_json)

    print(f"[load] {args.input_json}: {len(orig)} points, "
          f"L={orig.total_length():.2f}m, R_min={orig.observed_min_radius():.2f}m")

    if args.method == "savgol":
        opt = smooth_savgol(orig, window=args.savgol_window,
                            polyorder=args.savgol_polyorder)
    elif args.method == "min_curv":
        opt = min_curvature(orig, max_offset_m=args.max_offset,
                            smoothness_weight=args.smoothness_weight,
                            enforce_feasibility=not args.aggressive,
                            feasibility_margin=args.feasibility_margin)
    elif args.method == "shorten":
        opt = shorten_arc(orig, max_offset_m=args.max_offset,
                          curvature_weight=args.curvature_weight)
    else:
        ap.error(f"unknown method {args.method}")

    report = diff_report(orig, opt)
    print(f"\n[report] arc: {report['arc_length_m']['orig']:.2f}m -> "
          f"{report['arc_length_m']['opt']:.2f}m "
          f"({report['arc_length_m']['change_pct']:+.1f}%)")
    print(f"[report] max|k|: {report['max_abs_curvature']['orig']:.3f} -> "
          f"{report['max_abs_curvature']['opt']:.3f} 1/m "
          f"({report['max_abs_curvature']['change_pct']:+.1f}%)")
    print(f"[report] R_min:  {report['min_radius_m']['orig']:.2f}m -> "
          f"{report['min_radius_m']['opt']:.2f}m")
    print(f"[report] max lateral offset: "
          f"{report['lateral_offset_m']['max']:.3f}m  "
          f"(budget {args.max_offset:.2f}m)")
    print(f"[report] feasibility: {report['feasibility']['feasibility_note']}")

    if args.save_png:
        plot_comparison(orig, opt, report, save_path=args.save_png)
    if args.save_json:
        save_path_json(opt, args.save_json, source_report=report)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
