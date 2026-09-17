"""
path_pipeline.py - 端到端路径优化管线 (2026-06-09, 路线图 §3 方案#2/#3)

低速人工录制 → 高速参考线,一条命令跑完整管线:

    learn dump (track_dump.py 抓的 8 字段 ins_record_point_t JSON)
      → ① 去跳点   (镜像固件 ins_record_drop_spurious_start + 内部离群点)
      → ② 重采样   (等弧长 ds=0.10m;修复 min_curv 二阶差分在不等距点列上的失真)
      → ③ 平滑     (savgol,窗口按米给,端点钉死)
      → ④ 走廊优化 (path_optimizer.min_curvature,默认 conservative)
      → ⑤ κ 预算   (δ_ff=atan(L·κ) ≤ frac·29°;L 用固件 0.65 实测 + 拟合 0.957 双报)
      → ⑥ 圈时预测 (sim build_velocity_profile = 固件 VPROF 同算法,orig vs opt)
      → ⑦ PP 仿真  (sim_pure_pursuit,VPROF 开;**只看相对排序** — sim 无转向 slew 模型)
      → 输出: paths/<label>_pipeline.png  四联对比图
              paths/<label>_opt.json      优化路径(老 schema,可喂 sim/param_sweep)
              paths/<label>_upload.json   8 字段合成点列(留给未来固件 TRACK UPLOAD)

为什么车端只需要几何:固件 VPROF(2026-06 已固化默认开)在 playback 启动时按
录制几何自己算每点速度 → 离线管线交付优化后的**几何**即可,速度车端自动规划。
upload JSON 里的 enc_spd_mm_s 填规划速度仅作参考(固件当前只用它判别倒车段,
故必须为正 → 本管线 v1 只处理纯前进轨迹,检测到倒车段会告警)。

用法:
    python path_pipeline.py recordings/learn_dump_20260605-052921_rerecord.json
    python path_pipeline.py <dump.json> --ds 0.10 --max-offset 0.30 \
        --smooth-window-m 1.1 --delta-frac 0.75 --full-speed 2.5 --a-lat 2.0 \
        --label rerecord --no-sim
"""
from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path as FsPath

import numpy as np

import sim_pure_pursuit as sim
from path_optimizer import Path, min_curvature, smooth_savgol, diff_report

try:
    import vehicle_params as vp
    WHEELBASE_FITTED_M = float(vp.WHEELBASE_M)      # 0.957 动力学拟合(含侧偏)
except Exception:
    WHEELBASE_FITTED_M = 0.957

WHEELBASE_FIRMWARE_M = 0.65     # INS_CTRL_WHEELBASE_M,卷尺实测;固件 FF/δ_ff 用它
STEER_LIMIT_DEG      = 29.0     # 前舵 ±29° 饱和(固件/实测一致)
PATHS_DIR = FsPath(__file__).resolve().parent / "paths"


# ============================================================================
# ⓪ 方向分段(倒车段切割)
# ============================================================================

def split_forward_segments(raw_pts: list, deadband_mm_s: int = 80,
                           min_len_m: float = 1.0) -> list:
    """按 enc_spd_mm_s 符号把 dump 切成同方向段。

    倒车入库类轨迹在换向点是 cusp(XY 折返),按单条前进路径处理会让曲率
    估计爆炸(实测 κ→32/m)且仿真必 lost。滞回 ±deadband(默认 80,>
    刹停噪声 -38 实测量级)防抖;|enc_spd| 在带内沿用当前方向。
    返回 [{i0, i1, dir(+1/-1), len_m}],已滤掉 < min_len_m 的碎段。"""
    if not raw_pts:
        return []
    segs = []
    cur_dir = 0
    i0 = 0
    for i, p in enumerate(raw_pts):
        v = p.get("enc_spd_mm_s", 0)
        d = 1 if v > deadband_mm_s else (-1 if v < -deadband_mm_s else 0)
        if cur_dir == 0 and d != 0:
            cur_dir = d
        elif d != 0 and d != cur_dir:
            segs.append((i0, i, cur_dir))
            i0, cur_dir = i, d
    segs.append((i0, len(raw_pts), cur_dir if cur_dir != 0 else 1))

    out = []
    for a, b, d in segs:
        if b - a < 3:
            continue
        xs = np.array([raw_pts[k]["px_m"] for k in range(a, b)])
        ys = np.array([raw_pts[k]["py_m"] for k in range(a, b)])
        ln = float(np.sum(np.hypot(np.diff(xs), np.diff(ys))))
        if ln >= min_len_m:
            out.append({"i0": a, "i1": b, "dir": d, "len_m": round(ln, 2)})
    return out


# ============================================================================
# ① 去跳点
# ============================================================================

def despike(path: Path, start_factor: float = 2.0, start_abs_m: float = 0.8,
            spike_factor: float = 3.0) -> tuple:
    """去掉录制起点 DR 跳变伪点 + 内部孤立离群点。

    起点规则镜像固件 ins_record_drop_spurious_start:
        dist(p0,p1) > max(start_factor × median_ds, start_abs_m) → 丢 p0,重复最多 3 次。
    内部规则(保守,只删孤立尖刺):
        点 i 同时满足 离开两侧邻居的距离都 > spike_factor × median_ds
        且邻居互距 < 2 × median_ds(说明 i 是孤立外跳而不是真弯) → 删 i。
    返回 (new_path, report_dict)。"""
    x, y, yaw = path.x.copy(), path.y.copy(), path.yaw_deg.copy()
    dropped_start = 0
    dropped_inner = []

    def seg(xa, ya):
        return np.hypot(np.diff(xa), np.diff(ya))

    for _ in range(3):
        if len(x) < 3:
            break
        ds = seg(x, y)
        med = float(np.median(ds))
        if ds[0] > max(start_factor * med, start_abs_m):
            x, y, yaw = x[1:], y[1:], yaw[1:]
            dropped_start += 1
        else:
            break

    if len(x) >= 5:
        ds = seg(x, y)
        med = float(np.median(ds))
        keep = np.ones(len(x), dtype=bool)
        for i in range(1, len(x) - 1):
            d_prev = math.hypot(x[i] - x[i-1], y[i] - y[i-1])
            d_next = math.hypot(x[i+1] - x[i], y[i+1] - y[i])
            d_skip = math.hypot(x[i+1] - x[i-1], y[i+1] - y[i-1])
            if (d_prev > spike_factor * med and d_next > spike_factor * med
                    and d_skip < 2.0 * med):
                keep[i] = False
                dropped_inner.append(i)
        x, y, yaw = x[keep], y[keep], yaw[keep]

    rep = {"dropped_start": dropped_start, "dropped_inner": dropped_inner}
    out = Path.from_xy(x, y, yaw, meta=dict(path.meta))
    out.meta["despike"] = rep
    return out, rep


# ============================================================================
# ② 等弧长重采样
# ============================================================================

def resample_uniform(path: Path, ds_m: float = 0.10) -> Path:
    """按弧长等距重采样(线性插值 x/y;yaw 重采样后由切线重推导)。

    min_curvature 的二阶差分矩阵把"曲率能量"建立在**等距点列**假设上;
    录制点是 0.10m(弯)/0.50m(直)混合间距,直接优化会把代价权重错配到
    稀疏段。重采样后该代理才成立。"""
    arc = path.cumulative_arc()
    total = float(arc[-1])
    if total < ds_m * 4:
        return path
    n_new = max(int(round(total / ds_m)) + 1, 5)
    s_new = np.linspace(0.0, total, n_new)
    x_new = np.interp(s_new, arc, path.x)
    y_new = np.interp(s_new, arc, path.y)
    out = Path.from_xy(x_new, y_new, meta=dict(path.meta))   # yaw ← 切线
    out.meta["resample"] = {"ds_m": ds_m, "n_in": len(path), "n_out": n_new,
                            "total_len_m": total}
    return out


# ============================================================================
# ⑤ κ / 转向预算
# ============================================================================

def kappa_budget_1pm(wheelbase_m: float, delta_frac: float) -> float:
    """δ_ff = atan(L·κ) ≤ frac·δ_max  →  κ_max = tan(frac·δ_max)/L。"""
    return math.tan(math.radians(STEER_LIMIT_DEG * delta_frac)) / wheelbase_m


def budget_report(path: Path, delta_fracs=(0.7, 0.8)) -> dict:
    """对固件轴距(0.65 实测)与拟合轴距(0.957 含侧偏)分别报 κ 预算占用。

    两个 L 差异巨大(R_min 1.63 vs 2.40m)是已知矛盾:0.65 是几何真值、
    固件 FF 在用;0.957 把轮胎侧偏折进等效轴距,高速可行性按它看更保守。
    报告两个,落车策略由人定。"""
    kap = np.abs(path.curvature())
    arc = path.cumulative_arc()
    out = {"kappa_peak_1pm": float(np.max(kap)) if kap.size else 0.0}
    for L_name, L in (("L_fw_0.65", WHEELBASE_FIRMWARE_M),
                      ("L_fit_0.957", WHEELBASE_FITTED_M)):
        per_frac = {}
        for frac in delta_fracs:
            kmax = kappa_budget_1pm(L, frac)
            viol = kap > kmax
            spans = []
            i = 0
            while i < len(viol):
                if viol[i]:
                    j = i
                    while j + 1 < len(viol) and viol[j + 1]:
                        j += 1
                    spans.append({
                        "s_from_m": round(float(arc[i]), 2),
                        "s_to_m":   round(float(arc[j]), 2),
                        "kappa_peak": round(float(np.max(kap[i:j+1])), 3),
                    })
                    i = j + 1
                else:
                    i += 1
            per_frac[f"frac_{frac:.2f}"] = {
                "kappa_max_1pm": round(kmax, 3),
                "n_points_over": int(np.sum(viol)),
                "pct_over": round(100.0 * float(np.mean(viol)), 1),
                "violating_spans": spans,
            }
        delta_peak = math.degrees(math.atan(L * out["kappa_peak_1pm"]))
        out[L_name] = {"delta_ff_peak_deg": round(delta_peak, 1), **per_frac}
    return out


# ============================================================================
# ④b minimax 优化(2026-06-09:低速录制线 → 高速可跑参考线)
# ============================================================================

def _curv_xy(x: np.ndarray, y: np.ndarray) -> np.ndarray:
    """向量化三点外接圆曲率,与 Path.curvature() 同公式同结果(端点 0)。
    minimax 内层每轮要评估 κ 十几次,Path 版的 python 循环太慢。"""
    n = len(x)
    kap = np.zeros(n)
    if n < 3:
        return kap
    dx1 = x[1:-1] - x[:-2]
    dy1 = y[1:-1] - y[:-2]
    dx2 = x[2:] - x[1:-1]
    dy2 = y[2:] - y[1:-1]
    cross = dx1 * dy2 - dy1 * dx2
    denom = (np.hypot(dx1, dy1) * np.hypot(dx2, dy2)
             * np.hypot(x[2:] - x[:-2], y[2:] - y[:-2]))
    mid = np.zeros(n - 2)
    ok = denom > 1e-9
    mid[ok] = 2.0 * cross[ok] / denom[ok]
    kap[1:-1] = mid
    return kap


def _kappa_jacobian_banded(x, y, nx, ny, eps: float = 1e-5):
    """∂κ/∂α 带状雅可比(α = 沿法向横移)。三点曲率 κ_i 只依赖点 i-1..i+1
    → 带宽 1;按 j%3 三色扰动,色间隔 3 > 2×带宽,响应不重叠,3 次前向差分
    精确恢复全部带内元素。返回 csr 稀疏 (n×n)。"""
    from scipy import sparse
    n = len(x)
    k0 = _curv_xy(x, y)
    rows, cols, vals = [], [], []
    idx = np.arange(n)
    for c in range(3):
        mask = (idx % 3) == c
        dk = (_curv_xy(x + eps * nx * mask, y + eps * ny * mask) - k0) / eps
        js = np.nonzero(mask)[0]
        for off in (-1, 0, 1):
            ii = js + off
            sel = (ii >= 0) & (ii < n)
            rows.append(ii[sel])
            cols.append(js[sel])
            vals.append(dk[ii[sel]])
    return sparse.csr_matrix(
        (np.concatenate(vals), (np.concatenate(rows), np.concatenate(cols))),
        shape=(n, n))


def _minimax_lp_step(x, y, nx, ny, lo, hi, slope_max: float):
    """单步 LP:在当前线性化下直接解 min max|κ|。
    vars z = [α(n), u(n), t]:
        min t + 1e-5·Σu
        s.t.  Jα - t ≤ -κ0 ;  -Jα - t ≤ κ0     (峰值约束,t = max|κ| 上界)
              α - u ≤ 0  ;  -α - u ≤ 0          (u ≥ |α|,L1 锚定:同峰值解
                                                  里挑离录制线最近的;1e-5 远小
                                                  于单轮峰值改善量,不会拿走廊
                                                  换正则)
              |α_{i+1} - α_i| ≤ slope_max       (横移坡度,防 LP 出锯齿)
              lo ≤ α ≤ hi ; u ≥ 0 ; t ≥ 0
    与 IRLS 加权 QP 的本质区别:目标函数就是峰值本身,削峰不再依赖"恰好调
    对权重",邻点等高尖峰在同一个 LP 里被同时压住 → 不打地鼠。"""
    from scipy import sparse
    from scipy.optimize import linprog
    n = len(x)
    k0 = _curv_xy(x, y)
    J = _kappa_jacobian_banded(x, y, nx, ny)
    I = sparse.identity(n, format="csr")
    Zn = sparse.csr_matrix((n, n))
    mt = sparse.csr_matrix(-np.ones((n, 1)))
    zt = sparse.csr_matrix((n, 1))
    D = sparse.diags([-np.ones(n), np.ones(n - 1)], [0, 1],
                     shape=(n - 1, n), format="csr")
    Zd = sparse.csr_matrix((n - 1, n))
    ztd = sparse.csr_matrix((n - 1, 1))
    A = sparse.vstack([
        sparse.hstack([J,  Zn, mt]),
        sparse.hstack([-J, Zn, mt]),
        sparse.hstack([I,  -I, zt]),
        sparse.hstack([-I, -I, zt]),
        sparse.hstack([D,  Zd, ztd]),
        sparse.hstack([-D, Zd, ztd]),
    ], format="csr")
    b = np.concatenate([-k0, k0, np.zeros(2 * n),
                        np.full(2 * (n - 1), slope_max)])
    c = np.concatenate([np.zeros(n), np.full(n, 1e-5), [1.0]])
    bounds = ([(float(l), float(h)) for l, h in zip(lo, hi)]
              + [(0.0, None)] * n + [(0.0, None)])
    res = linprog(c, A_ub=A, b_ub=b, bounds=bounds, method="highs")
    return res.x[:n] if res.success else None


def optimize_minimax(pre: Path, kappa_target: float, max_offset_m: float,
                     smoothness_weight: float, rounds: int = 25,
                     weight_pow: float = 4.0) -> Path:
    """真 min-max(2026-06-10 重写:LP + SQP 信赖域)。

    旧 IRLS(加权能量 QP)在离散发卡弯上打地鼠:削一个尖、邻点等高尖立刻
    冒头,331 实录线 0.7m 走廊只迈出 0.05m 就 "no improving step"。根因是
    加权目标只能**间接**压峰。本版线性化后直接 min t s.t. |κ0+Jα| ≤ t:
      - J 带状雅可比(三色扰动精确恢复,见 _kappa_jacobian_banded)
      - 信赖域限单轮步长,解完用**真实 κ**验收,失败缩域重试(线性化只在
        小步长内可信,实测大步会让真 κ 上坡)
      - 走廊框算 signed 横移 d+α 而不是 |位移| 单边钳,贴边点还能往回走
    kappa_target 仅作记录:LP 一直压到走廊/几何极限(用户目标是"低速录制
    线 → 高速极限",预算内也继续换弯速)。smoothness_weight/weight_pow 兼容
    旧签名,本实现未用(坡度约束顶替平滑职责)。"""
    TRUST_STEP_M = 0.12
    SLOPE_PER_M = 0.30          # 横移坡度上限 0.30 m/m(发卡 3m 内建 ±0.3 足够)
    n = len(pre)
    if n < 7:
        return pre
    arc = pre.cumulative_arc()
    ds = float(arc[-1]) / max(n - 1, 1)
    slope_max = SLOPE_PER_M * max(ds, 1e-3)
    cur_x = pre.x.copy()
    cur_y = pre.y.copy()
    best_x, best_y = cur_x, cur_y
    best_peak = float(np.max(np.abs(_curv_xy(cur_x, cur_y))))
    trust = TRUST_STEP_M
    r = 0
    while r < rounds:
        p = Path.from_xy(cur_x, cur_y, meta=dict(pre.meta))
        nx, ny = p.normals()
        # 走廊框:当前点沿本轮法向的 signed 横移 d ≈ (cur-pre)·n,
        # 新横移 d+α 必须落在 ±corridor 内,再与信赖域取交。
        d = (cur_x - pre.x) * nx + (cur_y - pre.y) * ny
        lo = np.maximum(-max_offset_m - d, -trust)
        hi = np.minimum(max_offset_m - d, trust)
        lo[:2] = hi[:2] = 0.0       # 起/终点几何钉死(mission 起步/到达锚点)
        lo[-2:] = hi[-2:] = 0.0
        bad = lo > hi               # 数值防御:框翻转就地夹零
        lo[bad] = hi[bad] = 0.0
        alpha = _minimax_lp_step(cur_x, cur_y, nx, ny, lo, hi, slope_max)
        peak_cur = float(np.max(np.abs(_curv_xy(cur_x, cur_y))))
        if alpha is None:
            print(f"  [minimax r{r}] LP infeasible (peak {peak_cur:.3f}), stop")
            break
        accepted = False
        for s in (1.0, 0.6, 0.35, 0.2):
            tx = cur_x + s * alpha * nx
            ty = cur_y + s * alpha * ny
            pk = float(np.max(np.abs(_curv_xy(tx, ty))))
            if pk < peak_cur - 1e-4:
                cur_x, cur_y = tx, ty
                accepted = True
                break
        if not accepted:
            if trust > 0.04:
                trust *= 0.5        # 线性化失真 → 缩信赖域同一轮重试
                continue
            print(f"  [minimax r{r}] converged (peak {peak_cur:.3f})")
            break
        peak_new = float(np.max(np.abs(_curv_xy(cur_x, cur_y))))
        used_max = float(np.max(np.hypot(cur_x - pre.x, cur_y - pre.y)))
        print(f"  [minimax r{r}] k_peak={peak_new:.3f} (target {kappa_target:.3f}), "
              f"step x{s}, offset {used_max:.2f}/{max_offset_m}m")
        if peak_new < best_peak:
            best_peak = peak_new
            best_x = cur_x.copy()
            best_y = cur_y.copy()
        r += 1
    out = Path.from_xy(best_x, best_y, meta=dict(pre.meta))
    meta = dict(out.meta)
    meta.setdefault("optimization", {})
    meta["optimization"]["objective"] = "minimax_lp_sqp"
    meta["optimization"]["kappa_target_1pm"] = kappa_target
    meta["optimization"]["kappa_peak_final"] = best_peak
    out.meta = meta
    return out


def optimize_time(pre: Path, kappa_cap: float, max_offset_m: float,
                  sim_params, smoothness_weight: float = 0.0) -> Path:
    """时间最优(2026-06-10:候选族 + 全模型遴选)。

    直接最小化圈时的线性化 LP 实测不可行:离散曲率 ∂κ/∂α ~ 2/Δ² 巨大且
    邻列反号,LP 在任意网格尺度都能"纸面搬运 κ"骗过线性模型(细网格预测
    -3.6s/真实 +2.7s;0.4m 粗 knot 网格仍 -3.0s/+0.5s),真模型验收后步长
    归零。工程解:用两个**已验证**的几何生成器造候选族——
      energy   = 抄近线(直道占优赛道赚时间,331 线实测 -3.2%)
      minimax  = 开弯(κ 峰 -33%,弯速占优赛道赚时间;LP-SQP 版,合成发卡
                 命中理论最优)
      blend    = 两者位置场凸组合(同源同点数,逐点线性混合)
    全部过真 VPROF predict_lap 打分,κ ≤ max(cap, pre)+tol 守门,取圈时最
    小者;"keep"(原线)兜底保证永不劣化。meta 记录完整排行。"""
    pre_peak = float(np.max(np.abs(_curv_xy(pre.x, pre.y))))
    kap_gate = max(kappa_cap, pre_peak) + 2e-3
    cands = [("keep", pre)]
    en = min_curvature(pre, max_offset_m=max_offset_m,
                       smoothness_weight=smoothness_weight,
                       enforce_feasibility=True)
    cands.append(("energy", en))
    mm = optimize_minimax(pre, kappa_cap, max_offset_m, smoothness_weight)
    cands.append(("minimax", mm))
    mm_half = optimize_minimax(pre, kappa_cap, max_offset_m * 0.5,
                               smoothness_weight)
    cands.append(("minimax_half", mm_half))
    for s in (0.25, 0.5, 0.75):
        bx = (1.0 - s) * en.x + s * mm.x
        by = (1.0 - s) * en.y + s * mm.y
        cands.append((f"blend{s:.2f}",
                      Path.from_xy(bx, by, meta=dict(pre.meta))))
    board = []
    for name, cand in cands:
        kpk = float(np.max(np.abs(_curv_xy(cand.x, cand.y))))
        _v, lap = predict_lap(cand, sim_params)
        ok = kpk <= kap_gate
        board.append({"name": name, "lap_s": round(lap, 2),
                      "kappa_peak": round(kpk, 3), "within_budget": bool(ok)})
        print(f"  [time cand] {name:13s} lap={lap:5.2f}s k_peak={kpk:.3f}"
              f"{'' if ok else '  (over kappa gate, skip)'}")
    valid = [(b, c) for b, (_nm, c) in zip(board, cands) if b["within_budget"]]
    best_b, best_path = min(valid, key=lambda t: t[0]["lap_s"])
    print(f"  [time] winner: {best_b['name']} "
          f"(lap {best_b['lap_s']}s, k_peak {best_b['kappa_peak']})")
    out = Path.from_xy(best_path.x, best_path.y, meta=dict(pre.meta))
    meta = dict(out.meta)
    meta.setdefault("optimization", {})
    meta["optimization"]["objective"] = f"time_select:{best_b['name']}"
    meta["optimization"]["kappa_cap_1pm"] = kappa_cap
    meta["optimization"]["leaderboard"] = board
    out.meta = meta
    return out


# ============================================================================
# ⑥⑦ sim 桥接:VPROF 圈时预测 + Pure Pursuit 仿真
# ============================================================================

def to_sim_path(path: Path) -> sim.Path:
    pts = [(float(x), float(y), float(sim.normalize_angle_deg(yaw)))
           for x, y, yaw in zip(path.x, path.y, path.yaw_deg)]
    return sim.Path(points=pts)


def make_sim_params(full_speed_ms: float, a_lat: float, a_acc: float,
                    a_brake: float, kp: float, la_mm: int) -> sim.Params:
    return sim.Params(
        steer_kp=kp, lookahead_mm=la_mm, head_blend=0.0,
        speed_mm_s=full_speed_ms * 1000.0,
        enable_velocity_profile=True,
        a_lat_max=a_lat, a_accel=a_acc, a_brake=a_brake,
        enable_adaptive_lookahead=False,
    )


def predict_lap(path: Path, params: sim.Params) -> tuple:
    """固件 VPROF 同算法(sim.build_velocity_profile,κ 按 yaw 差分)→ (v 数组 m/s, 圈时 s)。"""
    sp = to_sim_path(path)
    v = sim.build_velocity_profile(sp, params)
    arc_m = sp.cumulative_arc() / 1000.0
    ds = np.diff(arc_m)
    v_avg = np.maximum(0.5 * (v[:-1] + v[1:]), 1e-3)
    return v, float(np.sum(ds / v_avg))


def run_pp_sim(path: Path, params: sim.Params, max_steps: int = 30000) -> dict:
    """跑一遍 PP 仿真,提取对比统计。⚠ sim 无转向 slew/差速模型 —
    数字只用于 orig vs opt 相对排序,不能当绝对预测(2026-06-08 教训)。"""
    res = sim.simulate(to_sim_path(path), params, max_steps=max_steps)
    if not res.xte:
        return {"finished": False, "note": "sim produced no frames"}
    xte = np.abs(np.array(res.xte))
    sat = np.array(res.saturated, dtype=float)
    return {
        "finished": bool(res.finished),
        "lost": bool(res.lost),
        "sim_time_s": round(float(res.t[-1]), 1),
        "mean_abs_xte_m": round(float(np.mean(xte)), 3),
        "max_abs_xte_m": round(float(np.max(xte)), 3),
        "steer_saturated_pct": round(100.0 * float(np.mean(sat)), 1),
    }


# ============================================================================
# 输出:upload JSON(8 字段合成)
# ============================================================================

def save_upload_json(path: Path, v_ms: np.ndarray, file_path: FsPath,
                     label: str) -> None:
    """合成 ins_record_point_t 8 字段点列,留给未来固件 TRACK UPLOAD。
    t_ms/enc_dist 由几何+规划速度合成;enc_spd 填规划速度(正值=前进段,
    固件用符号判倒车);vx/vy 由 yaw×v 合成。"""
    arc_m = path.cumulative_arc()
    v = np.maximum(np.asarray(v_ms, dtype=float), 0.05)
    t_ms = np.zeros(len(path))
    for i in range(1, len(path)):
        ds = arc_m[i] - arc_m[i - 1]
        t_ms[i] = t_ms[i - 1] + 1000.0 * ds / max(0.5 * (v[i] + v[i - 1]), 1e-3)
    pts = []
    for i in range(len(path)):
        yaw_rad = math.radians(path.yaw_deg[i])
        pts.append({
            "t_ms": int(round(t_ms[i])),
            "px_m": round(float(path.x[i]), 4),
            "py_m": round(float(path.y[i]), 4),
            "vx_ms": round(float(v[i] * math.cos(yaw_rad)), 3),
            "vy_ms": round(float(v[i] * math.sin(yaw_rad)), 3),
            "yaw_deg": round(float(path.yaw_deg[i]), 2),
            "enc_spd_mm_s": int(round(v[i] * 1000.0)),
            "enc_dist_mm": int(round(arc_m[i] * 1000.0)),
        })
    payload = {
        "meta": {"source": "path_pipeline_synthetic", "label": label,
                 "schema": "ins_record_point_t[8]",
                 "note": "t/enc 字段为合成;固件 TRACK UPLOAD 尚未实现,先行格式约定",
                 "pipeline": path.meta},
        "points": pts,
    }
    with open(file_path, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=1)
    print(f"[json] upload-ready {file_path}  ({len(pts)} points)")


def save_opt_json(path: Path, file_path: FsPath, extra_meta: dict) -> None:
    payload = {
        "meta": {**path.meta, **extra_meta},
        "points": [{"x_m": float(x), "y_m": float(y), "yaw_deg": float(w)}
                   for x, y, w in zip(path.x, path.y, path.yaw_deg)],
    }
    with open(file_path, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=1)
    print(f"[json] optimized {file_path}  ({len(path)} points)")


# ============================================================================
# 四联对比图
# ============================================================================

def plot_dashboard(orig: Path, pre: Path, opt: Path,
                   v_orig: np.ndarray, t_orig: float,
                   v_opt: np.ndarray, t_opt: float,
                   budget: dict, delta_frac: float,
                   sim_orig: dict, sim_opt: dict,
                   save_path: FsPath, label: str) -> None:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    fig, axes = plt.subplots(2, 2, figsize=(15, 11))

    ax = axes[0][0]
    ax.plot(orig.x, orig.y, ".", color="#bbbbbb", markersize=3, label=f"raw dump ({len(orig)}pt)")
    ax.plot(pre.x, pre.y, "-", color="#4477cc", linewidth=1.0, alpha=0.7,
            label=f"resample+smooth ({len(pre)}pt)")
    ax.plot(opt.x, opt.y, "-", color="#FF7F00", linewidth=1.8, label=f"optimized ({len(opt)}pt)")
    ax.plot(orig.x[0], orig.y[0], "go", markersize=9, label="start")
    ax.plot(orig.x[-1], orig.y[-1], "rs", markersize=9, label="end")
    ax.set_aspect("equal", adjustable="datalim")
    ax.grid(True, alpha=0.3); ax.legend(fontsize=8)
    ax.set_title(f"XY  |  L {pre.total_length():.1f}m -> {opt.total_length():.1f}m")

    ax = axes[0][1]
    arc_pre, arc_opt = pre.cumulative_arc(), opt.cumulative_arc()
    ax.plot(arc_pre, np.abs(pre.curvature()), "-", color="#4477cc", linewidth=1.0,
            label="pre-opt |k|")
    ax.plot(arc_opt, np.abs(opt.curvature()), "-", color="#FF7F00", linewidth=1.5,
            label="opt |k|")
    for L_name, L, color in (("L=0.65(fw)", WHEELBASE_FIRMWARE_M, "#cc2222"),
                             ("L=0.957(fit)", WHEELBASE_FITTED_M, "#882288")):
        kmax = kappa_budget_1pm(L, delta_frac)
        ax.axhline(kmax, color=color, linestyle="--", linewidth=1.2,
                   label=f"budget {delta_frac:.0%}δmax {L_name}: {kmax:.2f}")
    ax.grid(True, alpha=0.3); ax.legend(fontsize=8)
    ax.set_xlabel("arc (m)"); ax.set_ylabel("|kappa| (1/m)")
    ax.set_title(f"curvature vs steering budget  |  peak {budget['kappa_peak_1pm']:.2f} 1/m")

    ax = axes[1][0]
    ax.plot(arc_pre, v_orig, "-", color="#4477cc", linewidth=1.2,
            label=f"VPROF pre-opt  ({t_orig:.1f}s)")
    ax.plot(arc_opt, v_opt, "-", color="#FF7F00", linewidth=1.5,
            label=f"VPROF optimized ({t_opt:.1f}s, {100*(t_opt-t_orig)/t_orig:+.1f}%)")
    ax.grid(True, alpha=0.3); ax.legend(fontsize=9)
    ax.set_xlabel("arc (m)"); ax.set_ylabel("planned v (m/s)")
    ax.set_title("firmware-VPROF speed plan (same two-pass algorithm)")

    ax = axes[1][1]
    ax.axis("off")
    rows = [
        ["", "pre-opt", "optimized"],
        ["VPROF lap time (s)", f"{t_orig:.1f}", f"{t_opt:.1f}"],
        ["planned mean v (m/s)", f"{np.mean(v_orig):.2f}", f"{np.mean(v_opt):.2f}"],
        ["sim mean|xte| (m)", str(sim_orig.get("mean_abs_xte_m", "-")),
                              str(sim_opt.get("mean_abs_xte_m", "-"))],
        ["sim max|xte| (m)", str(sim_orig.get("max_abs_xte_m", "-")),
                             str(sim_opt.get("max_abs_xte_m", "-"))],
        ["sim steer sat (%)", str(sim_orig.get("steer_saturated_pct", "-")),
                              str(sim_opt.get("steer_saturated_pct", "-"))],
        ["sim finished", str(sim_orig.get("finished", "-")),
                         str(sim_opt.get("finished", "-"))],
    ]
    tbl = ax.table(cellText=rows, loc="center", cellLoc="center")
    tbl.scale(1.0, 1.6); tbl.set_fontsize(11)
    ax.set_title("comparison (sim = relative ordering ONLY, no slew model)",
                 fontsize=10)

    fig.suptitle(f"path_pipeline - {label}", fontsize=14, fontweight="bold")
    fig.tight_layout()
    fig.savefig(save_path, dpi=110)
    plt.close(fig)
    print(f"[plot] {save_path}")


# ============================================================================
# 主流程
# ============================================================================

def run_pipeline(args) -> int:
    in_path = FsPath(args.input_json)
    label = args.label or in_path.stem.replace("learn_dump_", "")
    PATHS_DIR.mkdir(exist_ok=True)

    # 载入(8 字段 learn dump) + 方向分段
    with open(in_path, "r", encoding="utf-8") as f:
        raw = json.load(f)
    raw_pts = raw["points"] if isinstance(raw, dict) else raw

    segs = split_forward_segments(raw_pts)
    fwd = [s for s in segs if s["dir"] > 0]
    if len(segs) > 1:
        print(f"[segment] {len(segs)} direction segments: "
              + ", ".join(f"[{s['i0']}:{s['i1']}]{'F' if s['dir']>0 else 'R'} {s['len_m']}m"
                          for s in segs))
    if not fwd:
        print("[error] 没有 >=1m 的前进段,无法处理"); return 1
    if args.segment_index is not None:
        sel = segs[args.segment_index]
        if sel["dir"] < 0:
            print(f"[warn] 选中的段 {args.segment_index} 是倒车段:按几何镜像处理,"
                  f"输出仍是前进序——落车语义自行确认")
    else:
        sel = max(fwd, key=lambda s: s["len_m"])
    if len(segs) > 1:
        print(f"[segment] -> 处理 [{sel['i0']}:{sel['i1']}] {sel['len_m']}m"
              f"(其余段丢弃;倒车段优化是后续版本)")

    sl = raw_pts[sel["i0"]:sel["i1"]]
    orig = Path.from_xy(np.array([p["px_m"] for p in sl]),
                        np.array([p["py_m"] for p in sl]),
                        np.array([p["yaw_deg"] for p in sl]),
                        meta={"source": "learn_dump_segment", "file": str(in_path),
                              "segment": sel, "n_segments_total": len(segs)})
    print(f"[load] {in_path.name}: {len(orig)}pt, L={orig.total_length():.2f}m, "
          f"k_peak={orig.max_abs_curvature():.3f}")

    # ① 去跳点
    desp, drep = despike(orig)
    if drep["dropped_start"] or drep["dropped_inner"]:
        print(f"[despike] start -{drep['dropped_start']}, inner -{len(drep['dropped_inner'])}")

    # ② 重采样 → ③ 平滑 = pre-opt 基线(同一几何的干净表达)
    pre = resample_uniform(desp, ds_m=args.ds)
    win_pts = max(int(round(args.smooth_window_m / args.ds)) | 1, 5)
    pre = smooth_savgol(pre, window=win_pts, polyorder=3)
    pre = resample_uniform(pre, ds_m=args.ds)     # savgol 后再均一化一次
    print(f"[pre] resample(ds={args.ds})+savgol(win={win_pts}pt): "
          f"{len(pre)}pt, k_peak={pre.max_abs_curvature():.3f}")

    # ④ 走廊优化。auto:κ 峰超转向预算 → minimax(把线修到高速可跟,救可跑性);
    #    不超 → energy(线已可跑,抄近线赚圈时)。time = 直接最小化圈时
    #    (κ≤预算硬约束),收益依赖速度参数,显式选用。
    kt = args.kappa_target if args.kappa_target else \
         kappa_budget_1pm(WHEELBASE_FIRMWARE_M, args.delta_frac)
    params = make_sim_params(args.full_speed, args.a_lat, args.a_acc,
                             args.a_brake, args.kp, args.la)
    objective = args.objective
    if objective == "auto":
        objective = "minimax" if pre.max_abs_curvature() > kt else "energy"
        print(f"[opt] auto -> {objective} (pre k_peak {pre.max_abs_curvature():.3f} "
              f"vs budget {kt:.3f})")
    if objective == "minimax":
        print(f"[opt] objective=minimax, kappa_target={kt:.3f} 1/m "
              f"(L={WHEELBASE_FIRMWARE_M}, {args.delta_frac:.0%} of {STEER_LIMIT_DEG}deg)")
        opt = optimize_minimax(pre, kt, args.max_offset, args.smoothness_weight)
    elif objective == "time":
        print(f"[opt] objective=time, kappa_cap={kt:.3f} 1/m, "
              f"FULL={args.full_speed} a_lat={args.a_lat}")
        opt = optimize_time(pre, kt, args.max_offset, params)
    else:
        opt = min_curvature(pre, max_offset_m=args.max_offset,
                            smoothness_weight=args.smoothness_weight,
                            enforce_feasibility=not args.aggressive)
    rep = diff_report(pre, opt)
    print(f"[opt] max|k| {rep['max_abs_curvature']['orig']:.3f} -> "
          f"{rep['max_abs_curvature']['opt']:.3f} "
          f"({rep['max_abs_curvature']['change_pct']:+.1f}%), "
          f"offset used {rep['lateral_offset_m']['max']:.2f}/{args.max_offset}m")

    # ⑤ κ 预算
    fracs = tuple(sorted({0.70, args.delta_frac, 0.80}))
    bud_pre, bud_opt = budget_report(pre, fracs), budget_report(opt, fracs)
    key = f"frac_{args.delta_frac:.2f}"
    for name, b in (("pre", bud_pre), ("opt", bud_opt)):
        fw = b["L_fw_0.65"][key]
        print(f"[budget {name}] L=0.65 {args.delta_frac:.0%}dmax: kmax={fw['kappa_max_1pm']} "
              f"-> {fw['n_points_over']}pt over ({fw['pct_over']}%), "
              f"spans={len(fw['violating_spans'])}, dff_peak={b['L_fw_0.65']['delta_ff_peak_deg']}deg")

    # ⑥ VPROF 圈时(固件同算法;params 已在 ④ 前构建)
    v_pre, t_pre = predict_lap(pre, params)
    v_opt, t_opt = predict_lap(opt, params)
    print(f"[vprof] lap {t_pre:.1f}s -> {t_opt:.1f}s ({100*(t_opt-t_pre)/t_pre:+.1f}%), "
          f"mean v {np.mean(v_pre):.2f} -> {np.mean(v_opt):.2f} m/s "
          f"(FULL={args.full_speed}, a_lat={args.a_lat})")

    # ⑦ PP 仿真(可关)
    sim_pre, sim_opt = {}, {}
    if not args.no_sim:
        sim_pre = run_pp_sim(pre, params)
        sim_opt = run_pp_sim(opt, params)
        print(f"[sim pre] {sim_pre}")
        print(f"[sim opt] {sim_opt}")

    # 输出
    extra = {"budget_pre": bud_pre, "budget_opt": bud_opt,
             "vprof": {"full_speed": args.full_speed, "a_lat": args.a_lat,
                       "lap_s_pre": round(t_pre, 1), "lap_s_opt": round(t_opt, 1)},
             "sim_pre": sim_pre, "sim_opt": sim_opt,
             "diff_report": rep}
    save_opt_json(opt, PATHS_DIR / f"{label}_opt.json", extra)
    save_upload_json(opt, v_opt, PATHS_DIR / f"{label}_upload.json", label)
    plot_dashboard(orig, pre, opt, v_pre, t_pre, v_opt, t_opt,
                   bud_opt, args.delta_frac, sim_pre, sim_opt,
                   PATHS_DIR / f"{label}_pipeline.png", label)
    return 0


def main(argv) -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("input_json", help="learn dump JSON (track_dump.py 产物)")
    ap.add_argument("--label", default=None)
    ap.add_argument("--ds", type=float, default=0.10, help="重采样间距 m (默认 0.10)")
    ap.add_argument("--smooth-window-m", type=float, default=0.7,
                    help="savgol 窗口长度,米 (默认 0.7;再大会在 R≈1.2m 急弯系统性"
                         "压平曲率 — 平滑只杀点噪声,整形交给 min_curv)")
    ap.add_argument("--max-offset", type=float, default=0.30,
                    help="走廊预算 m (默认 0.30 — 这是安全约束,按场地实际余量给!)")
    ap.add_argument("--objective", choices=["auto", "minimax", "energy", "time"],
                    default="auto",
                    help="auto(默认)=κ 峰超预算用 minimax(修成高速可跑),否则 energy;"
                         "minimax=压 κ 峰进转向预算;energy=曲率能量最小化(抄近线提速);"
                         "time=直接最小化 VPROF 圈时、κ≤预算硬约束(推荐:提速终极目标,"
                         "结果依赖 --full-speed/--a-lat,按下次实跑参数给)")
    ap.add_argument("--kappa-target", type=float, default=None,
                    help="minimax 的 κ 目标 1/m(默认按 --delta-frac 和 L=0.65 自动算)")
    ap.add_argument("--smoothness-weight", type=float, default=0.0)
    ap.add_argument("--aggressive", action="store_true",
                    help="允许输出比输入更紧(racing line 切弯);默认 conservative")
    ap.add_argument("--delta-frac", type=float, default=0.75,
                    help="κ 预算用的满舵比例 (默认 0.75 = 留 25%% 纠偏余量)")
    ap.add_argument("--full-speed", type=float, default=2.5, help="FULL_SPEED m/s")
    ap.add_argument("--a-lat", type=float, default=2.0)
    ap.add_argument("--a-acc", type=float, default=2.0)
    ap.add_argument("--a-brake", type=float, default=2.0)
    ap.add_argument("--kp", type=float, default=2.0, help="sim 用 PP 增益(幅值,sim 内部极性约定)")
    ap.add_argument("--la", type=int, default=300, help="lookahead mm")
    ap.add_argument("--no-sim", action="store_true", help="跳过 PP 仿真(只要几何+圈时)")
    ap.add_argument("--segment-index", type=int, default=None,
                    help="多方向段时强制选第 N 段(默认取最长前进段)")
    args = ap.parse_args(argv)
    return run_pipeline(args)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
